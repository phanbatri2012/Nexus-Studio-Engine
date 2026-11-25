from __future__ import annotations

import subprocess
from pathlib import Path

import imageio_ffmpeg
import pytest
from PIL import Image

from nexus_engine.services import full_movie_thumbnail_pipeline as pipeline
from nexus_engine.services import full_movie_thumbnail_worker as worker
from nexus_engine.services.chatgpt_runtime import ChatGPTAttentionRequiredError


def _media() -> dict:
    return {
        "duration": 120.0,
        "width": 1920,
        "height": 1080,
        "video_codec": "h264",
    }


def test_resolve_movie_path_accepts_file_or_single_full_movie_folder(tmp_path):
    movie = tmp_path / "Full Movie - Demo.mp4"
    movie.write_bytes(b"video")

    assert pipeline.resolve_movie_path(str(movie)) == movie.resolve()
    assert pipeline.resolve_movie_path(str(tmp_path)) == movie.resolve()


def test_resolve_movie_path_rejects_ambiguous_folder(tmp_path):
    (tmp_path / "Full Movie - One.mp4").write_bytes(b"one")
    (tmp_path / "Full Movie - Two.mkv").write_bytes(b"two")

    with pytest.raises(pipeline.FullMovieThumbnailError, match="nhiều video full"):
        pipeline.resolve_movie_path(str(tmp_path))


def test_crop_pillarbox_removes_symmetric_black_sides_without_stretching():
    source = Image.new("RGB", (1920, 1080), "black")
    source.paste(Image.new("RGB", (608, 1080), "#c06050"), (656, 0))

    cropped = pipeline._crop_pillarbox(source)

    assert 590 <= cropped.width <= 630
    assert cropped.height == 1080


def test_real_frame_extraction_crops_black_sides_from_1282_vertical_source(tmp_path):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    movie = tmp_path / "Full Movie - Vertical.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=720x1282:rate=5:duration=3",
            "-vf",
            (
                "scale=1920:1080:force_original_aspect_ratio=decrease:force_divisible_by=2,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:black,format=yuv420p"
            ),
            "-c:v",
            "libx264",
            str(movie),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )

    manifest = pipeline.extract_reference_frames(movie, tmp_path / "work", 3.0)

    assert manifest["frames"]
    assert Path(manifest["contact_sheet"]).is_file()
    with Image.open(manifest["frames"][0]["path"]) as reference:
        assert reference.width < 800
        assert reference.height >= 1000


def test_render_text_variant_is_youtube_size_and_below_two_megabytes(tmp_path):
    raw = tmp_path / "raw.png"
    output = tmp_path / "final.jpg"
    image = Image.new("RGB", (1536, 1024), "#7b3540")
    image.paste(Image.new("RGB", (600, 900), "#d7a080"), (700, 50))
    image.save(raw)

    score = pipeline.render_text_variant(
        raw,
        output,
        "HER SECRET REVENGE",
        "left",
        1,
    )

    with Image.open(output) as rendered:
        assert rendered.size == (1280, 720)
        assert rendered.mode == "RGB"
    assert output.stat().st_size <= pipeline.MAX_OUTPUT_BYTES
    assert 0 < score <= 100


def test_create_job_deduplicates_active_video(tmp_path, monkeypatch):
    movie = tmp_path / "Full Movie - Demo.mp4"
    movie.write_bytes(b"video")
    active = {
        "id": "a" * 32,
        "job_type": pipeline.JOB_TYPE,
        "status": "running",
        "payload": {"resolved_video_path": str(movie.resolve())},
        "result": {},
    }
    monkeypatch.setattr(pipeline, "probe_media", lambda _path: _media())
    monkeypatch.setattr(
        pipeline.db,
        "list_system_jobs",
        lambda **_kwargs: [active],
    )

    job, created = pipeline.create_job("Demo Drama", str(movie))

    assert created is False
    assert job["id"] == active["id"]


def test_create_job_records_snapshot_and_origin(tmp_path, monkeypatch):
    movie = tmp_path / "Full Movie - Demo.mp4"
    movie.write_bytes(b"video")
    created_payload = {}
    monkeypatch.setattr(pipeline, "probe_media", lambda _path: _media())
    monkeypatch.setattr(pipeline.db, "list_system_jobs", lambda **_kwargs: [])

    def create_system_job(job_id, job_type, title, payload):
        created_payload.update(payload)
        return {
            "id": job_id,
            "job_type": job_type,
            "status": "queued",
            "title": title,
            "progress": "",
            "payload": payload,
            "result": {},
        }

    monkeypatch.setattr(pipeline.db, "create_system_job", create_system_job)

    job, created = pipeline.create_job(
        "Demo Drama",
        str(movie),
        origin="full_movie_merge",
        parent_job_id="parent-job",
    )

    assert created is True
    assert job["job_type"] == pipeline.JOB_TYPE
    assert created_payload["origin"] == "full_movie_merge"
    assert created_payload["parent_job_id"] == "parent-job"
    assert created_payload["source_snapshot"]["size"] == 5


def test_public_job_and_image_slot_use_only_published_result(tmp_path):
    output_dir = tmp_path / "Thumbnails" / "Demo"
    output_dir.mkdir(parents=True)
    variant = output_dir / "01 - HOOK.jpg"
    primary = output_dir / "PRIMARY.jpg"
    variant.write_bytes(b"jpg")
    primary.write_bytes(b"jpg")
    job = {
        "id": "b" * 32,
        "job_type": pipeline.JOB_TYPE,
        "status": "completed",
        "title": "Thumbnail Demo",
        "payload": {"stage": "completed"},
        "result": {
            "output_dir": str(output_dir),
            "primary_path": str(primary),
            "primary_index": 1,
            "variants": [{"index": 1, "path": str(variant), "hook": "HOOK"}],
        },
    }

    public = pipeline.public_job(job)

    assert public["result"]["variants"][0]["url"].endswith("/images/1")
    assert pipeline.image_path_for_slot(job, "1") == variant.resolve()
    assert pipeline.image_path_for_slot(job, "primary") == primary.resolve()
    with pytest.raises(pipeline.FullMovieThumbnailError):
        pipeline.image_path_for_slot(job, "4")


def test_retry_job_type_uses_scheduled_backoff():
    from nexus_engine.services import database

    assert pipeline.JOB_TYPE in database.SCHEDULED_RETRY_JOB_TYPES


def test_json_prompt_repairs_one_invalid_response(monkeypatch):
    responses = iter(
        [
            "not json",
            '{"normalized_title_us":"Demo","variants":['
            '{"index":1,"hook":"FIRST HOOK","reference_ids":["F01"],"concept":"one","text_zone":"left"},'
            '{"index":2,"hook":"SECOND HOOK","reference_ids":["F01"],"concept":"two","text_zone":"right"},'
            '{"index":3,"hook":"THIRD HOOK","reference_ids":["F01"],"concept":"three","text_zone":"bottom"}]}'
        ]
    )
    monkeypatch.setattr(pipeline, "send_prompt", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(
        pipeline,
        "recover_assistant_response_from_backend",
        lambda *_args, **_kwargs: "",
    )

    result = pipeline._send_json_prompt(
        object(),
        "prompt",
        [],
        lambda value: pipeline._validate_concepts(value, "Demo", {"F01"}),
    )

    assert result["normalized_title_us"] == "Demo"
    assert [item["hook"] for item in result["variants"]] == [
        "FIRST HOOK",
        "SECOND HOOK",
        "THIRD HOOK",
    ]


def test_retry_later_is_bounded_and_scheduled(monkeypatch):
    scheduled = []
    monkeypatch.setattr(
        pipeline.db,
        "get_system_job",
        lambda _job_id: {"recovery_count": 0},
    )
    monkeypatch.setattr(
        pipeline.db,
        "schedule_system_job_recovery",
        lambda job_id, **changes: scheduled.append((job_id, changes)),
    )

    pipeline.handle_error(
        {"id": "a" * 32, "resume_from_step": "concepts"},
        pipeline.FullMovieThumbnailRetryLater("ChatGPT đang bận"),
    )

    assert scheduled[0][1]["delay_seconds"] == 20
    assert scheduled[0][1]["resume_from_step"] == "concepts"


def test_attention_error_pauses_instead_of_failing(monkeypatch):
    updates = []
    monkeypatch.setattr(
        pipeline.db,
        "update_system_job",
        lambda job_id, **changes: updates.append((job_id, changes)),
    )

    pipeline.handle_error(
        {"id": "b" * 32},
        ChatGPTAttentionRequiredError("Phiên đăng nhập ChatGPT đã hết hạn."),
    )

    assert updates[0][1]["status"] == "paused"
    assert updates[0][1]["result_json"]["attention_required"] == "chatgpt_verification"


def test_execute_publishes_three_variants_and_primary_without_real_chatgpt(tmp_path, monkeypatch):
    movie = tmp_path / "Full Movie - Demo.mp4"
    movie.write_bytes(b"video")
    snapshot = {
        "path": str(movie.resolve()),
        "size": 5,
        "mtime_ns": movie.stat().st_mtime_ns,
        "duration": 60.0,
        "width": 1920,
        "height": 1080,
    }
    job_id = "c" * 32
    job = {
        "id": job_id,
        "payload": {
            "input_title": "Demo Drama",
            "input_path": str(movie),
            "resolved_video_path": str(movie),
            "source_snapshot": snapshot,
            "stage": "scan_frames",
        },
    }
    updates = []
    monkeypatch.setattr(pipeline, "_snapshot_matches", lambda *_args: True)
    monkeypatch.setattr(
        pipeline,
        "extract_reference_frames",
        lambda *_args: {"frames": [{"id": "F01", "path": str(movie)}], "contact_sheet": str(movie)},
    )
    monkeypatch.setattr(
        pipeline.db,
        "get_system_job",
        lambda _job_id: {"id": job_id, "status": "running", "cancel_requested": False},
    )
    monkeypatch.setattr(
        pipeline.db,
        "update_system_job",
        lambda _job_id, **changes: updates.append(changes) or {"id": job_id, **changes},
    )

    def fake_generate(_job_id, _payload, _checkpoint, temp_dir, _manifest):
        final_dir = temp_dir / "final"
        final_dir.mkdir(parents=True, exist_ok=True)
        variants = []
        for index in range(1, 4):
            target = final_dir / f"variant_{index}.jpg"
            Image.new("RGB", (1280, 720), (60 * index, 40, 90)).save(target, quality=85)
            variants.append(
                {
                    "index": index,
                    "hook": f"HOOK {index}",
                    "text_zone": pipeline.TEXT_ZONES[index - 1],
                    "technical_score": 70 + index,
                    "creative_score": 80 + index,
                    "score": 78 + index,
                    "reason": "mock",
                    "temp_path": str(target),
                }
            )
        return {"normalized_title_us": "Demo Drama", "variants": []}, variants

    monkeypatch.setattr(pipeline, "_generate_with_chatgpt", fake_generate)

    pipeline.execute(job)

    completed = updates[-1]
    result = completed["result_json"]
    assert completed["status"] == "completed"
    assert len(result["variants"]) == 3
    assert result["primary_index"] == 3
    assert Path(result["primary_path"]).is_file()
    assert Path(result["output_dir"]).parent.name == "Thumbnails"
    assert not (tmp_path / ".nexus_thumbnail").exists()


def test_worker_pauses_interrupted_jobs_after_restart(monkeypatch):
    updates = []
    monkeypatch.setattr(worker.db, "recover_interrupted_system_jobs", lambda _job_type: 1)
    monkeypatch.setattr(
        worker.db,
        "list_system_jobs",
        lambda **_kwargs: [{"id": "d" * 32, "result": {}}],
    )
    monkeypatch.setattr(
        worker.db,
        "update_system_job",
        lambda job_id, **changes: updates.append((job_id, changes)) or {"id": job_id},
    )

    paused = worker.pause_after_restart()

    assert paused == 1
    assert updates[0][1]["status"] == "paused"
    assert (
        updates[0][1]["result_json"]["attention_required"]
        == "full_movie_thumbnail_resume_after_restart"
    )
