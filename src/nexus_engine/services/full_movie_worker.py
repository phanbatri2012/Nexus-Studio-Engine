"""Explicitly-started coordinator for local full-movie merge jobs."""

from __future__ import annotations

import threading

from nexus_engine.services import database as db, full_movie_pipeline, process_registry
from nexus_engine.services.production_coordinator import ProductionCoordinator


JOB_TYPE = full_movie_pipeline.JOB_TYPE
_LOCK = threading.Lock()
_COORDINATOR: ProductionCoordinator | None = None


def _coordinator() -> ProductionCoordinator:
    global _COORDINATOR
    with _LOCK:
        if _COORDINATOR is None:
            _COORDINATOR = ProductionCoordinator(
                {JOB_TYPE: full_movie_pipeline.execute},
                error_handler=full_movie_pipeline.handle_error,
            )
        return _COORDINATOR


def kick() -> None:
    _coordinator().wake()


def stop() -> None:
    with _LOCK:
        coordinator = _COORDINATOR
    if coordinator is not None:
        current_job_id = str(coordinator.status().get("current_job_id") or "")
        if current_job_id:
            current = db.get_system_job(current_job_id)
            if current and current.get("status") == "running":
                db.update_system_job(
                    current_job_id,
                    status="paused",
                    progress=(
                        "Nexus đang dừng; checkpoint ghép phim đã được giữ để "
                        "tiếp tục thủ công."
                    ),
                    error="",
                    cancel_requested=0,
                    finished_at="",
                )
            process_registry.kill_job_processes(current_job_id)
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


def pause(job_id: str) -> dict | None:
    job = db.get_system_job(job_id)
    if not job:
        return None
    if job.get("status") == "running":
        updated = db.update_system_job(
            job_id,
            status="paused",
            progress="Đã tạm dừng; checkpoint phần đã hoàn tất được giữ lại",
            error="",
            cancel_requested=0,
            finished_at="",
        )
        process_registry.kill_job_processes(job_id)
        return updated
    return db.pause_system_job(job_id)


def cancel_running(job_id: str) -> dict | None:
    updated = db.request_cancel_system_job(job_id)
    if updated and updated.get("cancel_requested"):
        process_registry.kill_job_processes(job_id)
    return updated


def pause_after_restart() -> int:
    db.recover_interrupted_system_jobs(JOB_TYPE)
    jobs = db.list_system_jobs(
        limit=None,
        job_type=JOB_TYPE,
        statuses={"queued", "retry_wait"},
    )
    paused = 0
    for job in jobs:
        result = dict(job.get("result") or {})
        result["attention_required"] = "full_movie_resume_after_restart"
        updated = db.update_system_job(
            str(job["id"]),
            status="paused",
            progress=(
                "Ghép phim đã giữ checkpoint sau khi Nexus khởi động lại; "
                "bấm Tiếp tục để chạy tiếp."
            ),
            result_json=result,
            error="",
            next_retry_at="",
            cancel_requested=0,
            finished_at="",
        )
        if updated:
            paused += 1
    return paused
