import pytest
from pathlib import Path
from unittest.mock import patch

from nexus_engine.services import database as db
from nexus_engine.main import _check_video_media_readiness


def test_media_readiness_prioritizes_flow_job_progress(tmp_path):
    video_id = db.save_video(
        url="https://youtube.com/watch?v=test_readiness_priority",
        title="Test Readiness Priority Video",
        transcript="Test transcript",
        generated_script="Test script",
        prompt_version="test_v1",
    )
    job_id = "test-flow-job-priority-1"

    try:
        # Create 10 old completed artifacts for this video in DB
        for i in range(10):
            img_file = tmp_path / f"scene_{i}.png"
            img_file.write_bytes(b"PNG_DATA")
            db.upsert_video_artifact(
                video_id=video_id,
                artifact_type=f"scene:{i}",
                path=str(img_file),
                content_hash=f"old_hash_{i}",
                status="completed",
                mime_type="image/png",
            )

        # Mock visual scene plan with 10 scenes
        mock_plan = (
            "hash",
            {"scenes": [{"index": i, "is_video": (i < 2)} for i in range(10)]},
        )

        with patch("nexus_engine.services.video_production.load_visual_scene_plan", return_value=mock_plan):
            # Case 1: When no media job exists, falls back to counting valid artifacts (10 images)
            readiness_fallback = _check_video_media_readiness(video_id)
            assert readiness_fallback["completed_images"] == 10
            assert readiness_fallback["total_images"] == 10
            assert readiness_fallback["all_base_images_ready"] is True

            # Case 2: When an active/paused flow_media_generation job exists reporting verified progress (e.g. 4/10 images)
            job = db.create_system_job(
                job_id=job_id,
                job_type="flow_media_generation",
                video_id=video_id,
                title="Tạo media Google Flow",
                payload={"video_id": video_id},
            )
            flow_progress = {
                "phase": "images",
                "images_total": 10,
                "images_completed": 4,
                "images_pending": 6,
                "videos_total": 2,
                "videos_completed": 0,
            }
            db.update_system_job(
                job["id"],
                status="running",
                result_json={"flow_media_progress": flow_progress},
            )

            readiness_job = _check_video_media_readiness(video_id)
            assert readiness_job["completed_images"] == 4
            assert readiness_job["total_images"] == 10
            assert readiness_job["all_base_images_ready"] is False
            assert readiness_job["completed_videos"] == 0
            assert readiness_job["total_videos"] == 2

            # Case 3: When the job is paused at checkpoint
            db.update_system_job(job["id"], status="paused")
            readiness_paused = _check_video_media_readiness(video_id)
            assert readiness_paused["completed_images"] == 4
            assert readiness_paused["all_base_images_ready"] is False

    finally:
        try:
            db.delete_system_job(job_id)
        except Exception:
            pass
        db.delete_video(video_id)
