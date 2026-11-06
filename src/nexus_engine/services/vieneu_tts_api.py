"""VieNeu voice management and standalone generation API."""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from nexus_engine.services import database as db
from nexus_engine.services import security_logging, tts_registry, voice_config
from nexus_engine.services import vieneu_tts, vieneu_tts_worker, vieneu_voice_profiles


router = APIRouter(prefix="/api/tts", tags=["tts-vieneu"])
_CONFIG_LOCK = voice_config.CONFIG_LOCK
_UPLOAD_CHUNK_BYTES = 1024 * 1024
_MAX_UPLOAD_FILES = 50


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


async def _save_upload(upload: UploadFile, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    try:
        with destination.open("wb") as output:
            for _chunk_index in range(102):
                chunk = await upload.read(_UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                total += len(chunk)
                if total > vieneu_voice_profiles.MAX_SOURCE_BYTES:
                    raise ValueError("File upload vượt giới hạn 100 MB.")
                output.write(chunk)
            else:
                raise ValueError("File upload vượt giới hạn đọc an toàn.")
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    if total <= 0:
        destination.unlink(missing_ok=True)
        raise ValueError("File upload rỗng.")


def _voice_by_id(config: dict, voice_id: str) -> dict:
    voice = next((item for item in config["voices"] if item["id"] == voice_id), None)
    if not voice:
        raise ValueError("Giọng đọc không tồn tại.")
    if voice["provider_id"] != voice_config.VIENEU_PROVIDER_ID:
        raise ValueError("Giọng đọc không thuộc VieNeu v3 Turbo.")
    return voice


def _new_voice_record(*, voice_id: str, name: str, revision: int) -> dict:
    return {
        "id": voice_id,
        "name": str(name or "").strip(),
        "provider_id": voice_config.VIENEU_PROVIDER_ID,
        "provider_voice_id": voice_id,
        "status": "draft",
        "revision": revision,
        "active_revision": None,
        "draft_revision": revision,
        "config": {
            "backend": "onnx",
            "precision": "fp32",
            "sample_rate": 48000,
            "use_ref_codes": True,
            "denoise_reference": False,
            "apply_watermark": True,
        },
        "casting_profile": voice_config.normalize_casting_profile(
            {
                "gender": "any",
                "age_group": "any",
                "languages": ["vi"],
                "roles": ["guest"],
                "styles": [],
                "usage_modes": ["single_narrator"],
            }
        ),
    }


def _public_voice(voice: dict) -> dict:
    revisions = [
        vieneu_voice_profiles.public_revision(item)
        for item in vieneu_voice_profiles.list_revisions(voice["id"])
    ]
    return {
        **voice,
        "revisions": revisions,
        "profile_ready": any(item.get("available") for item in revisions),
    }


def _register_new_voice(*, name: str, revision_builder) -> dict:
    clean_name = str(name or "").strip()
    if not clean_name:
        raise ValueError("Tên giọng VieNeu không được để trống.")
    voice_id = str(uuid.uuid4())
    revision = 1
    revision_builder(voice_id, revision)
    try:
        with _CONFIG_LOCK:
            config = voice_config.load_voice_config()
            record = _new_voice_record(
                voice_id=voice_id,
                name=clean_name,
                revision=revision,
            )
            config["voices"].append(record)
            saved = voice_config.save_voice_config(config)
            return _public_voice(
                next(item for item in saved["voices"] if item["id"] == voice_id)
            )
    except Exception:
        shutil.rmtree(vieneu_voice_profiles.voice_directory(voice_id), ignore_errors=True)
        raise


@router.get("/providers/vieneu_v3_turbo/profiles")
def list_vieneu_profiles():
    config = voice_config.load_voice_config()
    return {
        "items": [
            _public_voice(voice)
            for voice in config["voices"]
            if voice["provider_id"] == voice_config.VIENEU_PROVIDER_ID
        ]
    }


@router.post("/providers/vieneu_v3_turbo/voices")
async def clone_vieneu_voice(
    file: UploadFile = File(...),
    name: str = Form(..., min_length=1, max_length=120),
    start_seconds: float = Form(0.0, ge=0),
    end_seconds: float = Form(..., gt=0),
    transcript: str = Form("", max_length=4000),
    source_label: str = Form("", max_length=300),
    rights_note: str = Form("", max_length=1000),
):
    suffix = Path(file.filename or "reference.wav").suffix.casefold()
    if suffix not in vieneu_voice_profiles.SUPPORTED_SOURCE_SUFFIXES:
        raise HTTPException(status_code=400, detail="Định dạng audio không được hỗ trợ.")
    temporary = Path(tempfile.mkdtemp(prefix="vieneu-upload-"))
    source = temporary / f"source{suffix}"
    try:
        await _save_upload(file, source)
        return _register_new_voice(
            name=name,
            revision_builder=lambda voice_id, revision: vieneu_voice_profiles.create_revision(
                voice_id=voice_id,
                revision=revision,
                source_path=source,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                transcript=transcript,
                name=name,
                source_label=source_label,
                rights_note=rights_note,
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(exc) from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


@router.post("/providers/vieneu_v3_turbo/import")
async def import_vieneu_folder(
    files: list[UploadFile] = File(...),
    name: str = Form(..., min_length=1, max_length=120),
    start_seconds: float = Form(0.0, ge=0),
    end_seconds: float = Form(8.0, gt=0),
    source_label: str = Form("", max_length=300),
    rights_note: str = Form("", max_length=1000),
):
    if not files or len(files) > _MAX_UPLOAD_FILES:
        raise HTTPException(status_code=400, detail="Thư mục import phải có từ 1 đến 50 file.")
    temporary = Path(tempfile.mkdtemp(prefix="vieneu-folder-"))
    audio_candidates: list[Path] = []
    uploaded_by_name: dict[str, Path] = {}
    transcript = ""
    try:
        for index, upload in enumerate(files):
            filename = Path(upload.filename or f"file-{index}").name
            suffix = Path(filename).suffix.casefold()
            destination = temporary / f"{index:03d}-{filename}"
            await _save_upload(upload, destination)
            normalized_name = filename.casefold()
            if normalized_name in uploaded_by_name:
                raise ValueError(f"Thư mục import có file trùng tên: {filename}.")
            uploaded_by_name[normalized_name] = destination
            if suffix in vieneu_voice_profiles.SUPPORTED_SOURCE_SUFFIXES:
                audio_candidates.append(destination)
            elif suffix in {".txt", ".srt"} and not transcript:
                transcript = destination.read_text(encoding="utf-8", errors="replace")[:4000]
        checksum_file = uploaded_by_name.get("checksums.json")
        metadata_file = uploaded_by_name.get("metadata.json")
        if checksum_file or metadata_file:
            if not checksum_file or not metadata_file:
                raise ValueError("Profile managed phải có đủ metadata.json và checksums.json.")
            try:
                checksum_data = json.loads(checksum_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("checksums.json của profile không hợp lệ.") from exc
            expected_files = checksum_data.get("files") or {}
            if not isinstance(expected_files, dict) or not expected_files:
                raise ValueError("checksums.json không có danh sách file.")
            for managed_name, expected_hash in expected_files.items():
                candidate = uploaded_by_name.get(str(managed_name).casefold())
                normalized_hash = str(expected_hash or "").strip().casefold()
                if candidate is None or len(normalized_hash) != 64:
                    raise ValueError("Profile managed thiếu file hoặc checksum hợp lệ.")
                if vieneu_tts.file_sha256(candidate) != normalized_hash:
                    raise ValueError(f"Checksum profile không khớp: {managed_name}.")

        preferred = [
            path
            for path in audio_candidates
            if Path(path.name.split("-", 1)[-1]).stem.casefold() == "reference"
        ]
        candidates = preferred or audio_candidates
        if len(candidates) != 1:
            raise ValueError(
                "Thư mục phải có đúng một audio reference, hoặc một file tên reference.*."
            )
        source = candidates[0]
        return _register_new_voice(
            name=name,
            revision_builder=lambda voice_id, revision: vieneu_voice_profiles.create_revision(
                voice_id=voice_id,
                revision=revision,
                source_path=source,
                start_seconds=start_seconds,
                end_seconds=end_seconds,
                transcript=transcript,
                name=name,
                source_label=source_label,
                rights_note=rights_note,
            ),
        )
    except HTTPException:
        raise
    except Exception as exc:
        raise _error(exc) from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


@router.post("/voices/{voice_id}/revisions")
async def create_vieneu_revision(
    voice_id: str,
    file: UploadFile = File(...),
    start_seconds: float = Form(0.0, ge=0),
    end_seconds: float = Form(..., gt=0),
    transcript: str = Form("", max_length=4000),
    source_label: str = Form("", max_length=300),
    rights_note: str = Form("", max_length=1000),
):
    with _CONFIG_LOCK:
        config = voice_config.load_voice_config()
        voice = dict(_voice_by_id(config, voice_id))
    revision = vieneu_voice_profiles.next_revision(voice_id)
    suffix = Path(file.filename or "reference.wav").suffix.casefold()
    temporary = Path(tempfile.mkdtemp(prefix="vieneu-revision-"))
    source = temporary / f"source{suffix}"
    created_revision = False
    try:
        await _save_upload(file, source)
        profile = vieneu_voice_profiles.create_revision(
            voice_id=voice_id,
            revision=revision,
            source_path=source,
            start_seconds=start_seconds,
            end_seconds=end_seconds,
            transcript=transcript,
            name=voice["name"],
            source_label=source_label,
            rights_note=rights_note,
        )
        created_revision = True
        with _CONFIG_LOCK:
            config = voice_config.load_voice_config()
            current = _voice_by_id(config, voice_id)
            current["draft_revision"] = revision
            if current.get("active_revision") is None:
                current["status"] = "draft"
                current["revision"] = revision
            saved = voice_config.save_voice_config(config)
        updated = next(item for item in saved["voices"] if item["id"] == voice_id)
        return {"voice": _public_voice(updated), "revision": profile}
    except Exception as exc:
        if created_revision:
            shutil.rmtree(
                vieneu_voice_profiles.revision_directory(voice_id, revision),
                ignore_errors=True,
            )
        raise _error(exc) from exc
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


@router.post("/voices/{voice_id}/revisions/{revision}/activate")
def activate_vieneu_revision(voice_id: str, revision: int):
    status = vieneu_voice_profiles.revision_status(voice_id, revision, verify_runtime=True)
    if not status.get("available"):
        raise HTTPException(status_code=409, detail=status.get("detail") or "Revision chưa sẵn sàng.")
    with _CONFIG_LOCK:
        config = voice_config.load_voice_config()
        voice = _voice_by_id(config, voice_id)
        voice["active_revision"] = int(revision)
        if voice.get("draft_revision") == int(revision):
            voice["draft_revision"] = None
        voice["revision"] = int(revision)
        voice["status"] = "active"
        saved = voice_config.save_voice_config(config)
    return _public_voice(next(item for item in saved["voices"] if item["id"] == voice_id))


@router.get("/voices/{voice_id}/reference-audio")
def serve_vieneu_reference(
    voice_id: str,
    revision: int = Query(..., ge=1),
    download: bool = Query(False),
):
    try:
        path = vieneu_voice_profiles.reference_path(voice_id, revision)
    except Exception as exc:
        raise _error(exc, status_code=404) from exc
    return FileResponse(
        str(path),
        media_type="audio/wav",
        filename=f"{voice_id}-r{revision}.wav" if download else None,
        headers={"Cache-Control": "no-store"},
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
