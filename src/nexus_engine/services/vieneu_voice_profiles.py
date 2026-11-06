"""Managed, immutable VieNeu voice profile revisions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np

from nexus_engine.paths import (
    SHORT_VIDEO_AUDIO_PROFILES_DIR,
    VIENEU_TTS_VOICE_PROFILES_DIR,
)
from nexus_engine.services import security_logging, vieneu_tts


SUPPORTED_SOURCE_SUFFIXES = frozenset(
    {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".webp"}
)
MIN_REFERENCE_SECONDS = 3.0
MAX_REFERENCE_SECONDS = 8.0
MAX_SOURCE_BYTES = 100 * 1024 * 1024
_VOICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PROFILE_LOCK = threading.RLock()


class VieNeuVoiceProfileError(RuntimeError):
    pass


def _voice_id(value: object) -> str:
    normalized = str(value or "").strip()
    if not _VOICE_ID_PATTERN.fullmatch(normalized):
        raise VieNeuVoiceProfileError("Voice ID VieNeu không hợp lệ.")
    return normalized


def voice_directory(voice_id: str) -> Path:
    return VIENEU_TTS_VOICE_PROFILES_DIR / _voice_id(voice_id)


def revision_directory(voice_id: str, revision: int) -> Path:
    return voice_directory(voice_id) / "revisions" / str(max(1, int(revision)))


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VieNeuVoiceProfileError(f"Không đọc được {path.name}.") from exc
    if not isinstance(value, dict):
        raise VieNeuVoiceProfileError(f"{path.name} phải là JSON object.")
    return value


def _write_json_atomic(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _wav_metrics(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_rate = source.getframerate()
            sample_width = source.getsampwidth()
            frame_count = source.getnframes()
            frames = source.readframes(frame_count)
    except (OSError, wave.Error) as exc:
        raise VieNeuVoiceProfileError("Reference WAV không giải mã được.") from exc
    if channels != 1 or sample_rate != 48000 or sample_width != 2:
        raise VieNeuVoiceProfileError("Reference phải là mono PCM16 48 kHz.")
    duration = frame_count / max(1, sample_rate)
    if duration < MIN_REFERENCE_SECONDS - 0.02 or duration > MAX_REFERENCE_SECONDS + 0.02:
        raise VieNeuVoiceProfileError("Reference phải dài từ 3 đến 8 giây.")
    samples = np.frombuffer(frames, dtype="<i2")
    if not len(samples):
        raise VieNeuVoiceProfileError("Reference audio rỗng.")
    clipping_ratio = float(np.mean(np.abs(samples.astype(np.int32)) >= 32760))
    if clipping_ratio > 0.001:
        raise VieNeuVoiceProfileError("Reference bị clipping quá mức cho phép.")
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "duration_seconds": round(duration, 3),
        "clipping_ratio": round(clipping_ratio, 8),
    }


def normalize_reference(
    source_path: Path,
    destination: Path,
    *,
    start_seconds: float,
    end_seconds: float,
) -> dict:
    source_path = Path(source_path)
    if source_path.suffix.casefold() not in SUPPORTED_SOURCE_SUFFIXES:
        raise VieNeuVoiceProfileError("Định dạng audio reference không được hỗ trợ.")
    if not source_path.is_file() or source_path.stat().st_size <= 0:
        raise VieNeuVoiceProfileError("File audio reference không tồn tại hoặc rỗng.")
    if source_path.stat().st_size > MAX_SOURCE_BYTES:
        raise VieNeuVoiceProfileError("File audio reference vượt giới hạn 100 MB.")
    start = max(0.0, float(start_seconds or 0.0))
    end = float(end_seconds or 0.0)
    if end <= start or end - start < MIN_REFERENCE_SECONDS or end - start > MAX_REFERENCE_SECONDS:
        raise VieNeuVoiceProfileError("Đoạn reference phải dài từ 3 đến 8 giây.")
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        imageio_ffmpeg.get_ffmpeg_exe(),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{start:.3f}",
        "-t",
        f"{end - start:.3f}",
        "-i",
        str(source_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "48000",
        "-c:a",
        "pcm_s16le",
        str(destination),
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise VieNeuVoiceProfileError("Không thể chuẩn hóa reference audio.") from exc
    if result.returncode != 0 or not destination.is_file():
        detail = security_logging.redact_sensitive(result.stderr or "")
        raise VieNeuVoiceProfileError(
            f"Không thể giải mã reference audio. {str(detail)[:240]}"
        )
    return _wav_metrics(destination)


def _install_revision(
    *,
    voice_id: str,
    revision: int,
    normalized_reference: Path,
    transcript: str,
    name: str,
    source_label: str,
    rights_note: str,
    source_sha256: str,
) -> dict:
    normalized_voice_id = _voice_id(voice_id)
    revision = max(1, int(revision))
    destination = revision_directory(normalized_voice_id, revision)
    if destination.exists():
        raise VieNeuVoiceProfileError("Revision VieNeu đã tồn tại và không thể ghi đè.")
    metrics = _wav_metrics(normalized_reference)
    revision_root = destination.parent
    revision_root.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{revision}-", dir=revision_root))
    try:
        reference_path = temporary / "reference.wav"
        shutil.copy2(normalized_reference, reference_path)
        transcript_path = temporary / "transcript.txt"
        transcript_path.write_text(str(transcript or "").strip(), encoding="utf-8")
        metadata = {
            "schema_version": 1,
            "provider": vieneu_tts.SUPPORTED_PROVIDER,
            "voice_id": normalized_voice_id,
            "name": str(name or normalized_voice_id).strip(),
            "revision": revision,
            "sample_rate": 48000,
            "channels": 1,
            "duration_seconds": metrics["duration_seconds"],
            "clipping_ratio": metrics["clipping_ratio"],
            "source_label": str(source_label or "").strip(),
            "rights_note": str(rights_note or "").strip(),
            "source_sha256": str(source_sha256 or "").strip().casefold(),
            "denoise_reference": False,
        }
        _write_json_atomic(temporary / "metadata.json", metadata)
        files = {
            filename: vieneu_tts.file_sha256(temporary / filename)
            for filename in ("reference.wav", "transcript.txt", "metadata.json")
        }
        _write_json_atomic(
            temporary / "checksums.json",
            {"schema_version": 1, "files": files},
        )
        os.replace(temporary, destination)
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return revision_status(normalized_voice_id, revision)


def create_revision(
    *,
    voice_id: str,
    revision: int,
    source_path: Path,
    start_seconds: float,
    end_seconds: float,
    transcript: str = "",
    name: str = "",
    source_label: str = "",
    rights_note: str = "",
) -> dict:
    source_path = Path(source_path)
    source_hash = vieneu_tts.file_sha256(source_path)
    with _PROFILE_LOCK:
        staging_root = voice_directory(voice_id) / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="reference-", dir=staging_root))
        try:
            normalized = temporary / "reference.wav"
            normalize_reference(
                source_path,
                normalized,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
            )
            return _install_revision(
                voice_id=voice_id,
                revision=revision,
                normalized_reference=normalized,
                transcript=transcript,
                name=name,
                source_label=source_label,
                rights_note=rights_note,
                source_sha256=source_hash,
            )
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


def revision_status(
    voice_id: str,
    revision: int,
    *,
    verify_runtime: bool = False,
) -> dict:
    directory = revision_directory(voice_id, revision)
    result = {
        "available": False,
        "voice_id": _voice_id(voice_id),
        "revision": max(1, int(revision)),
        "detail": "Voice profile chưa sẵn sàng.",
    }
    try:
        metadata = _read_json(directory / "metadata.json")
        checksums = _read_json(directory / "checksums.json")
        expected = checksums.get("files") or {}
        for filename in ("reference.wav", "transcript.txt", "metadata.json"):
            path = directory / filename
            expected_hash = str(expected.get(filename) or "").casefold()
            if not path.is_file() or not expected_hash:
                raise VieNeuVoiceProfileError("Voice profile thiếu file hoặc checksum.")
            if vieneu_tts.file_sha256(path) != expected_hash:
                raise VieNeuVoiceProfileError("Voice profile không khớp SHA-256.")
        metrics = _wav_metrics(directory / "reference.wav")
        if str(metadata.get("voice_id") or "") != result["voice_id"]:
            raise VieNeuVoiceProfileError("Voice profile không khớp Voice ID.")
        if int(metadata.get("revision") or 0) != result["revision"]:
            raise VieNeuVoiceProfileError("Voice profile không khớp revision.")
        runtime = vieneu_tts.runtime_status() if verify_runtime else None
        result.update(
            {
                "available": bool(not runtime or runtime.get("available")),
                "reference_ready": True,
                "detail": (
                    runtime.get("detail")
                    if runtime and not runtime.get("available")
                    else "Voice profile VieNeu sẵn sàng."
                ),
                "name": str(metadata.get("name") or result["voice_id"]),
                "duration_seconds": metrics["duration_seconds"],
                "reference_sha256": str(expected["reference.wav"]),
                "transcript_sha256": str(expected["transcript.txt"]),
                "source_label": str(metadata.get("source_label") or ""),
                "rights_note": str(metadata.get("rights_note") or ""),
                "runtime": runtime,
            }
        )
    except (OSError, ValueError, VieNeuVoiceProfileError) as exc:
        result["detail"] = str(security_logging.redact_sensitive(exc))
    return result


def reference_path(voice_id: str, revision: int) -> Path:
    status = revision_status(voice_id, revision, verify_runtime=False)
    if not status.get("available"):
        raise VieNeuVoiceProfileError(str(status.get("detail") or "Voice profile lỗi."))
    return revision_directory(voice_id, revision) / "reference.wav"


def list_revisions(voice_id: str) -> list[dict]:
    root = voice_directory(voice_id) / "revisions"
    if not root.is_dir():
        return []
    revisions = []
    for candidate in root.iterdir():
        if candidate.is_dir() and candidate.name.isdigit():
            revisions.append(revision_status(voice_id, int(candidate.name)))
    return sorted(revisions, key=lambda value: int(value["revision"]), reverse=True)


def public_revision(status: dict) -> dict:
    """Return only revision metadata safe for API responses."""

    return {
        key: status.get(key)
        for key in (
            "available",
            "voice_id",
            "revision",
            "detail",
            "name",
            "duration_seconds",
            "reference_ready",
            "reference_sha256",
        )
        if key in status
    }


def next_revision(voice_id: str) -> int:
    revisions = [int(item["revision"]) for item in list_revisions(voice_id)]
    return max(revisions, default=0) + 1


def build_voice_snapshot_fields(*, voice_id: str, revision: int) -> dict:
    profile = revision_status(voice_id, revision, verify_runtime=True)
    if not profile.get("available"):
        raise VieNeuVoiceProfileError(str(profile.get("detail") or "Voice profile lỗi."))
    runtime = profile.get("runtime") or {}
    return {
        "profile": {
            "voice_id": profile["voice_id"],
            "revision": profile["revision"],
            "reference_sha256": profile["reference_sha256"],
            "duration_seconds": profile["duration_seconds"],
        },
        "runtime_revision": str(runtime.get("runtime_revision") or ""),
        "model_revision": runtime.get("model_revision") or {},
        "runtime_lock_sha256": str(runtime.get("lock_sha256") or ""),
    }


def snapshot_revision(
    *,
    voice_snapshot: dict,
    item_dir: Path,
) -> dict:
    """Copy one immutable global voice revision into a pipeline item."""

    voice_id = _voice_id(voice_snapshot.get("voice_id"))
    revision = int(voice_snapshot.get("voice_revision") or 0)
    if revision <= 0:
        raise VieNeuVoiceProfileError("Voice snapshot VieNeu thiếu revision.")
    profile = voice_snapshot.get("profile") or {}
    expected_reference_hash = str(profile.get("reference_sha256") or "").casefold()
    status = revision_status(voice_id, revision, verify_runtime=True)
    if not status.get("available"):
        raise VieNeuVoiceProfileError(str(status.get("detail") or "Voice profile lỗi."))
    if expected_reference_hash != str(status.get("reference_sha256") or "").casefold():
        raise VieNeuVoiceProfileError("Voice snapshot không khớp reference revision hiện hành.")

    source = revision_directory(voice_id, revision)
    destination = Path(item_dir) / "references" / "voice_profile"
    temporary = destination.with_name(destination.name + ".tmp")
    copied_hash = ""
    with _PROFILE_LOCK:
        if destination.exists():
            existing_reference = destination / "reference.wav"
            if not existing_reference.is_file():
                raise VieNeuVoiceProfileError("Snapshot voice profile hiện có không toàn vẹn.")
            copied_hash = vieneu_tts.file_sha256(existing_reference)
            if copied_hash != expected_reference_hash:
                raise VieNeuVoiceProfileError("Snapshot voice profile hiện có sai SHA-256.")
        else:
            shutil.rmtree(temporary, ignore_errors=True)
            temporary.parent.mkdir(parents=True, exist_ok=True)
            try:
                temporary.mkdir(parents=True, exist_ok=False)
                for filename in (
                    "reference.wav",
                    "transcript.txt",
                    "metadata.json",
                    "checksums.json",
                ):
                    source_path = source / filename
                    if not source_path.is_file():
                        raise VieNeuVoiceProfileError("Voice profile thiếu artifact bắt buộc.")
                    shutil.copy2(source_path, temporary / filename)
                copied_hash = vieneu_tts.file_sha256(temporary / "reference.wav")
                if copied_hash != expected_reference_hash:
                    raise VieNeuVoiceProfileError("Snapshot reference không khớp SHA-256.")
                os.replace(temporary, destination)
            except Exception:
                shutil.rmtree(temporary, ignore_errors=True)
                raise

    runtime = status.get("runtime") or {}
    return {
        "provider": vieneu_tts.SUPPORTED_PROVIDER,
        "profile_id": voice_id,
        "profile_version": revision,
        "voice_id": voice_id,
        "revision": revision,
        "runtime_revision": str(runtime.get("runtime_revision") or ""),
        "model_revision": runtime.get("model_revision") or {},
        "runtime_lock_sha256": str(runtime.get("lock_sha256") or ""),
        "reference": {
            "path": (destination / "reference.wav").relative_to(item_dir).as_posix(),
            "sha256": copied_hash,
        },
        "transcript": {
            "path": (destination / "transcript.txt").relative_to(item_dir).as_posix(),
            "sha256": str(status.get("transcript_sha256") or ""),
        },
    }


def import_legacy_bac_ho_profiles() -> list[dict]:
    """Copy legacy Short Video revisions into the global managed profile."""

    imported: list[dict] = []
    voice_id = "bac-ho-ngam-tho"
    with _PROFILE_LOCK:
        for revision in (1, 2, 3):
            destination = revision_directory(voice_id, revision)
            if destination.exists():
                imported.append(revision_status(voice_id, revision))
                continue
            source = SHORT_VIDEO_AUDIO_PROFILES_DIR / "bac-ho-ngam-tho" / f"v{revision}"
            reference = source / "reference.wav"
            if not reference.is_file():
                continue
            metadata = _read_json(source / "reference.json")
            transcript_path = source / "source_transcript.srt"
            source_hash = vieneu_tts.file_sha256(reference)
            status = _install_revision(
                voice_id=voice_id,
                revision=revision,
                normalized_reference=reference,
                transcript=(
                    transcript_path.read_text(encoding="utf-8")
                    if transcript_path.is_file()
                    else str(metadata.get("transcript") or "")
                ),
                name="Bác Hồ ngâm thơ",
                source_label=str(metadata.get("source_label") or "Legacy Short Video profile"),
                rights_note=str(metadata.get("rights_note") or ""),
                source_sha256=source_hash,
            )
            if vieneu_tts.file_sha256(destination / "reference.wav") != source_hash:
                shutil.rmtree(destination, ignore_errors=True)
                raise VieNeuVoiceProfileError("Import legacy không giữ nguyên checksum reference.")
            imported.append(status)
    return imported
