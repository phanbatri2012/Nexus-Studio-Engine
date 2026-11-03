import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from nexus_engine.services import video_production
from nexus_engine.services import database as db
from nexus_engine.main import (
    app,
    decide_intro_review,
    trigger_regenerate_intro_videos,
    IntroReviewDecisionRequest,
    RegenerateIntroRequest,
)


def test_purge_intro_video_artifacts_selective(tmp_path):
    video_id = 99999
    img_file = tmp_path / "99999_0_img.png"
    img_file.write_text("fake_image")

    # Successful video clip > 1000 bytes
    vid_file_ok = tmp_path / "99999_0_video_ok.mp4"
    vid_file_ok.write_bytes(b"A" * 2000)

    # Failed video clip
    vid_file_failed = tmp_path / "99999_1_video_failed.mp4"
    vid_file_failed.write_bytes(b"small_err")

    man_file = tmp_path / "99999_manifest.json"
    man_file.write_text("{}")
    mp4_file = tmp_path / "99999_final.mp4"
    mp4_file.write_text("fake_final")

    db.upsert_video_artifact(
        video_id=video_id,
        artifact_type="scene:0",
        path=str(img_file),
        content_hash="hash_img",
        status="completed",
        mime_type="image/png"
    )
    db.upsert_video_artifact(
        video_id=video_id,
        artifact_type="scene_video:0",
        path=str(vid_file_ok),
        content_hash="hash_vid_ok",
        status="completed",
        mime_type="video/mp4"
    )
    db.upsert_video_artifact(
        video_id=video_id,
        artifact_type="scene_video:1",
        path=str(vid_file_failed),
        content_hash="hash_vid_failed",
        status="failed",
        mime_type="video/mp4"
    )
    db.upsert_video_artifact(
        video_id=video_id,
        artifact_type="flow_media_manifest",
        path=str(man_file),
        content_hash="hash_man",
        status="ready",
        mime_type="application/json"
    )

    try:
        # Case 1: keep_successful=True -> Keep scene_video:0, delete scene_video:1
        purged = video_production.purge_intro_video_artifacts(video_id, keep_successful=True)
        assert purged == 1

        # Base image must still exist
        assert img_file.exists()
        assert db.get_latest_video_artifact(video_id, "scene:0") is not None

        # Successful video must be KEPT
        assert vid_file_ok.exists()
        assert db.get_latest_video_artifact(video_id, "scene_video:0") is not None

        # Failed video must be REMOVED
        assert not vid_file_failed.exists()
        assert db.get_latest_video_artifact(video_id, "scene_video:1") is None

        # Manifest must be purged
        assert not man_file.exists()
        assert db.get_latest_video_artifact(video_id, "flow_media_manifest") is None

        # Case 2: keep_successful=False -> Purge all remaining scene_video artifacts
        purged_all = video_production.purge_intro_video_artifacts(video_id, keep_successful=False)
        assert purged_all == 1
        assert not vid_file_ok.exists()
        assert db.get_latest_video_artifact(video_id, "scene_video:0") is None

    finally:
        for art in db.list_video_artifacts(video_id):
            if art.get("id"):
                db.delete_video_artifact(art["id"])


def test_guard_clause_and_intro_review_decision():
    video_id = db.save_video(
        url="https://youtube.com/watch?v=testguard",
        title="Test Guard Clause",
        transcript="Test transcript",
        generated_script="Test script",
        prompt_version="test_v1"
    )

    try:
        # 1. Test decision accept_fallback
        with patch("nexus_engine.main._enqueue_video_render_job") as mock_enqueue, \
             patch("nexus_engine.main._kick_job_queues") as mock_kick:
            mock_enqueue.return_value = ({"id": "render-job-123"}, True)
            
            res = decide_intro_review(video_id, IntroReviewDecisionRequest(action="accept_fallback"))
            assert res["success"] is True
            assert res["action"] == "accept_fallback"
            
            video = db.get_video(video_id)
            assert video["current_stage"] == "video_render_queue"
            assert video["render_status"] == "queued"

        # 2. Test decision retry_intro with keep_successful=True
        with patch("nexus_engine.main._enqueue_flow_media_job") as mock_flow, \
             patch("nexus_engine.main._kick_job_queues") as mock_kick:
            mock_flow.return_value = ({"id": "flow-job-456", "status": "queued"}, True)

            res = decide_intro_review(video_id, IntroReviewDecisionRequest(action="retry_intro", keep_successful=True))
            assert res["success"] is True
            assert res["job_id"] == "flow-job-456"
            assert res["keep_successful"] is True

            video = db.get_video(video_id)
            assert video["current_stage"] == "flow_media_generation"
            assert video["render_status"] == "queued"

        # 3. Test direct trigger_regenerate_intro_videos with keep_successful=False
        with patch("nexus_engine.main._enqueue_flow_media_job") as mock_flow, \
             patch("nexus_engine.main._kick_job_queues") as mock_kick:
            mock_flow.return_value = ({"id": "flow-job-789", "status": "queued"}, True)

            res = trigger_regenerate_intro_videos(video_id, RegenerateIntroRequest(keep_successful=False))
            assert res["success"] is True
            assert res["job_id"] == "flow-job-789"
            assert res["keep_successful"] is False
    finally:
        db.delete_video(video_id)


def test_materialize_flow_media_manifest_with_fallbacks(tmp_path):
    video_id = 99998
    img0 = tmp_path / "img0.png"
    img0.write_bytes(b"img0")
    img1 = tmp_path / "img1.png"
    img1.write_bytes(b"img1")

    vid0 = tmp_path / "vid0.mp4"
    vid0.write_bytes(b"vid0" * 300)

    db.upsert_video_artifact(video_id=video_id, artifact_type="scene:0", path=str(img0), content_hash="h0", status="completed", mime_type="image/png")
    db.upsert_video_artifact(video_id=video_id, artifact_type="scene:1", path=str(img1), content_hash="h1", status="completed", mime_type="image/png")
    db.upsert_video_artifact(video_id=video_id, artifact_type="scene_video:0", path=str(vid0), content_hash="hv0", status="completed", mime_type="video/mp4")
    db.upsert_video_artifact(video_id=video_id, artifact_type="scene_video:1", path=str(tmp_path / "vid1.mp4"), content_hash="hv1", status="queued", mime_type="video/mp4")

    mock_plan = {
        "scenes": [
            {"index": 0, "is_video": True, "media_type": "video"},
            {"index": 1, "is_video": True, "media_type": "video"},
        ],
        "visual_bible": {},
    }
    mock_prepared = {
        "plan_hash": "plan_hash_123",
        "caption_hash": "cap_hash_456",
        "windows": [],
    }

    with patch("nexus_engine.services.video_production.prepare_visual_plan_inputs", return_value=mock_prepared), \
         patch("nexus_engine.services.video_production.load_visual_scene_plan", return_value=({"content_hash": "plan_hash_123"}, mock_plan)), \
         patch("nexus_engine.services.video_production.validate_visual_scene_plan", return_value=mock_plan):
        
        result = video_production.materialize_flow_media_manifest_with_fallbacks(video_id, snapshot={})
        
        assert result["plan_hash"] == "plan_hash_123"
        assert result["scene_count"] == 2
        summary = result["intro_video_summary"]
        assert summary["enabled"] is True
        assert summary["requested_count"] == 2
        assert summary["generated_count"] == 1
        assert summary["fallback_count"] == 1
        assert summary["fallback_scenes"][0]["scene_index"] == 1
        assert summary["fallback_scenes"][0]["reason_code"] == "user_accepted_fallback"

        # Check DB artifact
        art = db.get_latest_video_artifact(video_id, "flow_media_manifest", status="ready")
        assert art is not None
        assert art["status"] == "ready"

        # Verify queued scene_video:1 was updated to failed
        vid1_art = db.get_latest_video_artifact(video_id, "scene_video:1")
        assert vid1_art["status"] == "failed"

    # Cleanup
    for a in db.list_video_artifacts(video_id):
        if a.get("id"):
            db.delete_video_artifact(a["id"])



def test_accept_fallback_rejected_when_base_images_incomplete():
    from fastapi import HTTPException
    video_id = db.save_video(
        url="https://youtube.com/watch?v=testincomplete",
        title="Test Incomplete Images",
        transcript="Test transcript",
        generated_script="Test script",
        prompt_version="test_v1"
    )
    try:
        # Mock _check_video_media_readiness to report 77/137 images
        mock_readiness = {
            "all_base_images_ready": False,
            "total_scenes": 137,
            "completed_base_images": 77,
            "requested_intro_videos": 12,
            "completed_intro_videos": 0,
        }
        with patch("nexus_engine.main._check_video_media_readiness", return_value=mock_readiness):
            with pytest.raises(HTTPException) as exc_info:
                decide_intro_review(video_id, IntroReviewDecisionRequest(action="accept_fallback"))
            assert exc_info.value.status_code == 400
            assert "Chưa hoàn tất toàn bộ ảnh tĩnh cơ sở (77/137 ảnh)" in exc_info.value.detail
    finally:
        db.delete_video(video_id)
