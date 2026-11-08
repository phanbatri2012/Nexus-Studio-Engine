"""Loopback HTTP client for the VieNeu-TTS v3 Turbo provider."""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import wave
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nexus_engine.paths import VIENEU_TTS_SETTINGS_PATH
from nexus_engine.services import security_logging


ENV_API_BASE_URL = "NEXUS_VIENEU_TTS_API_URL"
SUPPORTED_PROVIDER = "vieneu_v3_turbo"


class VieNeuRuntimeError(RuntimeError):
    pass


def api_base_url() -> str:
    configured = str(os.environ.get(ENV_API_BASE_URL) or "").strip()
    if not configured and VIENEU_TTS_SETTINGS_PATH.is_file():
        configured = str(_read_json(VIENEU_TTS_SETTINGS_PATH).get("api_base_url") or "").strip()
    if not configured:
        raise VieNeuRuntimeError(
            f"Chưa cấu hình {ENV_API_BASE_URL} hoặc api_base_url trong vieneu_tts_settings.json."
        )
    parsed = urlparse(configured)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise VieNeuRuntimeError("VieNeu API phải dùng HTTP loopback cục bộ.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise VieNeuRuntimeError("VieNeu API URL không hợp lệ.")
    return configured.rstrip("/")


def _api_json(path: str, *, timeout_seconds: float = 10.0) -> dict:
    request = urllib.request.Request(
        f"{api_base_url()}{path}",
        headers={"Accept": "application/json", "User-Agent": "NexusStudio/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=max(1.0, timeout_seconds)) as response:
            payload = json.loads(response.read(1024 * 1024).decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise VieNeuRuntimeError("Không kết nối được VieNeu API.") from exc
    if not isinstance(payload, dict):
        raise VieNeuRuntimeError("VieNeu API trả dữ liệu không hợp lệ.")
    return payload


def api_health() -> dict:
    try:
        api_base_url()
    except VieNeuRuntimeError as exc:
        return {
            "available": False,
            "code": "api_unconfigured",
            "detail": security_logging.redact_sensitive(exc),
        }
    try:
        payload = _api_json("/health", timeout_seconds=5.0)
        ready = payload.get("status") == "ok"
        return {
            "available": ready,
            "code": "ready" if ready else "api_unavailable",
            "detail": "VieNeu API sẵn sàng." if ready else "VieNeu API chưa sẵn sàng.",
            "backend": str(payload.get("backend") or ""),
            "sample_rate": int(payload.get("sample_rate") or 0),
            "active": int(payload.get("active") or 0),
            "waiting": int(payload.get("waiting") or 0),
        }
    except VieNeuRuntimeError as exc:
        return {
            "available": False,
            "code": "api_unavailable",
            "detail": security_logging.redact_sensitive(exc),
        }


def api_voices() -> list[dict]:
    payload = _api_json("/v1/voices", timeout_seconds=10.0)
    voices = payload.get("data")
    if not isinstance(voices, list):
        raise VieNeuRuntimeError("VieNeu API trả danh sách giọng không hợp lệ.")
    return [deepcopy(voice) for voice in voices if isinstance(voice, dict)]


def api_voice(voice_id: str) -> dict:
    selected = str(voice_id or "").strip()
    voice = next((item for item in api_voices() if str(item.get("id") or "") == selected), None)
    if voice is None:
        raise VieNeuRuntimeError("Giọng đã chọn không tồn tại trên VieNeu.")
    return voice


def generate_api_audio(
    *,
    text: str,
    provider_voice_id: str,
    seed: int,
    output_path: Path,
    timeout_seconds: float = 1800.0,
    cancel_check: Any = None,
) -> dict:
    normalized_text = str(text or "").strip()
    if not normalized_text:
        raise VieNeuRuntimeError("Nội dung tạo audio không được để trống.")
    payload = json.dumps(
        {
            "model": "vieneu-v3-turbo",
            "input": normalized_text,
            "voice": str(provider_voice_id or "").strip(),
            "response_format": "pcm",
            "stream_format": "audio",
            "sample_rate": 48000,
            "seed": int(seed) & 0xFFFFFFFF,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{api_base_url()}/v1/audio/speech",
        data=payload,
        method="POST",
        headers={
            "Accept": "audio/pcm",
            "Content-Type": "application/json; charset=utf-8",
            "User-Agent": "NexusStudio/1.0",
        },
    )
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".tmp.wav")
    started = time.monotonic()
    frame_bytes = 0
    request_id = ""
    remainder = b""
    try:
        with urllib.request.urlopen(
            request,
            timeout=min(max(5.0, float(timeout_seconds)), 60.0),
        ) as response:
            request_id = str(response.headers.get("X-Request-Id") or "")
            with wave.open(str(temporary), "wb") as target:
                target.setnchannels(1)
                target.setsampwidth(2)
                target.setframerate(48000)
                for _read_index in range(262_144):
                    if cancel_check is not None:
                        cancel_check()
                    if time.monotonic() - started > timeout_seconds:
                        raise VieNeuRuntimeError("VieNeu API vượt thời gian tạo audio.")
                    chunk = response.read(64 * 1024)
                    if not chunk:
                        break
                    chunk = remainder + chunk
                    aligned = len(chunk) - (len(chunk) % 2)
                    if aligned:
                        target.writeframesraw(chunk[:aligned])
                        frame_bytes += aligned
                    remainder = chunk[aligned:]
                else:
                    raise VieNeuRuntimeError("VieNeu API trả audio vượt giới hạn an toàn.")
        if remainder:
            raise VieNeuRuntimeError("VieNeu API trả PCM không toàn vẹn.")
        if frame_bytes <= 0:
            raise VieNeuRuntimeError("VieNeu API không trả audio.")
        os.replace(temporary, output)
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(64 * 1024).decode("utf-8")).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        raise VieNeuRuntimeError(
            str(security_logging.redact_sensitive(detail or "VieNeu API từ chối yêu cầu."))
        ) from exc
    except (OSError, urllib.error.URLError) as exc:
        raise VieNeuRuntimeError("Mất kết nối với VieNeu API khi tạo audio.") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "provider": SUPPORTED_PROVIDER,
        "provider_request_id": request_id,
        "provider_voice_id": str(provider_voice_id or "").strip(),
        "sample_rate": 48000,
        "seed": int(seed) & 0xFFFFFFFF,
        "duration_seconds": round(frame_bytes / 2 / 48000.0, 3),
        "sha256": file_sha256(output),
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VieNeuRuntimeError(f"Không đọc được {path.name}.") from exc
    if not isinstance(value, dict):
        raise VieNeuRuntimeError(f"{path.name} phải là JSON object.")
    return value


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


class VieNeuGenerationSession:
    def __init__(
        self,
        *,
        job_id: str,
        provider_voice_id: str,
        work_dir: Path,
        timeout_seconds: float = 900,
        cancel_check: Any = None,
    ) -> None:
        self.job_id = str(job_id)
        self.provider_voice_id = str(provider_voice_id or "").strip()
        self.work_dir = Path(work_dir).resolve()
        self.timeout_seconds = float(timeout_seconds)
        self.cancel_check = cancel_check
        self._voice: dict = {}

    def __enter__(self) -> "VieNeuGenerationSession":
        status = api_health()
        if not status.get("available"):
            raise VieNeuRuntimeError(
                str(status.get("detail") or "VieNeu API chưa sẵn sàng.")
            )
        self._voice = api_voice(self.provider_voice_id)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        return self

    def generate(self, *, text: str, seed: int, output_path: Path) -> dict:
        if not self._voice:
            raise VieNeuRuntimeError("VieNeu API session chưa khởi động.")
        output_path = Path(output_path).resolve()
        receipt_path = output_path.with_suffix(".receipt.json")
        receipt = generate_api_audio(
            text=text,
            provider_voice_id=self.provider_voice_id,
            seed=seed,
            output_path=output_path,
            timeout_seconds=self.timeout_seconds,
            cancel_check=self.cancel_check,
        )
        receipt["request_hash"] = hashlib.sha256(
            json.dumps(
                {
                    "text": str(text or ""),
                    "seed": int(seed),
                    "provider_voice_id": self.provider_voice_id,
                    "voice_revision": int(self._voice.get("revision") or 1),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        _write_json_atomic(receipt_path, receipt)
        return receipt

    def close(self) -> None:
        self._voice = {}

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()


def run_candidate(
    *,
    job_id: str,
    text: str,
    seed: int,
    provider_voice_id: str,
    output_path: Path,
    timeout_seconds: float = 900,
) -> dict:
    with VieNeuGenerationSession(
        job_id=job_id,
        provider_voice_id=provider_voice_id,
        work_dir=Path(output_path).parent,
        timeout_seconds=timeout_seconds,
    ) as session:
        return session.generate(text=text, seed=seed, output_path=output_path)
