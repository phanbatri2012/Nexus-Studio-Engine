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
