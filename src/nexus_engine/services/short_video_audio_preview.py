"""On-demand VieNeu audio preview jobs for Short Video projects."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from nexus_engine.paths import SHORT_VIDEO_DIR
from nexus_engine.services import (
    database as db,
    security_logging,
    vieneu_tts,
)


class ShortVideoAudioPreviewError(RuntimeError):
    pass


def _preview_directory(preview_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f-]{32,36}", str(preview_id or "").casefold()):
        raise ShortVideoAudioPreviewError("Preview ID không hợp lệ.")
    root = (SHORT_VIDEO_DIR / "_audio_previews").resolve()
    target = (root / preview_id).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ShortVideoAudioPreviewError("Namespace preview không hợp lệ.") from exc
    target.mkdir(parents=True, exist_ok=True)
    return target


def audio_path(preview: dict) -> Path:
    filename = str(preview.get("audio_filename") or "")
    if filename != "preview.wav":
        raise FileNotFoundError("Bản nghe thử chưa sẵn sàng.")
    path = (_preview_directory(str(preview["id"])) / filename).resolve()
    if not path.is_file():
        raise FileNotFoundError("Artifact nghe thử không còn tồn tại.")
    return path


def _check_canceled(job_id: str) -> None:
    latest = db.get_system_job(job_id)
    if latest and latest.get("cancel_requested"):
        raise ShortVideoAudioPreviewError("Bản nghe thử đã bị hủy.")


def execute(job: dict) -> None:
    payload = job.get("payload") or {}
    preview_id = str(payload.get("preview_id") or "")
    preview = db.get_short_video_audio_preview(preview_id)
    if preview is None:
        raise ShortVideoAudioPreviewError("Không tìm thấy bản nghe thử.")
    directory = _preview_directory(preview_id)
    final_path = directory / "preview.wav"
    if preview.get("status") == "completed" and final_path.is_file():
        db.update_system_job(
            str(job["id"]),
            status="completed",
            progress="Bản nghe thử đã sẵn sàng",
            result_json={"preview_id": preview_id},
            error="",
            finished_at=db.utc_now(),
        )
        return

    profile = payload.get("audio_profile") or {}
    voice_snapshot = job.get("voice_snapshot") or payload.get("voice_snapshot") or {}
    provider_voice_id = str(
        voice_snapshot.get("provider_voice_id")
        or profile.get("provider_voice_id")
        or ""
    ).strip()
    if not provider_voice_id:
        raise ShortVideoAudioPreviewError(
            "Preview legacy chưa có voice ID trên VieNeu; hãy tạo preview mới."
        )
    poem_excerpt = str(preview.get("poem_excerpt") or "").strip()
    lines = [line for line in poem_excerpt.splitlines() if line.strip()]
    if not lines or len(lines) > 4:
        raise ShortVideoAudioPreviewError("Preview phải có từ một đến bốn dòng thơ.")
    db.update_short_video_audio_preview(preview_id, status="processing", error="")
    db.update_system_job(
        str(job["id"]),
        progress="Đang tạo bản nghe thử VieNeu",
        resume_from_step="audio_preview",
    )
    seed = int.from_bytes(
        hashlib.sha256(f"{preview_id}:{provider_voice_id}".encode("utf-8")).digest()[:4],
        "big",
    )
    with vieneu_tts.VieNeuGenerationSession(
        job_id=str(job["id"]),
        provider_voice_id=provider_voice_id,
        work_dir=directory,
        cancel_check=lambda: _check_canceled(str(job["id"])),
    ) as session:
        receipt = session.generate(
            text=poem_excerpt,
            seed=seed,
            output_path=final_path,
        )
    duration = float(receipt.get("duration_seconds") or 0)
    if duration <= 0:
        raise ShortVideoAudioPreviewError("VieNeu không trả duration hợp lệ cho preview.")
    db.update_short_video_audio_preview(
        preview_id,
        status="completed",
        audio_filename="preview.wav",
        duration_seconds=round(duration, 3),
        error="",
    )
    db.update_system_job(
        str(job["id"]),
        status="completed",
        progress="Bản nghe thử VieNeu đã sẵn sàng",
        result_json={"preview_id": preview_id},
        error="",
        cancel_requested=0,
        finished_at=db.utc_now(),
    )


def handle_error(job: dict, exc: Exception) -> None:
    preview_id = str((job.get("payload") or {}).get("preview_id") or "")
    safe_error = security_logging.safe_user_error(
        "short_video_audio_preview",
        exc,
        "Không thể tạo bản nghe thử VieNeu.",
    )
    latest = db.get_system_job(str(job.get("id") or "")) or {}
    canceled = bool(latest.get("cancel_requested"))
    status = "canceled" if canceled else "failed"
    if preview_id:
        db.update_short_video_audio_preview(
            preview_id,
            status=status,
            error=safe_error,
        )
    db.update_system_job(
        str(job.get("id") or ""),
        status=status,
        progress="Đã hủy bản nghe thử" if canceled else "Tạo bản nghe thử thất bại",
        error=safe_error,
        finished_at=db.utc_now(),
    )
