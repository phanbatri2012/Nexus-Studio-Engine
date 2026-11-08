"""On-demand coordinator for global VieNeu TTS jobs."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from copy import deepcopy
from pathlib import Path

from nexus_engine.paths import VIENEU_TTS_JOBS_DIR
from nexus_engine.services import database as db
from nexus_engine.services import security_logging, vieneu_tts
from nexus_engine.services.production_coordinator import ProductionCoordinator


JOB_TYPE = "vieneu_tts_generation"
TASK_PREFIX = "vieneu:"
MAX_TEXT_CHARACTERS = 10_000
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


def validate_text(text: str) -> str:
    normalized = str(text or "").strip()
    if not normalized:
        raise ValueError("Nội dung tạo audio không được để trống.")
    if len(normalized) > MAX_TEXT_CHARACTERS:
        raise ValueError("Nội dung tạo audio vượt giới hạn 10.000 ký tự.")
    return normalized


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


def _execute(job: dict) -> None:
    job_id = raw_job_id(str(job["id"]))
    payload = job.get("payload") or {}
    text = str(payload.get("text") or "").strip()
    request_hash = str(payload.get("request_hash") or "").strip()
    snapshot = deepcopy(job.get("voice_snapshot") or payload.get("voice_snapshot") or {})
    if not request_hash or not snapshot:
        raise RuntimeError("Job VieNeu thiếu request hash hoặc voice snapshot.")
    text = validate_text(text)
    voice_id = str(snapshot.get("voice_id") or "")
    provider_voice_id = str(snapshot.get("provider_voice_id") or "").strip()
    if not provider_voice_id:
        raise RuntimeError("Job VieNeu thiếu provider_voice_id.")
    remote_voice = vieneu_tts.api_voice(provider_voice_id)
    expected_revision = int(snapshot.get("provider_voice_revision") or 0)
    remote_revision = int(remote_voice.get("revision") or 1)
    if expected_revision and expected_revision != remote_revision:
        raise RuntimeError("Revision giọng trên VieNeu đã đổi so với snapshot của job.")

    work_dir = _job_directory(job_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    result = deepcopy(job.get("result") or {})
    _assert_not_canceled(job_id)
    output = work_dir / "audio.wav"
    db.update_system_job(
        job_id,
        progress="Đang chờ VieNeu tạo audio",
        resume_from_step="provider_request",
    )
    provider_receipt = vieneu_tts.generate_api_audio(
        text=text,
        provider_voice_id=provider_voice_id,
        seed=_seed(request_hash, 0),
        output_path=output,
        timeout_seconds=1800,
        cancel_check=lambda: _assert_not_canceled(job_id),
    )
    _assert_not_canceled(job_id)
    receipt = {
        "provider": vieneu_tts.SUPPORTED_PROVIDER,
        "request_hash": request_hash,
        "voice_id": voice_id,
        "provider_voice_id": provider_voice_id,
        "provider_voice_revision": remote_revision,
        "provider_request_id": provider_receipt.get("provider_request_id") or "",
        "audio_sha256": vieneu_tts.file_sha256(output),
        "duration_seconds": float(provider_receipt.get("duration_seconds") or 0),
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
    text = validate_text(text)
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
