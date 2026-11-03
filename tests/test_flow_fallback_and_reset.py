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

def test_flow_media_resume_resets_stagnant_count(monkeypatch):
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
    def mock_get_system_job(job_id):
        return mock_job

    def mock_update_system_job(job_id, **kwargs):
        if "result_json" in kwargs:
            updated_result.update(kwargs["result_json"])

    def mock_resume_system_job(job_id):
        return {"id": job_id, "status": "queued"}

    def mock_update_video_production_state(*args, **kwargs):
        pass

    monkeypatch.setattr(db, "get_system_job", mock_get_system_job)
    monkeypatch.setattr(db, "update_system_job", mock_update_system_job)
    monkeypatch.setattr(db, "resume_system_job", mock_resume_system_job)
    monkeypatch.setattr(db, "update_video_production_state", mock_update_video_production_state)
    monkeypatch.setattr(main, "_kick_job_queues", lambda x: None)

    main._run_system_job_center_action("flow-test-123", "resume", kick_queues=False)

    assert updated_result.get("reconcile_stagnant_count") == 0
    assert "attention_required" not in updated_result
