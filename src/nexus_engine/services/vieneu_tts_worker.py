"""On-demand coordinator for global VieNeu TTS jobs."""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
import wave
from copy import deepcopy
from pathlib import Path

from nexus_engine.paths import VIENEU_TTS_JOBS_DIR
from nexus_engine.services import database as db
from nexus_engine.services import process_registry, security_logging, vieneu_tts
from nexus_engine.services import vieneu_voice_profiles
from nexus_engine.services.production_coordinator import ProductionCoordinator


JOB_TYPE = "vieneu_tts_generation"
TASK_PREFIX = "vieneu:"
MAX_TEXT_CHARACTERS = 10_000
MAX_CHUNK_CHARACTERS = 220
_JOB_NAMESPACE = uuid.UUID("6e8b76ca-39ca-47c2-8b49-0d7e0b5d4a8a")
_LOCK = threading.Lock()
_COORDINATOR: ProductionCoordinator | None = None


class VieNeuJobCanceled(RuntimeError):
    pass


def raw_job_id(task_id: str) -> str:
    value = str(task_id or "").strip()
    if value.startswith(TASK_PREFIX):
        value = value[len(TASK_PREFIX) :]
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Mã task VieNeu không hợp lệ.") from exc


def task_id(job_id: str) -> str:
    return f"{TASK_PREFIX}{raw_job_id(job_id)}"


def _split_long_sentence(sentence: str) -> list[str]:
    chunks: list[str] = []
    remaining = sentence.strip()
    while len(remaining) > MAX_CHUNK_CHARACTERS:
        split_at = max(
            remaining.rfind(mark, 0, MAX_CHUNK_CHARACTERS + 1)
            for mark in (", ", "; ", ": ", " ")
        )
        if split_at < MAX_CHUNK_CHARACTERS // 2:
            split_at = MAX_CHUNK_CHARACTERS
        chunks.append(remaining[:split_at].strip())
        remaining = remaining[split_at:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks


def split_text(text: str) -> list[str]:
    normalized = re.sub(r"[\t ]+", " ", str(text or "").replace("\r", "")).strip()
    if not normalized:
        raise ValueError("Nội dung tạo audio không được để trống.")
    if len(normalized) > MAX_TEXT_CHARACTERS:
        raise ValueError("Nội dung tạo audio vượt giới hạn 10.000 ký tự.")
    sentences = re.split(r"(?<=[.!?…])\s+|\n+", normalized)
    chunks: list[str] = []
    pending = ""
    for raw_sentence in sentences:
        sentence = raw_sentence.strip()
        if not sentence:
            continue
        for part in _split_long_sentence(sentence):
            candidate = f"{pending} {part}".strip() if pending else part
            if len(candidate) <= MAX_CHUNK_CHARACTERS:
                pending = candidate
            else:
                if pending:
                    chunks.append(pending)
                pending = part
    if pending:
        chunks.append(pending)
    return chunks


def _seed(request_hash: str, index: int) -> int:
    value = f"{request_hash}:{index}"
    return int.from_bytes(hashlib.sha256(value.encode("utf-8")).digest()[:4], "big")


def _job_directory(job_id: str) -> Path:
    return VIENEU_TTS_JOBS_DIR / raw_job_id(job_id)


def _assert_not_canceled(job_id: str) -> dict:
    job = db.get_system_job(raw_job_id(job_id))
    if not job or job.get("cancel_requested") or job.get("status") == "canceled":
        raise VieNeuJobCanceled("Job VieNeu đã bị hủy.")
    return job


def _read_chunk(path: Path) -> tuple[bytes, int, int, int]:
    try:
        with wave.open(str(path), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            sample_rate = source.getframerate()
            frames = source.readframes(source.getnframes())
    except (OSError, wave.Error) as exc:
        raise RuntimeError("Chunk VieNeu không phải WAV hợp lệ.") from exc
    if (channels, sample_width, sample_rate) != (1, 2, 48000):
        raise RuntimeError("Chunk VieNeu phải là mono PCM16 48 kHz.")
    return frames, channels, sample_width, sample_rate


def _pause_milliseconds(text: str) -> int:
    stripped = str(text or "").rstrip()
    if stripped.endswith((".", "!", "?", "…")):
        return 350
    if stripped.endswith((",", ";", ":")):
        return 180
    return 120


def _assemble(chunks: list[Path], texts: list[str], destination: Path) -> float:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp.wav")
    total_frames = 0
    with wave.open(str(temporary), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        for index, chunk_path in enumerate(chunks):
            frames, _channels, _width, _rate = _read_chunk(chunk_path)
            output.writeframes(frames)
            total_frames += len(frames) // 2
            if index < len(chunks) - 1:
                pause_frames = int(48000 * _pause_milliseconds(texts[index]) / 1000)
                output.writeframes(b"\x00\x00" * pause_frames)
                total_frames += pause_frames
    os.replace(temporary, destination)
    return total_frames / 48000.0


def _execute(job: dict) -> None:
    job_id = raw_job_id(str(job["id"]))
    payload = job.get("payload") or {}
    text = str(payload.get("text") or "").strip()
    request_hash = str(payload.get("request_hash") or "").strip()
    snapshot = deepcopy(job.get("voice_snapshot") or payload.get("voice_snapshot") or {})
    if not request_hash or not snapshot:
        raise RuntimeError("Job VieNeu thiếu request hash hoặc voice snapshot.")
    chunks = split_text(text)
    profile = snapshot.get("profile") or {}
    voice_id = str(profile.get("voice_id") or snapshot.get("voice_id") or "")
    revision = int(profile.get("revision") or snapshot.get("voice_revision") or 0)
    reference = vieneu_voice_profiles.reference_path(voice_id, revision)
    if vieneu_tts.file_sha256(reference) != str(profile.get("reference_sha256") or ""):
        raise RuntimeError("Reference VieNeu không khớp snapshot của job.")
    runtime = vieneu_tts.runtime_status()
    if not runtime.get("available"):
        raise RuntimeError(str(runtime.get("detail") or "Runtime VieNeu chưa sẵn sàng."))
    expected_lock = str(snapshot.get("runtime_lock_sha256") or "")
    if expected_lock and expected_lock != str(runtime.get("lock_sha256") or ""):
        raise RuntimeError("Runtime VieNeu đã thay đổi so với snapshot của job.")

    work_dir = _job_directory(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    result = deepcopy(job.get("result") or {})
    checkpoints = result.get("chunks") if isinstance(result.get("chunks"), list) else []
    completed_by_index = {
        int(item.get("index")): item
        for item in checkpoints
        if isinstance(item, dict) and str(item.get("index", "")).isdigit()
    }
    output_paths: list[Path] = []
    with vieneu_tts.VieNeuGenerationSession(
        job_id=job_id,
        reference_path=reference,
        work_dir=work_dir,
        timeout_seconds=900,
    ) as session:
        for index, chunk_text in enumerate(chunks):
            _assert_not_canceled(job_id)
            output_path = work_dir / f"chunk_{index:04d}.wav"
            checkpoint = completed_by_index.get(index)
            if (
                checkpoint
                and output_path.is_file()
                and vieneu_tts.file_sha256(output_path) == checkpoint.get("sha256")
                and checkpoint.get("text_sha256")
                == hashlib.sha256(chunk_text.encode("utf-8")).hexdigest()
            ):
                output_paths.append(output_path)
                continue
            receipt = session.generate(
                text=chunk_text,
                seed=_seed(request_hash, index),
                output_path=output_path,
            )
            checkpoint = {
                "index": index,
                "filename": output_path.name,
                "sha256": vieneu_tts.file_sha256(output_path),
                "text_sha256": hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                "request_hash": receipt.get("request_hash") or "",
            }
            completed_by_index[index] = checkpoint
            db.update_system_job(
                job_id,
                progress=f"Đã tạo {index + 1}/{len(chunks)} đoạn audio",
                result_json={
                    **result,
                    "chunks": [completed_by_index[key] for key in sorted(completed_by_index)],
                },
                resume_from_step=f"chunk_{index + 1}",
            )
            output_paths.append(output_path)

    _assert_not_canceled(job_id)
    output = work_dir / "audio.wav"
    duration = _assemble(output_paths, chunks, output)
    receipt = {
        "provider": vieneu_tts.SUPPORTED_PROVIDER,
        "request_hash": request_hash,
        "voice_id": voice_id,
        "voice_revision": revision,
        "reference_sha256": profile.get("reference_sha256") or "",
        "runtime_revision": snapshot.get("runtime_revision") or "",
        "model_revision": snapshot.get("model_revision") or {},
        "chunk_count": len(chunks),
        "audio_sha256": vieneu_tts.file_sha256(output),
        "duration_seconds": round(duration, 3),
    }
    receipt_path = work_dir / "receipt.json"
    temporary_receipt = receipt_path.with_suffix(".tmp")
    temporary_receipt.write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary_receipt, receipt_path)
    db.update_system_job(
        job_id,
        status="done",
        progress="Đã tạo audio VieNeu",
        result_json={
            **result,
            "chunks": [completed_by_index[key] for key in sorted(completed_by_index)],
            "audio_filename": "audio.wav",
            "audio_sha256": receipt["audio_sha256"],
            "duration_seconds": receipt["duration_seconds"],
            "request_hash": request_hash,
        },
        error="",
        resume_from_step="completed",
        finished_at=db.utc_now(),
        cancel_requested=0,
    )


def _handle_error(job: dict, exc: Exception) -> None:
    job_id = raw_job_id(str(job.get("id") or ""))
    current = db.get_system_job(job_id)
    canceled = isinstance(exc, VieNeuJobCanceled) or bool(
        current and (current.get("cancel_requested") or current.get("status") == "canceled")
    )
    db.update_system_job(
        job_id,
        status="canceled" if canceled else "failed",
        progress="Đã hủy tạo audio VieNeu" if canceled else "Tạo audio VieNeu thất bại",
        error="" if canceled else str(security_logging.redact_sensitive(exc)),
        cancel_requested=0,
        finished_at=db.utc_now(),
    )


def _coordinator() -> ProductionCoordinator:
    global _COORDINATOR
    with _LOCK:
        if _COORDINATOR is None:
            _COORDINATOR = ProductionCoordinator(
                {JOB_TYPE: _execute},
                error_handler=_handle_error,
            )
        return _COORDINATOR


def kick() -> None:
    _coordinator().wake()


def stop() -> None:
    with _LOCK:
        coordinator = _COORDINATOR
    if coordinator is not None:
        coordinator.stop()


def status() -> dict:
    with _LOCK:
        coordinator = _COORDINATOR
    if coordinator is None:
        return {
            "running": False,
            "ready": False,
            "current_job_id": "",
            "registered_job_types": [JOB_TYPE],
        }
    return coordinator.status()


def enqueue(
    *,
    text: str,
    voice_snapshot: dict,
    request_hash: str,
    title: str = "",
    client_request_id: str = "",
) -> dict:
    split_text(text)
    canonical_key = (
        f"client:{client_request_id}"
        if client_request_id
        else f"request:{request_hash}"
    )
    job_id = str(uuid.uuid5(_JOB_NAMESPACE, canonical_key))
    existing = db.get_system_job(job_id)
    if existing is not None:
        existing_hash = str((existing.get("payload") or {}).get("request_hash") or "")
        if existing_hash != request_hash:
            raise ValueError("client_request_id đã được dùng với nội dung khác.")
        return existing
    snapshot_json = json.dumps(voice_snapshot, ensure_ascii=False)
    try:
        job = db.create_system_job(
            job_id=job_id,
            job_type=JOB_TYPE,
            title=str(title or "Tạo audio VieNeu").strip()[:200],
            payload={
                "text": text,
                "request_hash": request_hash,
                "client_request_id": str(client_request_id or ""),
                "voice_snapshot": voice_snapshot,
            },
            voice_id=str(voice_snapshot.get("voice_id") or ""),
            voice_name=str(voice_snapshot.get("voice_name") or ""),
            tts_provider_id=vieneu_tts.SUPPORTED_PROVIDER,
            voice_revision=int(voice_snapshot.get("voice_revision") or 1),
            voice_snapshot_json=snapshot_json,
        )
    except Exception:
        existing = db.get_system_job(job_id)
        if existing is None:
            raise
        job = existing
    kick()
    return job


def cancel(task: str) -> dict:
    job_id = raw_job_id(task)
    job = db.request_cancel_system_job(job_id)
    if job is None:
        raise ValueError("Task VieNeu không tồn tại.")
    if job.get("status") == "running" or job.get("cancel_requested"):
        def force_after_grace() -> None:
            current = db.get_system_job(job_id)
            if current and current.get("status") == "running" and current.get("cancel_requested"):
                process_registry.kill_job_processes(job_id)

        timer = threading.Timer(5.0, force_after_grace)
        timer.daemon = True
        timer.start()
    return job


def recover(task: str) -> dict:
    job_id = raw_job_id(task)
    job = db.get_system_job(job_id)
    if job is None:
        raise ValueError("Task VieNeu không tồn tại.")
    if job["status"] == "paused":
        job = db.resume_system_job(job_id)
    elif job["status"] in {"failed", "error", "canceled"}:
        job = db.retry_system_job(job_id)
    if job and job.get("status") == "queued":
        kick()
    return job


def pause_after_restart() -> int:
    db.recover_interrupted_system_jobs(JOB_TYPE)
    jobs = db.list_system_jobs(
        limit=None,
        job_type=JOB_TYPE,
        statuses={"queued", "retry_wait"},
    )
    paused = 0
    for job in jobs:
        updated = db.update_system_job(
            str(job["id"]),
            status="paused",
            progress="VieNeu đã giữ checkpoint sau restart; bấm Tiếp tục để chạy tiếp.",
            next_retry_at="",
            cancel_requested=0,
        )
        if updated:
            paused += 1
    return paused


def audio_path(task: str) -> Path:
    job_id = raw_job_id(task)
    job = db.get_system_job(job_id)
    if not job or job.get("status") != "done":
        raise ValueError("Audio VieNeu chưa hoàn tất.")
    result = job.get("result") or {}
    if result.get("audio_filename") != "audio.wav":
        raise ValueError("Artifact VieNeu không hợp lệ.")
    path = _job_directory(job_id) / "audio.wav"
    if not path.is_file() or vieneu_tts.file_sha256(path) != result.get("audio_sha256"):
        raise ValueError("Artifact VieNeu thiếu hoặc sai checksum.")
    return path


def delete_job(task: str) -> dict:
    job_id = raw_job_id(task)
    job = db.get_system_job(job_id)
    if not job:
        raise ValueError("Task VieNeu không tồn tại.")
    if job.get("status") == "running":
        raise ValueError("Hãy hủy task VieNeu trước khi xóa.")
    deleted = db.delete_system_job(job_id)
    work_dir = _job_directory(job_id)
    if work_dir.is_dir():
        import shutil

        shutil.rmtree(work_dir, ignore_errors=True)
    return deleted or job
