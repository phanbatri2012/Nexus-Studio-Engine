"""Analyze dominant source-video vocal timbre for Radar casting fallback."""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.request
import uuid
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np
import onnxruntime as ort
from faster_whisper.vad import get_speech_timestamps
from yt_dlp import YoutubeDL

from nexus_engine.paths import DATA_DIR
from nexus_engine.services import security_logging
from nexus_engine.services.proxy_utils import ProxyConfigurationError, parse_proxy_url
from nexus_engine.services.youtube_service import validate_youtube_video_url


LOGGER = logging.getLogger("nexus_engine.source_voice")
MODEL_REPOSITORY = "Alice-Sabrina-Ivy/voice-gender-classifier-onnx-q8"
MODEL_REVISION = "27dcf77b4a1028590ee14db10c2e57edd02e4c61"
ANALYZER_REVISION = "source-voice-v2"
MODEL_FILENAME = "model_quantized.onnx"
MODEL_SHA256 = "33a7f84f17d5ceee61aae8b23488e06fc1e3295a44eaf7eb5744f3e01e380784"
MODEL_URL = (
    f"https://huggingface.co/{MODEL_REPOSITORY}/resolve/{MODEL_REVISION}/"
    f"onnx/{MODEL_FILENAME}"
)
MODEL_CACHE_DIR = DATA_DIR / "model_cache" / "voice_gender_ecapa_q8"
MODEL_PATH = MODEL_CACHE_DIR / MODEL_FILENAME
BENCHMARK_PATH = DATA_DIR / "source_voice_benchmark.json"
TEMP_ROOT = DATA_DIR / "tmp" / "source_voice"
SAMPLE_RATE = 16_000
WINDOW_SECONDS = 12.0
WINDOW_RATIOS = (0.2, 0.5, 0.8)
CLASSIFY_CHUNK_SECONDS = 2.0
MIN_CLASSIFY_CHUNK_SECONDS = 0.5
MAX_CLASSIFY_SAMPLES = int(SAMPLE_RATE * CLASSIFY_CHUNK_SECONDS)
MIN_CLASSIFY_SAMPLES = int(SAMPLE_RATE * MIN_CLASSIFY_CHUNK_SECONDS)
MIN_TOTAL_SPEECH_SECONDS = 9.0
MIN_AGGREGATE_PROBABILITY = 0.85
MIN_DURATION_AGREEMENT = 0.80
MAX_MODEL_BYTES = 20 * 1024 * 1024
MAX_SOURCE_AUDIO_BYTES = 256 * 1024 * 1024
SOURCE_SOCKET_TIMEOUT_SECONDS = 30
SOURCE_DOWNLOAD_RETRIES = 2
FFMPEG_TIMEOUT_SECONDS = 30
ANALYSIS_TIMEOUT_SECONDS = 240


class SourceVoiceAnalysisError(RuntimeError):
    """Raised when source analysis cannot safely produce evidence."""

    failure_code = "audio_decode_failed"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        failure_code: str | None = None,
        retryable: bool | None = None,
        diagnostic: object = "",
    ) -> None:
        super().__init__(message)
        if failure_code:
            self.failure_code = failure_code
        if retryable is not None:
            self.retryable = bool(retryable)
        self.diagnostic = security_logging.redact_sensitive(diagnostic)


class SourceVoiceDownloadError(SourceVoiceAnalysisError):
    failure_code = "source_media_unavailable"
    retryable = True


class SourceVoiceDecodeError(SourceVoiceAnalysisError):
    failure_code = "audio_decode_failed"
    retryable = False


class SourceVoiceModelError(SourceVoiceAnalysisError):
    failure_code = "model_invalid"
    retryable = False


def _is_timeout_exception(exc: BaseException) -> bool:
    current: BaseException | None = exc
    for _depth in range(8):
        if current is None:
            return False
        if isinstance(current, (TimeoutError, socket.timeout)):
            return True
        message = str(current).lower()
        if "timed out" in message or "timeout" in message or "time out" in message:
            return True
        current = current.__cause__ or current.__context__
    return False


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def benchmark_gate_status() -> dict:
    result = {
        "enabled": False,
        "mode": "advisory",
        "reason": "Chưa có benchmark tiếng Việt đạt điều kiện 10 nam + 10 nữ.",
    }
    try:
        payload = json.loads(BENCHMARK_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return result
    try:
        male_count = int(payload.get("male_count") or 0)
        female_count = int(payload.get("female_count") or 0)
        error_rate = float(payload.get("error_rate") or 1.0)
    except (TypeError, ValueError, OverflowError):
        return result
    if (
        male_count < 0
        or female_count < 0
        or not math.isfinite(error_rate)
        or not 0.0 <= error_rate <= 1.0
    ):
        return result
    enabled = male_count >= 10 and female_count >= 10 and error_rate <= 0.05
    return {
        "enabled": enabled,
        "mode": "auto" if enabled else "advisory",
        "reason": (
            "Benchmark tiếng Việt đạt ngưỡng."
            if enabled
            else "Benchmark tiếng Việt chưa đạt ngưỡng sai số tối đa 5%."
        ),
        "male_count": male_count,
        "female_count": female_count,
        "error_rate": error_rate,
    }


def model_status() -> dict:
    status = "missing"
    actual_checksum = ""
    if MODEL_PATH.is_file():
        try:
            actual_checksum = _sha256(MODEL_PATH)
            status = "ready" if actual_checksum == MODEL_SHA256 else "invalid"
        except OSError:
            status = "invalid"
    return {
        "status": status,
        "revision": MODEL_REVISION,
        "analyzer_revision": ANALYZER_REVISION,
        "checksum": MODEL_SHA256,
        "actual_checksum": actual_checksum,
        "size_bytes": MODEL_PATH.stat().st_size if MODEL_PATH.is_file() else 0,
        "benchmark": benchmark_gate_status(),
        "classifier_scope": "binary_vocal_timbre",
    }


def ensure_model() -> Path:
    current = model_status()
    if current["status"] == "ready":
        return MODEL_PATH
    if current["status"] == "invalid":
        raise SourceVoiceModelError(
            "Model phân tích âm sắc có checksum không hợp lệ; không dùng fallback."
        )
    MODEL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    partial = MODEL_CACHE_DIR / f".{MODEL_FILENAME}.{os.getpid()}.part"
    try:
        request = urllib.request.Request(
            MODEL_URL,
            headers={"User-Agent": "Nexus-Studio-Engine/source-voice-model"},
        )
        with urllib.request.urlopen(
            request, timeout=120
        ) as response, partial.open("wb") as output:
            total = 0
            for block in iter(lambda: response.read(1024 * 1024), b""):
                total += len(block)
                if total > MAX_MODEL_BYTES:
                    raise SourceVoiceModelError("Artifact model vượt giới hạn 20 MB.")
                output.write(block)
        if _sha256(partial) != MODEL_SHA256:
            raise SourceVoiceModelError("Artifact model tải về sai checksum.")
        os.replace(partial, MODEL_PATH)
    except SourceVoiceModelError:
        raise
    except Exception as exc:
        raise SourceVoiceModelError(
            "Không thể tải model phân tích âm sắc đã pin.", diagnostic=exc
        ) from exc
    finally:
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
    return MODEL_PATH


def _window_bounds(duration_seconds: float) -> list[tuple[float, float]]:
    if duration_seconds <= 0:
        raise SourceVoiceDecodeError("Thiếu thời lượng video nguồn.")
    bounds = []
    for ratio in WINDOW_RATIOS:
        center = duration_seconds * ratio
        start = max(
            0.0,
            min(center - WINDOW_SECONDS / 2.0, duration_seconds - WINDOW_SECONDS),
        )
        end = min(duration_seconds, start + WINDOW_SECONDS)
        bounds.append((round(start, 3), round(end, 3)))
    return bounds


def _download_source(url: str, *, proxy_url: str, output_stem: Path) -> Path:
    template = str(output_stem) + ".%(ext)s"

    def enforce_size_limit(status: dict) -> None:
        downloaded = int(status.get("downloaded_bytes") or 0)
        expected = int(
            status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        )
        if max(downloaded, expected) > MAX_SOURCE_AUDIO_BYTES:
            raise SourceVoiceDownloadError(
                "Audio nguồn vượt giới hạn dung lượng an toàn."
            )

    options = {
        "format": "bestaudio[abr<=96]/worstaudio/best[height<=360]/best",
        "outtmpl": template,
        "proxy": proxy_url,
        "noplaylist": True,
        "quiet": True,
        "noprogress": True,
        "no_warnings": True,
        "socket_timeout": SOURCE_SOCKET_TIMEOUT_SECONDS,
        "retries": SOURCE_DOWNLOAD_RETRIES,
        "fragment_retries": SOURCE_DOWNLOAD_RETRIES,
        "file_access_retries": 1,
        "max_filesize": MAX_SOURCE_AUDIO_BYTES,
        "progress_hooks": [enforce_size_limit],
    }
    try:
        with YoutubeDL(options) as downloader:
            downloader.download([url])
    except SourceVoiceDownloadError:
        raise
    except Exception as exc:
        if _is_timeout_exception(exc):
            raise SourceVoiceAnalysisError(
                "Tải audio nguồn đã vượt timeout.",
                failure_code="source_media_timeout",
                retryable=True,
                diagnostic=exc,
            ) from exc
        raise SourceVoiceDownloadError(
            "Không thể tải audio nguồn qua proxy.", diagnostic=exc
        ) from exc
    matches = [
        path
        for path in output_stem.parent.glob(output_stem.name + ".*")
        if path.suffix not in {".part", ".ytdl", ".wav"} and path.is_file()
    ]
    if not matches:
        raise SourceVoiceDownloadError("yt-dlp không tạo được audio nguồn qua proxy.")
    source = max(matches, key=lambda path: path.stat().st_size)
    if source.stat().st_size > MAX_SOURCE_AUDIO_BYTES:
        raise SourceVoiceDownloadError("Audio nguồn vượt giới hạn dung lượng an toàn.")
    return source


def _extract_window_to_mono_wav(
    source: Path,
    target: Path,
    *,
    start: float,
    end: float,
) -> None:
    duration = max(0.0, min(WINDOW_SECONDS, end - start))
    if duration <= 0:
        raise SourceVoiceDecodeError("Cửa sổ audio nguồn không hợp lệ.")
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-i",
        str(source),
        "-t",
        f"{duration:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise SourceVoiceAnalysisError(
            "Chuẩn hóa audio nguồn đã vượt timeout.",
            failure_code="source_media_timeout",
            retryable=True,
            diagnostic=exc,
        ) from exc
    if completed.returncode != 0 or not target.is_file():
        raise SourceVoiceDecodeError(
            "Không thể chuẩn hóa audio mẫu về mono 16 kHz.",
            diagnostic=completed.stderr,
        )


def _read_wav(path: Path) -> np.ndarray:
    try:
        with wave.open(str(path), "rb") as handle:
            if handle.getnchannels() != 1 or handle.getframerate() != SAMPLE_RATE:
                raise SourceVoiceDecodeError("Audio mẫu không đúng mono 16 kHz.")
            samples = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    except SourceVoiceDecodeError:
        raise
    except (OSError, EOFError, wave.Error) as exc:
        raise SourceVoiceDecodeError("Không đọc được audio mẫu đã chuẩn hóa.") from exc
    return samples.astype(np.float32) / 32768.0


def _voiced_audio(audio: np.ndarray) -> tuple[np.ndarray, float]:
    try:
        timestamps = get_speech_timestamps(audio, sampling_rate=SAMPLE_RATE)
    except Exception as exc:
        raise SourceVoiceDecodeError(
            "Không thể phát hiện vùng giọng nói trong audio mẫu.", diagnostic=exc
        ) from exc
    chunks = []
    sample_count = 0
    for timestamp in timestamps:
        start = max(0, int(timestamp.get("start") or 0))
        end = min(len(audio), int(timestamp.get("end") or 0))
        if end > start:
            chunks.append(audio[start:end])
            sample_count += end - start
    if not chunks:
        return np.empty(0, dtype=np.float32), 0.0
    return np.concatenate(chunks), sample_count / SAMPLE_RATE


def _softmax(logits: np.ndarray) -> np.ndarray:
    flattened = np.asarray(logits, dtype=np.float64).reshape(-1)
    shifted = flattened - np.max(flattened)
    probabilities = np.exp(shifted)
    return probabilities / np.sum(probabilities)


def _classify(session: ort.InferenceSession, audio: np.ndarray) -> tuple[float, float]:
    if len(audio) < MIN_CLASSIFY_SAMPLES or len(audio) > MAX_CLASSIFY_SAMPLES:
        raise SourceVoiceModelError(
            "Kích thước chunk audio nằm ngoài giới hạn inference.",
            failure_code="model_inference_failed",
        )
    input_name = session.get_inputs()[0].name
    try:
        logits = session.run(
            None, {input_name: audio.reshape(1, -1).astype(np.float32)}
        )[0]
    except Exception as exc:
        raise SourceVoiceModelError(
            "Model không thể phân tích chunk audio đã giới hạn.",
            failure_code="model_inference_failed",
            diagnostic=exc,
        ) from exc
    probabilities = _softmax(logits)
    if probabilities.size != 2 or not np.all(np.isfinite(probabilities)):
        raise SourceVoiceModelError(
            "Model trả về logits không hợp lệ.",
            failure_code="model_inference_failed",
        )
    return float(probabilities[0]), float(probabilities[1])


def _classify_chunks(
    session: ort.InferenceSession, audio: np.ndarray
) -> tuple[float, float, float]:
    weighted_male = 0.0
    weighted_female = 0.0
    classified_samples = 0
    for start in range(0, len(audio), MAX_CLASSIFY_SAMPLES):
        chunk = audio[start : start + MAX_CLASSIFY_SAMPLES]
        if len(chunk) < MIN_CLASSIFY_SAMPLES:
            continue
        male_probability, female_probability = _classify(session, chunk)
        weighted_male += male_probability * len(chunk)
        weighted_female += female_probability * len(chunk)
        classified_samples += len(chunk)
    if classified_samples <= 0:
        return 0.0, 0.0, 0.0
    return (
        weighted_male / classified_samples,
        weighted_female / classified_samples,
        classified_samples / SAMPLE_RATE,
    )


def _analyze_source_voice_impl(
    url: str,
    *,
    duration_seconds: float,
    proxy: str,
    run_id: str = "",
) -> dict:
    validated_url = validate_youtube_video_url(url)
    proxy_url = parse_proxy_url(proxy)
    if not proxy_url:
        raise ProxyConfigurationError(
            "Kênh đích thiếu proxy GPM hợp lệ; phân tích giọng nguồn trực tiếp đã bị chặn."
        )
    model_path = ensure_model()
    benchmark = benchmark_gate_status()
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        session = ort.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
    except Exception as exc:
        raise SourceVoiceModelError(
            "Không thể khởi tạo model phân tích âm sắc.", diagnostic=exc
        ) from exc

    window_results: list[dict] = []
    try:
        safe_run_id = "".join(
            character for character in str(run_id or "") if character.isalnum()
        )[:32]
        temp_prefix = f"radar-{safe_run_id}-" if safe_run_id else "radar-"
        with tempfile.TemporaryDirectory(prefix=temp_prefix, dir=TEMP_ROOT) as temp_name:
            temp_dir = Path(temp_name)
            source = _download_source(
                validated_url,
                proxy_url=proxy_url,
                output_stem=temp_dir / "source",
            )
            for index, (start, end) in enumerate(
                _window_bounds(float(duration_seconds))
            ):
                wav_path = temp_dir / f"window_{index}.wav"
                _extract_window_to_mono_wav(source, wav_path, start=start, end=end)
                voiced, detected_speech_seconds = _voiced_audio(_read_wav(wav_path))
                male_probability, female_probability, classified_seconds = (
                    _classify_chunks(session, voiced)
                )
                if classified_seconds <= 0:
                    window_results.append(
                        {
                            "index": index,
                            "speech_seconds": 0.0,
                            "detected_speech_seconds": round(detected_speech_seconds, 3),
                            "male_probability": 0.0,
                            "female_probability": 0.0,
                            "label": "any",
                        }
                    )
                    continue
                window_results.append(
                    {
                        "index": index,
                        "speech_seconds": round(classified_seconds, 3),
                        "detected_speech_seconds": round(detected_speech_seconds, 3),
                        "male_probability": male_probability,
                        "female_probability": female_probability,
                        "label": "male" if male_probability >= female_probability else "female",
                    }
                )
    except (ProxyConfigurationError, SourceVoiceAnalysisError):
        raise
    except Exception as exc:
        raise SourceVoiceDecodeError(
            "Phân tích audio nguồn thất bại.", diagnostic=exc
        ) from exc

    total_speech = sum(item["speech_seconds"] for item in window_results)
    if total_speech > 0:
        male_probability = sum(
            item["male_probability"] * item["speech_seconds"]
            for item in window_results
        ) / total_speech
        female_probability = sum(
            item["female_probability"] * item["speech_seconds"]
            for item in window_results
        ) / total_speech
    else:
        male_probability = female_probability = 0.0
    predicted = "male" if male_probability >= female_probability else "female"
    confidence = max(male_probability, female_probability)
    agreeing_seconds = sum(
        item["speech_seconds"]
        for item in window_results
        if item["label"] == predicted
    )
    agreement = agreeing_seconds / total_speech if total_speech else 0.0
    threshold_passed = (
        total_speech >= MIN_TOTAL_SPEECH_SECONDS
        and confidence >= MIN_AGGREGATE_PROBABILITY
        and agreement >= MIN_DURATION_AGREEMENT
    )
    auto_enabled = bool(benchmark.get("enabled"))
    accepted = threshold_passed and auto_enabled
    status = (
        "ready"
        if accepted
        else "advisory"
        if threshold_passed and not auto_enabled
        else "insufficient_speech"
        if total_speech < MIN_TOTAL_SPEECH_SECONDS
        else "uncertain"
    )
    return {
        "status": status,
        "gender": predicted if accepted else "any",
        "predicted_timbre": predicted if total_speech else "any",
        "confidence": round(confidence, 6),
        "male_probability": round(male_probability, 6),
        "female_probability": round(female_probability, 6),
        "speech_seconds": round(total_speech, 3),
        "agreement": round(agreement, 6),
        "threshold_passed": threshold_passed,
        "benchmark_gate": benchmark,
        "analyzer_revision": ANALYZER_REVISION,
        "model_revision": MODEL_REVISION,
        "model_checksum": MODEL_SHA256,
        "windows": window_results,
    }


def _worker_failure(exc: BaseException) -> dict:
    if isinstance(exc, SourceVoiceAnalysisError):
        return {
            "failure_code": exc.failure_code,
            "retryable": bool(exc.retryable),
        }
    if isinstance(exc, ProxyConfigurationError):
        return {"failure_code": "source_media_unavailable", "retryable": False}
    return {"failure_code": "audio_decode_failed", "retryable": False}


def _worker_main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        result = _analyze_source_voice_impl(
            str(payload.get("url") or ""),
            duration_seconds=float(payload.get("duration_seconds") or 0),
            proxy=str(payload.get("proxy") or ""),
            run_id=str(payload.get("run_id") or ""),
        )
        response = {"ok": True, "result": result}
    except BaseException as exc:
        response = {"ok": False, **_worker_failure(exc)}
        diagnostic = getattr(exc, "diagnostic", "") or exc
        LOGGER.error(
            "Source voice worker failed (%s): %s",
            response["failure_code"],
            security_logging.redact_sensitive(diagnostic),
        )
    sys.stdout.write(json.dumps(response, ensure_ascii=True))
    sys.stdout.flush()
    return 0


def _run_analysis_subprocess(payload: dict) -> dict:
    command = [
        sys.executable,
        "-m",
        "nexus_engine.services.source_voice_analyzer",
        "--worker",
    ]
    env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[2])
    existing_pythonpath = str(env.get("PYTHONPATH") or "").strip()
    env["PYTHONPATH"] = (
        source_root
        if not existing_pythonpath
        else source_root + os.pathsep + existing_pythonpath
    )
    try:
        completed = subprocess.run(
            command,
            input=json.dumps(payload),
            capture_output=True,
            check=False,
            timeout=ANALYSIS_TIMEOUT_SECONDS,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        _cleanup_run_temp(str(payload.get("run_id") or ""))
        raise SourceVoiceAnalysisError(
            "Phân tích âm sắc đã vượt timeout an toàn.",
            failure_code="source_media_timeout",
            retryable=True,
            diagnostic=exc,
        ) from exc
    finally:
        _cleanup_run_temp(str(payload.get("run_id") or ""))
    safe_stderr = security_logging.redact_sensitive(
        str(completed.stderr or "").replace("\x00", "")
    )
    try:
        response = json.loads(completed.stdout or "{}")
    except (TypeError, ValueError) as exc:
        raise SourceVoiceModelError(
            "Worker phân tích âm sắc trả về dữ liệu không hợp lệ.",
            failure_code="model_inference_failed",
            diagnostic=safe_stderr or exc,
        ) from exc
    if completed.returncode != 0 or not response.get("ok"):
        failure_code = str(response.get("failure_code") or "model_inference_failed")
        retryable = bool(response.get("retryable"))
        error_type = (
            SourceVoiceDownloadError
            if failure_code in {"source_media_timeout", "source_media_unavailable"}
            else SourceVoiceDecodeError
            if failure_code == "audio_decode_failed"
            else SourceVoiceModelError
        )
        raise error_type(
            "Worker không thể hoàn tất phân tích âm sắc.",
            failure_code=failure_code,
            retryable=retryable,
            diagnostic=safe_stderr,
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise SourceVoiceModelError(
            "Worker phân tích âm sắc không trả về profile hợp lệ.",
            failure_code="model_inference_failed",
            diagnostic=safe_stderr,
        )
    return result


def _cleanup_run_temp(run_id: str) -> None:
    safe_run_id = "".join(
        character for character in str(run_id or "") if character.isalnum()
    )[:32]
    if not safe_run_id or not TEMP_ROOT.is_dir():
        return
    try:
        resolved_root = TEMP_ROOT.resolve()
    except OSError:
        return
    for path in TEMP_ROOT.glob(f"radar-{safe_run_id}-*"):
        try:
            stays_in_temp_root = path.resolve().parent == resolved_root
        except OSError:
            stays_in_temp_root = False
        if stays_in_temp_root and path.is_dir():
            shutil.rmtree(path, ignore_errors=True)


def analyze_source_voice(
    url: str,
    *,
    duration_seconds: float,
    proxy: str,
) -> dict:
    """Return bounded vocal-timbre evidence from an isolated worker process."""
    validated_url = validate_youtube_video_url(url)
    proxy_url = parse_proxy_url(proxy)
    if not proxy_url:
        raise ProxyConfigurationError(
            "Kênh đích thiếu proxy GPM hợp lệ; phân tích giọng nguồn trực tiếp đã bị chặn."
        )
    return _run_analysis_subprocess(
        {
            "url": validated_url,
            "duration_seconds": float(duration_seconds),
            "proxy": proxy_url,
            "run_id": uuid.uuid4().hex,
        }
    )


if __name__ == "__main__" and "--worker" in sys.argv:
    raise SystemExit(_worker_main())
