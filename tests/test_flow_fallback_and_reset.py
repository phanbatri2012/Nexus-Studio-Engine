import pytest
from unittest.mock import MagicMock, patch
from nexus_engine import main
from nexus_engine.services import database as db, video_production

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

def test_flow_reconcile_resume_does_not_reset_terminal_scenes(monkeypatch):
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
    assert upserted_artifacts == []


def test_flow_scene_retry_only_resets_failed_scene_and_preserves_later_completed(monkeypatch):
    mock_job = {
        "id": "flow-video-471",
        "job_type": "flow_media_generation",
        "status": "paused",
        "video_id": 471,
        "result": {
            "attention_required": "flow_image_generation",
            "flow_media_progress": {"images_total": 186, "images_completed": 185},
        },
        "payload": {
            "video_id": 471,
            "force_new_project": True,
            "force_new_flow_project": True,
        },
    }
    artifacts = {
        f"scene:{index}": {
            "id": index + 1,
            "status": "completed",
            "path": f"scene-{index}.png",
            "content_hash": f"hash-{index}",
            "metadata": {"index": index},
        }
        for index in range(184)
    }
    artifacts["scene:184"] = {
        "id": 185,
        "status": "failed",
        "path": "scene-184.png",
        "content_hash": "hash-184",
        "metadata": {
            "index": 184,
            "terminal": True,
            "reason_code": "flow_max_attempts_exceeded",
            "error": "original failure",
            "submission_count": 5,
            "submission_count_total": 5,
            "manual_retry_epoch": 0,
        },
    }
    artifacts["scene:185"] = {
        "id": 186,
        "status": "completed",
        "path": "scene-185.png",
        "content_hash": "hash-185",
        "metadata": {"index": 185},
    }
    upserts = []
    updated_job_fields = {}

    monkeypatch.setattr(db, "get_system_job", lambda job_id: mock_job)
    monkeypatch.setattr(db, "get_system_job_queue_position", lambda job_id: None)
    monkeypatch.setattr(db, "list_latest_video_artifacts", lambda video_id, prefixes: artifacts)
    monkeypatch.setattr(db, "get_video", lambda video_id: {"id": video_id, "flow_scene_count": 186})
    monkeypatch.setattr(db, "upsert_video_artifact", lambda **kwargs: upserts.append(kwargs))
    monkeypatch.setattr(
        db,
        "update_system_job",
        lambda job_id, **kwargs: updated_job_fields.update(kwargs),
    )
    monkeypatch.setattr(db, "resume_system_job", lambda job_id: {"id": job_id, "status": "queued"})
    monkeypatch.setattr(db, "update_video_production_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_kick_job_queues", lambda job_types: None)

    main._run_system_job_center_action("flow-video-471", "resume", kick_queues=False)

    assert [item["artifact_type"] for item in upserts] == ["scene:184"]
    retried = upserts[0]
    assert retried["status"] == "queued"
    assert retried["metadata"]["submission_count"] == 0
    assert retried["metadata"]["submission_count_total"] == 5
    assert retried["metadata"]["manual_retry_epoch"] == 1
    assert retried["metadata"]["last_error"] == "original failure"
    assert artifacts["scene:185"]["status"] == "completed"
    assert updated_job_fields["payload_json"]["force_new_project"] is False
    assert updated_job_fields["payload_json"]["force_new_flow_project"] is False


def test_video_render_resume_uses_shared_flow_recovery_helper(monkeypatch):
    paused_job = {
        "id": "flow-shared-resume",
        "job_type": "flow_media_generation",
        "status": "paused",
        "video_id": 471,
        "payload": {"video_id": 471},
    }
    resumed_job = {**paused_job, "status": "queued"}
    calls = []
    monkeypatch.setattr(video_production, "get_render_mode", lambda snapshot: "google_flow")
    monkeypatch.setattr(main, "_enqueue_flow_media_job", lambda **kwargs: (paused_job, False))
    monkeypatch.setattr(
        main,
        "_resume_flow_media_job",
        lambda job: calls.append(job["id"]) or resumed_job,
    )
    monkeypatch.setattr(db, "update_video_production_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(db, "get_system_job", lambda job_id: resumed_job)
    monkeypatch.setattr(main, "_kick_job_queues", lambda job_types: None)

    job, created, phase = main._route_video_render_job(
        video_id=471,
        snapshot={},
        mode="resume",
    )

    assert calls == ["flow-shared-resume"]
    assert created is False
    assert phase == "media_generation"
    assert job["status"] == "queued"


@pytest.mark.parametrize(
    "attention_required",
    ["flow_image_quota_exhausted", "flow_agent_limit_reached"],
)
def test_quota_and_agent_limit_resume_do_not_reset_scene_artifacts(
    monkeypatch, attention_required
):
    job = {
        "id": f"flow-{attention_required}",
        "job_type": "flow_media_generation",
        "status": "paused",
        "video_id": 500,
        "result": {"attention_required": attention_required},
        "payload": {"video_id": 500},
    }
    upserts = []
    monkeypatch.setattr(db, "get_system_job", lambda job_id: job)
    monkeypatch.setattr(db, "get_system_job_queue_position", lambda job_id: None)
    monkeypatch.setattr(db, "upsert_video_artifact", lambda **kwargs: upserts.append(kwargs))
    monkeypatch.setattr(db, "update_system_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(db, "resume_system_job", lambda job_id: {**job, "status": "queued"})
    monkeypatch.setattr(db, "update_video_production_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(main, "_kick_job_queues", lambda job_types: None)

    item = main._system_job_center_item(job, None, hydrate=True)
    assert item["can_flow_reset_project"] is False
    assert item["can_flow_fallback_static"] is False
    main._run_system_job_center_action(job["id"], "resume", kick_queues=False)

    assert upserts == []


def test_flow_attention_details_reports_failed_scene_and_progress(monkeypatch):
    artifacts = {
        f"scene:{index}": {
            "status": "completed",
            "metadata": {"index": index},
        }
        for index in range(185)
    }
    artifacts["scene:184"] = {
        "status": "failed",
        "metadata": {
            "index": 184,
            "terminal": True,
            "reason_code": "flow_max_attempts_exceeded",
            "submission_count": 5,
            "manual_retry_epoch": 1,
            "last_error": "safe diagnostic",
        },
    }
    artifacts["scene:185"] = {
        "status": "completed",
        "metadata": {"index": 185},
    }
    monkeypatch.setattr(db, "list_latest_video_artifacts", lambda video_id, prefixes: artifacts)
    monkeypatch.setattr(db, "get_video", lambda video_id: {"flow_scene_count": 186})

    details = main._build_flow_attention_details(
        471,
        {"flow_media_progress": {"images_total": 186}},
    )

    assert details["message"] == "Cảnh 185 thất bại sau 5 lượt gửi; 185/186 ảnh đã hoàn tất."
    assert details["failed_scenes"] == [
        {
            "scene_index": 184,
            "scene_number": 185,
            "reason_code": "flow_max_attempts_exceeded",
            "submission_count": 5,
            "last_error": "safe diagnostic",
        }
    ]
    assert details["can_retry_failed_scenes"] is True
    assert details["can_retry_new_canvas"] is True


def test_job_center_hydration_restores_flow_attention_capabilities(monkeypatch):
    item = {
        "id": "flow-hydrated",
        "type": "flow_media_generation",
        "video_id": 471,
        "status": "paused",
        "can_resume": True,
    }
    details = {
        "kind": "flow_scene_failure",
        "failed_scenes": [{"scene_number": 185}],
        "can_retry_failed_scenes": True,
        "can_retry_new_canvas": False,
    }
    monkeypatch.setattr(main, "_build_flow_attention_details", lambda *args: details)

    main._populate_system_job_payload_result(
        item,
        {"video_id": 471},
        {"attention_required": "flow_image_generation"},
    )

    assert item["attention_details"] == details
    assert item["can_resume"] is False
    assert item["can_retry_failed_scenes"] is True
    assert item["can_flow_reset_project"] is False


def test_flow_scene_resume_without_terminal_artifact_returns_clear_conflict(monkeypatch):
    job = {
        "id": "flow-missing-terminal",
        "job_type": "flow_media_generation",
        "status": "paused",
        "video_id": 471,
        "payload": {"video_id": 471},
        "result": {"attention_required": "flow_image_generation"},
    }
    monkeypatch.setattr(db, "get_system_job", lambda job_id: job)
    monkeypatch.setattr(db, "get_system_job_queue_position", lambda job_id: None)
    monkeypatch.setattr(db, "list_latest_video_artifacts", lambda *args, **kwargs: {})
    monkeypatch.setattr(db, "get_video", lambda video_id: {"id": video_id})

    with pytest.raises(main.JobCenterActionNotAllowedError) as excinfo:
        main._run_system_job_center_action(
            "flow-missing-terminal",
            "resume",
            kick_queues=False,
        )

    assert "Không tìm thấy cảnh ảnh Flow lỗi hợp lệ" in str(excinfo.value)
    assert "Tạo lại từ cảnh" in str(excinfo.value)


def test_render_status_exposes_structured_flow_attention(tmp_path):
    video_id = db.save_video(
        "https://youtube.com/watch?v=flow-attention-api",
        "Flow attention API",
        "Transcript",
        "Script",
    )
    job_id = f"flow-attention-{video_id}"
    try:
        completed_path = tmp_path / "scene-0.png"
        completed_path.write_bytes(b"image")
        db.upsert_video_artifact(
            video_id=video_id,
            artifact_type="scene:0",
            path=str(completed_path),
            content_hash="completed-hash",
            status="completed",
            mime_type="image/png",
            metadata={"index": 0},
        )
        db.upsert_video_artifact(
            video_id=video_id,
            artifact_type="scene:1",
            path=str(tmp_path / "scene-1.png"),
            content_hash="failed-hash",
            status="failed",
            mime_type="image/png",
            metadata={
                "index": 1,
                "terminal": True,
                "reason_code": "flow_max_attempts_exceeded",
                "submission_count": 5,
                "manual_retry_epoch": 0,
                "last_error": "safe failure",
            },
        )
        db.create_system_job(
            job_id=job_id,
            job_type="flow_media_generation",
            video_id=video_id,
            title="Tạo media Google Flow",
            payload={"video_id": video_id, "snapshot": {}},
        )
        db.update_system_job(
            job_id,
            status="paused",
            progress="needs attention",
            result_json={
                "attention_required": "flow_image_generation",
                "flow_media_progress": {"images_total": 2, "images_completed": 1},
            },
        )

        response = main.get_render_status(video_id)

        assert response["state"] == "paused"
        assert response["job"]["attention_required"] == "flow_image_generation"
        assert response["job"]["attention_details"]["kind"] == "flow_scene_failure"
        assert response["job"]["attention_details"]["failed_scenes"][0]["scene_number"] == 2
        assert response["job"]["can_retry_failed_scenes"] is True
        assert response["job"]["can_retry_new_canvas"] is False
    finally:
        db.delete_system_job(job_id)
        db.delete_video(video_id)


def test_startup_reconciles_orphan_render_states_without_enqueue(tmp_path, monkeypatch):
    final_mp4 = tmp_path / "ready.mp4"
    final_mp4.write_bytes(b"valid-mp4")
    videos = [
        {"id": 1, "video_status": "active", "render_status": "running"},
        {"id": 2, "video_status": "active", "render_status": "running"},
        {"id": 221, "video_status": db.VIDEO_STATUS_ERROR, "render_status": "running"},
        {"id": 4, "video_status": "active", "render_status": "running"},
    ]
    terminal_artifact = (
        "scene:184",
        {"status": "failed", "metadata": {"terminal": True}},
    )
    updates = []
    monkeypatch.setattr(db, "list_orphaned_active_render_videos", lambda: videos)
    monkeypatch.setattr(
        main,
        "_flow_terminal_scene_artifacts",
        lambda video_id: [terminal_artifact] if video_id == 1 else [],
    )
    monkeypatch.setattr(
        main,
        "_build_flow_attention_details",
        lambda video_id, result=None: {
            "message": "Cảnh 185 thất bại",
            "failed_scenes": [{"last_error": "safe failure"}],
        },
    )
    monkeypatch.setattr(
        db,
        "get_latest_video_artifact",
        lambda video_id, artifact_type, status=None: (
            {"path": str(final_mp4), "status": "ready"} if video_id == 2 else None
        ),
    )
    monkeypatch.setattr(
        db,
        "update_video_production_state",
        lambda video_id, **kwargs: updates.append((video_id, kwargs)),
    )
    monkeypatch.setattr(
        main,
        "_enqueue_flow_media_job",
        lambda **kwargs: pytest.fail("startup reconcile must not enqueue"),
    )

    counts = main._reconcile_orphaned_video_render_states()

    assert counts == {"paused": 1, "ready": 1, "error": 1, "canceled": 1}
    by_id = dict(updates)
    assert by_id[1]["current_stage"] == "flow_image_needs_attention"
    assert by_id[2]["render_status"] == "ready"
    assert by_id[221]["render_status"] == "error"
    assert by_id[4]["current_stage"] == "interrupted"

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
    upserted_artifacts = []

    def mock_get_system_job(job_id):
        return mock_job

    def mock_update_system_job(job_id, **kwargs):
        pass

    def mock_update_video_production_state(*args, **kwargs):
        pass

    mock_scenes = {
        "scene:10": {"id": 101, "status": "completed", "metadata": {}},
        "scene:11": {"id": 102, "status": "failed", "path": "scene-11.png", "content_hash": "hash-11", "metadata": {"terminal": True, "reason_code": "flow_max_attempts_exceeded", "manual_retry_epoch": 1, "submission_count": 5, "submission_count_total": 10}},
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
    monkeypatch.setattr(db, "upsert_video_artifact", lambda **kwargs: upserted_artifacts.append(kwargs))
    monkeypatch.setattr(db, "request_new_flow_project", mock_request_new)
    monkeypatch.setattr(db, "get_video", lambda vid_id: {"id": vid_id, "flow_scene_count": 12})
    monkeypatch.setattr(main, "_kick_job_queues", lambda x: None)

    main._run_system_job_center_action("flow-test-456", "retry_new_flow_project", kick_queues=False)

    assert deleted_artifacts == []
    assert [item["artifact_type"] for item in upserted_artifacts] == ["scene:11"]
    assert upserted_artifacts[0]["status"] == "queued"
    assert upserted_artifacts[0]["metadata"]["manual_retry_epoch"] == 2
    assert upserted_artifacts[0]["metadata"]["submission_count_total"] == 10
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


def test_retry_new_flow_canvas_requires_current_canvas_retry_first(monkeypatch):
    from fastapi import HTTPException

    terminal_scene = {
        "scene:184": {
            "status": "failed",
            "metadata": {
                "index": 184,
                "terminal": True,
                "reason_code": "flow_max_attempts_exceeded",
                "submission_count": 5,
                "manual_retry_epoch": 0,
            },
        }
    }
    monkeypatch.setattr(
        db,
        "get_video",
        lambda video_id: {
            "id": video_id,
            "video_status": "active",
            "flow_scene_count": 186,
        },
    )
    monkeypatch.setattr(db, "list_system_jobs", lambda video_id, limit=None: [])
    monkeypatch.setattr(
        db,
        "list_latest_video_artifacts",
        lambda video_id, prefixes: terminal_scene,
    )

    with pytest.raises(HTTPException) as exc_info:
        main.retry_new_flow_canvas_for_video(471)

    assert exc_info.value.status_code == 409
    assert "Canvas hiện tại" in str(exc_info.value.detail)


@pytest.mark.parametrize(
    ("keep_successful", "request_body"),
    [
        (True, None),
        (False, main.RetryNewFlowCanvasRequest(keep_successful=False)),
    ],
)
def test_retry_new_flow_canvas_for_video_success_after_completed_job(
    monkeypatch,
    keep_successful,
    request_body,
):
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

    res = main.retry_new_flow_canvas_for_video(379, request_body)

    assert res["success"] is True
    assert res["video_id"] == 379
    assert res["job_id"] == "flow-media-new"
    assert res["purged_count"] == 12
    assert res["keep_successful"] is keep_successful

    # Verify paused job was canceled to avoid duplicate queue conflict
    assert canceled_jobs == ["flow-media-paused"]
    # Default remains compatible while the explicit false path removes all intro clips.
    assert purged_videos == [(379, keep_successful)]
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
    monkeypatch.setattr(video_production, "_scene_video_hash", lambda *args: "test1234abcd")

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
