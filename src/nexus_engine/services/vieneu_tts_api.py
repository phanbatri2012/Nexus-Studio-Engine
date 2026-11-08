"""VieNeu provider bridge and standalone generation API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from nexus_engine.services import database as db
from nexus_engine.services import security_logging, tts_registry, voice_config
from nexus_engine.services import vieneu_tts, vieneu_tts_worker


router = APIRouter(prefix="/api/tts", tags=["tts-vieneu"])
class TTSGenerationCreate(BaseModel):
    title: str = Field(default="", max_length=200)
    text: str = Field(min_length=1, max_length=vieneu_tts_worker.MAX_TEXT_CHARACTERS)
    voice_id: str = Field(min_length=1, max_length=128)
    client_request_id: str = Field(min_length=8, max_length=200)


def _error(exc: Exception, *, status_code: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail=str(security_logging.redact_sensitive(exc)),
    )


@router.get("/providers/vieneu_v3_turbo/profiles")
def list_vieneu_profiles():
    try:
        return {"items": vieneu_tts.api_voices()}
    except Exception as exc:
        raise _error(exc, status_code=503) from exc


@router.post("/providers/vieneu_v3_turbo/voices")
async def clone_vieneu_voice():
    raise HTTPException(
        status_code=410,
        detail="Hãy tạo và quản lý giọng trực tiếp trong VieNeu; Nexus chỉ sử dụng voice ID.",
    )


@router.post("/providers/vieneu_v3_turbo/import")
async def import_vieneu_folder():
    raise HTTPException(
        status_code=410,
        detail="Hãy import giọng trực tiếp trong VieNeu; Nexus không lưu reference giọng.",
    )


@router.post("/voices/{voice_id}/revisions")
async def create_vieneu_revision(voice_id: str):
    raise HTTPException(
        status_code=410,
        detail="Revision giọng được quản lý trực tiếp trong VieNeu.",
    )


@router.post("/voices/{voice_id}/revisions/{revision}/activate")
def activate_vieneu_revision(voice_id: str, revision: int):
    raise HTTPException(
        status_code=410,
        detail="Trạng thái giọng được quản lý trực tiếp trong VieNeu.",
    )


@router.get("/voices/{voice_id}/reference-audio")
def serve_vieneu_reference(
    voice_id: str,
    revision: int = Query(..., ge=1),
    download: bool = Query(False),
):
    raise HTTPException(
        status_code=410,
        detail="Reference giọng thuộc VieNeu và không được Nexus phục vụ.",
    )


def _public_generation(job: dict) -> dict:
    payload = job.get("payload") or {}
    result = job.get("result") or {}
    public_status = {
        "done": "completed",
        "running": "processing",
        "retry_wait": "queued",
        "error": "failed",
    }.get(str(job.get("status") or ""), str(job.get("status") or ""))
    return {
        "id": job["id"],
        "status": public_status,
        "title": job.get("title") or "",
        "text": str(payload.get("text") or ""),
        "character_count": len(str(payload.get("text") or "")),
        "voice_id": job.get("voice_id") or "",
        "voice_name": job.get("voice_name") or "",
        "voice_revision": job.get("voice_revision") or 1,
        "provider_id": job.get("tts_provider_id") or "",
        "progress": job.get("progress") or "",
        "duration_seconds": result.get("duration_seconds"),
        "audio_url": (
            f"/api/tts/generations/{job['id']}/audio"
            if job.get("status") == "done"
            else ""
        ),
        "error": str(security_logging.redact_sensitive(job.get("error") or "")),
        "created_at": job.get("created_at") or "",
        "updated_at": job.get("updated_at") or "",
    }


def _generation(job_id: str) -> dict:
    try:
        normalized = str(uuid.UUID(job_id))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(status_code=404, detail="Job tạo audio không tồn tại.") from exc
    job = db.get_system_job(normalized)
    if not job or job.get("job_type") != vieneu_tts_worker.JOB_TYPE:
        raise HTTPException(status_code=404, detail="Job tạo audio không tồn tại.")
    return job


@router.post("/generations")
def create_generation(payload: TTSGenerationCreate):
    try:
        voice, _provider = tts_registry.get_voice_context(payload.voice_id)
        if voice["provider_id"] != voice_config.VIENEU_PROVIDER_ID:
            raise ValueError("Công cụ Tạo audio hiện chỉ hỗ trợ giọng VieNeu.")
        snapshot = voice_config.build_voice_snapshot(voice)
        text = payload.text.strip()
        request_hash = tts_registry.get_request_hash(text, voice["id"], snapshot)
        job = vieneu_tts_worker.enqueue(
            text=text,
            voice_snapshot=snapshot,
            request_hash=request_hash,
            title=payload.title.strip() or f"Audio · {voice['name']}",
            client_request_id=payload.client_request_id,
        )
        return _public_generation(job)
    except ValueError as exc:
        status_code = 409 if "client_request_id" in str(exc) else 400
        raise _error(exc, status_code=status_code) from exc
    except Exception as exc:
        raise _error(exc, status_code=503) from exc


@router.get("/generations")
def list_generations(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    jobs = db.list_system_jobs(
        limit=limit,
        offset=offset,
        job_type=vieneu_tts_worker.JOB_TYPE,
    )
    return {"items": [_public_generation(job) for job in jobs], "limit": limit, "offset": offset}


@router.get("/generations/{job_id}")
def get_generation(job_id: str):
    return _public_generation(_generation(job_id))


@router.get("/generations/{job_id}/audio")
def serve_generation_audio(job_id: str, download: bool = Query(False)):
    job = _generation(job_id)
    try:
        path = vieneu_tts_worker.audio_path(job["id"])
    except Exception as exc:
        raise _error(exc, status_code=409) from exc
    return FileResponse(
        str(path),
        media_type="audio/wav",
        filename=f"vieneu-{job['id']}.wav" if download else None,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/generations/{job_id}/cancel")
def cancel_generation(job_id: str):
    job = _generation(job_id)
    return _public_generation(vieneu_tts_worker.cancel(job["id"]))


@router.post("/generations/{job_id}/retry")
def retry_generation(job_id: str):
    job = _generation(job_id)
    try:
        return _public_generation(vieneu_tts_worker.recover(job["id"]))
    except Exception as exc:
        raise _error(exc, status_code=409) from exc


@router.delete("/generations/{job_id}")
def delete_generation(job_id: str):
    job = _generation(job_id)
    try:
        vieneu_tts_worker.delete_job(job["id"])
        return {"success": True, "id": job["id"]}
    except Exception as exc:
        raise _error(exc, status_code=409) from exc
