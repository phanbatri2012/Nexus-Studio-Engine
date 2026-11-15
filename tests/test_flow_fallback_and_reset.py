import pytest
from unittest.mock import MagicMock, patch
from nexus_engine import main
from nexus_engine.services import database as db

def test_job_supports_action_flow_capabilities():
    item_stalled = {
        "can_flow_fallback_static": True,
        "can_flow_reset_project": True,
        "can_resume": True,
        "can_cancel": True,
    }
    assert main._job_supports_action(item_stalled, "fallback_static") is True
    assert main._job_supports_action(item_stalled, "retry_new_flow_project") is True
    assert main._job_supports_action(item_stalled, "resume") is True
    assert main._job_supports_action(item_stalled, "cancel") is True

    item_other = {
        "can_flow_fallback_static": False,
        "can_flow_reset_project": False,
        "can_resume": True,
    }
    assert main._job_supports_action(item_other, "fallback_static") is False
    assert main._job_supports_action(item_other, "retry_new_flow_project") is False

def test_job_center_actions_includes_new_actions():
    assert "fallback_static" in main.JOB_CENTER_ACTIONS
    assert "retry_new_flow_project" in main.JOB_CENTER_ACTIONS

def test_flow_media_resume_resets_stagnant_count_and_terminal_scenes(monkeypatch):
    mock_job = {
        "id": "flow-test-123",
        "job_type": "flow_media_generation",
        "status": "paused",
        "video_id": 999,
        "result": {
            "attention_required": "flow_reconcile_stalled",
            "reconcile_stagnant_count": 15,
        },
        "payload": {"video_id": 999},
    }

    updated_result = {}
    upserted_artifacts = []
    def mock_get_system_job(job_id):
        return mock_job

    def mock_update_system_job(job_id, **kwargs):
        if "result_json" in kwargs:
            updated_result.update(kwargs["result_json"])

    def mock_resume_system_job(job_id):
        return {"id": job_id, "status": "queued"}

    def mock_update_video_production_state(*args, **kwargs):
        pass

    mock_artifacts = {
        "scene:0": {"id": 1, "status": "completed", "metadata": {}},
        "scene:1": {"id": 2, "status": "failed", "metadata": {"terminal": True, "reason_code": "flow_max_attempts_exceeded"}},
    }
    def mock_list_latest_video_artifacts(vid_id, prefixes):
        if "scene:" in prefixes:
            return mock_artifacts
        return {}

    def mock_upsert_video_artifact(**kwargs):
        upserted_artifacts.append(kwargs)

    monkeypatch.setattr(db, "get_system_job", mock_get_system_job)
    monkeypatch.setattr(db, "update_system_job", mock_update_system_job)
    monkeypatch.setattr(db, "resume_system_job", mock_resume_system_job)
    monkeypatch.setattr(db, "update_video_production_state", mock_update_video_production_state)
    monkeypatch.setattr(db, "list_latest_video_artifacts", mock_list_latest_video_artifacts)
    monkeypatch.setattr(db, "upsert_video_artifact", mock_upsert_video_artifact)
    monkeypatch.setattr(main, "_kick_job_queues", lambda x: None)

    main._run_system_job_center_action("flow-test-123", "resume", kick_queues=False)

    assert updated_result.get("reconcile_stagnant_count") == 0
    assert "attention_required" not in updated_result
    assert len(upserted_artifacts) == 1
    assert upserted_artifacts[0]["artifact_type"] == "scene:1"
    assert upserted_artifacts[0]["status"] == "queued"
    assert upserted_artifacts[0]["metadata"]["terminal"] is False
    assert upserted_artifacts[0]["metadata"]["generation_attempt"] == 0

def test_retry_new_flow_project_purges_failed_scene_artifacts(monkeypatch):
    mock_job = {
        "id": "flow-test-456",
        "job_type": "flow_media_generation",
        "status": "paused",
        "video_id": 888,
        "result": {"attention_required": "flow_image_generation"},
        "payload": {"video_id": 888},
    }

    deleted_artifacts = []
    project_requested = []

    def mock_get_system_job(job_id):
        return mock_job

    def mock_update_system_job(job_id, **kwargs):
        pass

    def mock_update_video_production_state(*args, **kwargs):
        pass

    mock_scenes = {
        "scene:10": {"id": 101, "status": "completed", "metadata": {}},
        "scene:11": {"id": 102, "status": "failed", "metadata": {"terminal": True, "reason_code": "flow_max_attempts_exceeded"}},
    }

    def mock_list_latest(vid_id, prefixes):
        if "scene:" in prefixes:
            return mock_scenes
        return {}

    def mock_delete_artifact(art_id):
        deleted_artifacts.append(art_id)
        return True

    def mock_request_new(vid_id):
        project_requested.append(vid_id)
        return {}

    monkeypatch.setattr(db, "get_system_job", mock_get_system_job)
    monkeypatch.setattr(db, "update_system_job", mock_update_system_job)
    monkeypatch.setattr(db, "update_video_production_state", mock_update_video_production_state)
    monkeypatch.setattr(db, "list_latest_video_artifacts", mock_list_latest)
    monkeypatch.setattr(db, "delete_video_artifact", mock_delete_artifact)
    monkeypatch.setattr(db, "request_new_flow_project", mock_request_new)
    monkeypatch.setattr(main, "_kick_job_queues", lambda x: None)

    main._run_system_job_center_action("flow-test-456", "retry_new_flow_project", kick_queues=False)

    assert deleted_artifacts == [102]
    assert project_requested == [888]


def test_retry_new_flow_canvas_for_video_validation(monkeypatch):
    from fastapi import HTTPException

    # Video not found -> 404
    monkeypatch.setattr(db, "get_video", lambda vid: None)
    with pytest.raises(HTTPException) as exc_info:
        main.retry_new_flow_canvas_for_video(9999)
    assert exc_info.value.status_code == 404

    # Video in error status -> 400
    monkeypatch.setattr(db, "get_video", lambda vid: {"id": vid, "video_status": db.VIDEO_STATUS_ERROR})
    with pytest.raises(HTTPException) as exc_info:
        main.retry_new_flow_canvas_for_video(9999)
    assert exc_info.value.status_code == 400

    # Active conflicting job running -> 409
    monkeypatch.setattr(db, "get_video", lambda vid: {"id": vid, "video_status": "ready"})
    monkeypatch.setattr(db, "list_system_jobs", lambda video_id, limit=None: [
        {"id": "job-1", "job_type": "flow_media_generation", "status": "running"}
    ])
    with pytest.raises(HTTPException) as exc_info:
        main.retry_new_flow_canvas_for_video(9999)
    assert exc_info.value.status_code == 409


def test_retry_new_flow_canvas_for_video_success_after_completed_job(monkeypatch):
    """Simulate video 379 scenario: video in intro_fallback_review with a completed flow job."""
    from nexus_engine.services import video_production

    mock_video = {
        "id": 379,
        "video_status": "ready",
        "prompt_version": "v3",
        "render_status": "pending_review",
        "current_stage": "intro_fallback_review",
    }
    # Video has completed previous jobs
    mock_jobs = [
        {"id": "flow-media-old", "job_type": "flow_media_generation", "status": "completed"},
        {"id": "flow-media-paused", "job_type": "flow_media_generation", "status": "paused"},
    ]

    purged_videos = []
    def mock_purge_intro(vid_id, keep_successful=True):
        purged_videos.append((vid_id, keep_successful))
        return 12

    requested_flow_projects = []
    def mock_request_new_flow(vid_id):
        requested_flow_projects.append(vid_id)
        return {"reset_requested": 1}

    enqueued_jobs = []
    def mock_enqueue(video_id, snapshot, mode, force_new_flow_project):
        enqueued_jobs.append({
            "video_id": video_id,
            "mode": mode,
            "force_new_flow_project": force_new_flow_project,
        })
        return {"id": "flow-media-new", "status": "queued"}, True

    updated_production_states = []
    def mock_update_prod_state(vid_id, **kwargs):
        updated_production_states.append((vid_id, kwargs))

    canceled_jobs = []
    def mock_update_system_job(job_id, **kwargs):
        if kwargs.get("status") == "canceled":
            canceled_jobs.append(job_id)

    monkeypatch.setattr(db, "get_video", lambda vid: mock_video)
    monkeypatch.setattr(db, "list_system_jobs", lambda video_id, limit=None: mock_jobs)
    monkeypatch.setattr(db, "update_system_job", mock_update_system_job)
    monkeypatch.setattr(video_production, "purge_intro_video_artifacts", mock_purge_intro)
    monkeypatch.setattr(db, "request_new_flow_project", mock_request_new_flow)
    monkeypatch.setattr(main, "_get_prompt_production_snapshot", lambda ver: {"prompt_version": ver})
    monkeypatch.setattr(main, "_enqueue_flow_media_job", mock_enqueue)
    monkeypatch.setattr(db, "update_video_production_state", mock_update_prod_state)
    monkeypatch.setattr(main, "_kick_job_queues", lambda x: None)

    res = main.retry_new_flow_canvas_for_video(379)

    assert res["success"] is True
    assert res["video_id"] == 379
    assert res["job_id"] == "flow-media-new"
    assert res["purged_count"] == 12

    # Verify paused job was canceled to avoid duplicate queue conflict
    assert canceled_jobs == ["flow-media-paused"]
    # Verify intro video artifacts were purged with keep_successful=True
    assert purged_videos == [(379, True)]
    # Verify new flow project was requested
    assert requested_flow_projects == [379]
    # Verify new flow media job was enqueued with force_new_flow_project=True
    assert len(enqueued_jobs) == 1
    assert enqueued_jobs[0]["force_new_flow_project"] is True
    assert enqueued_jobs[0]["mode"] == "resume"
    # Verify production state transitioned to queued
    assert len(updated_production_states) == 1
    assert updated_production_states[0][1]["render_status"] == "queued"
    assert updated_production_states[0][1]["current_stage"] == "flow_media_generation"


@pytest.mark.anyio
async def test_handle_unexpected_error_adds_cors_header():
    from starlette.datastructures import Headers
    from unittest.mock import MagicMock

    mock_request = MagicMock()
    mock_request.headers = Headers({"origin": "http://127.0.0.1:5173"})

    exc = RuntimeError("Test unhandled exception")
    response = await main.handle_unexpected_error(mock_request, exc)

    assert response.status_code == 500
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:5173"
    assert response.headers["access-control-allow-credentials"] == "true"


@pytest.mark.anyio
async def test_generate_scene_video_reuses_valid_target_on_disk(tmp_path, monkeypatch):
    from pathlib import Path
    from nexus_engine.services import video_production

    mock_target = tmp_path / "379_0_video_test1234.mp4"
    mock_target.write_bytes(b"dummy video content" * 200)

    monkeypatch.setattr(video_production, "SCENES_DIR", tmp_path)
    monkeypatch.setattr(video_production, "probe_scene_video", lambda p: {"duration_seconds": 4.0, "video_codec": "h264"})
    monkeypatch.setattr(video_production, "_sha256_file", lambda p: "test_hash_val")
    monkeypatch.setattr(video_production, "_scene_hash", lambda *args: "test1234abcd")

    start_frame = tmp_path / "start.png"
    start_frame.write_bytes(b"image")
    end_frame = tmp_path / "end.png"
    end_frame.write_bytes(b"image")

    res = await video_production._generate_scene_video_async(
        video_id=379,
        scene={"index": 0, "prompt": "test prompt"},
        scene_count=1,
        start_frame_path=start_frame,
        end_frame_path=end_frame,
        profile={},
        settings={},
        progress=lambda *args, **kwargs: None,
        cancel_check=lambda: None,
        worker=None,
        force_new_project=False,
    )

    assert res == mock_target


@pytest.mark.anyio
async def test_download_video_from_ui_clicks_canvas_tile_locator(tmp_path):
    from unittest.mock import AsyncMock, MagicMock
    from pathlib import Path
    from nexus_engine.services.google_flow_worker import GoogleFlowWorker

    mock_page = MagicMock()
    mock_page.url = "https://flow.google.com/project/123"

    mock_tile = MagicMock()
    mock_tile.is_visible = AsyncMock(return_value=True)
    mock_tile.click = AsyncMock()
    mock_tile.first = mock_tile

    mock_dl_btn = MagicMock()
    mock_dl_btn.is_visible = AsyncMock(return_value=True)
    mock_dl_btn.click = AsyncMock()
    mock_dl_btn.first = mock_dl_btn

    mock_720 = MagicMock()
    mock_720.is_visible = AsyncMock(return_value=True)
    mock_720.click = AsyncMock()
    mock_720.first = mock_720

    mock_download = MagicMock()
    async def mock_save_as(path):
        Path(path).write_bytes(b"downloaded 720p content" * 100)
    mock_download.save_as = AsyncMock(side_effect=mock_save_as)

    class MockDownloadInfo:
        @property
        async def value(self):
            return mock_download

    class MockExpectDownload:
        async def __aenter__(self):
            return MockDownloadInfo()
        async def __aexit__(self, exc_type, exc, tb):
            pass

    mock_page.expect_download = MagicMock(return_value=MockExpectDownload())

    def mock_locator(sel):
        if "Tải" in sel or "Download" in sel:
            return mock_dl_btn
        if "flow-grid-tile" in sel or "flow-tile" in sel or "379_0" in sel:
            return mock_tile
        if "720p" in sel:
            return mock_720
        mock_generic = MagicMock()
        mock_generic.is_visible = AsyncMock(return_value=False)
        return mock_generic

    mock_page.locator = mock_locator
    mock_page.keyboard = MagicMock()
    mock_page.keyboard.press = AsyncMock()

    worker = GoogleFlowWorker.__new__(GoogleFlowWorker)
    worker.page = mock_page
    worker._last_video_timing = {}
    worker._active_video_submitted_at = None

    target = tmp_path / "downloaded.mp4"
    res = await worker._download_video_from_ui("flow_canvas_tile:379_0_f506a937.png", target)

    assert res is True
    assert target.exists()
    assert target.stat().st_size > 1000
    mock_tile.click.assert_awaited()
    mock_dl_btn.click.assert_awaited()
    mock_720.click.assert_awaited()


def test_video_extension_preserves_short_intro_video_with_slowdown_then_hold():
    from nexus_engine.services import video_production

    # A 4.032s video for a 7.23s scene
    # Without preserve_video: degraded_to_image
    res_default = video_production._plan_video_extension(
        source_duration=4.032,
        target_duration=7.23,
        has_fallback_image=True,
        preserve_video=False,
    )
    assert res_default["strategy"] == "degraded_to_image"
    assert res_default["render_degraded_to_image"] is True

    # With preserve_video=True: slowdown_then_hold
    res_preserved = video_production._plan_video_extension(
        source_duration=4.032,
        target_duration=7.23,
        has_fallback_image=True,
        preserve_video=True,
    )
    assert res_preserved["strategy"] == "slowdown_then_hold"
    assert res_preserved["render_degraded_to_image"] is False
    assert res_preserved["setpts_factor"] == 1.10
    # 7.23 - 4.032 * 1.10 = 7.23 - 4.4352 = 2.795s hold
    assert abs(res_preserved["hold_seconds"] - 2.795) < 0.01
