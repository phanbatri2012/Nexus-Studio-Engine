import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from nexus_engine.services.google_flow_worker import (
    FlowImageNeedsAttention,
    FlowMediaPending,
    FlowImagePending,
)
from nexus_engine.services import video_production


def test_scene_max_attempts_exceeded_fails_and_marks_db():
    mock_db = MagicMock()
    mock_scene = {"index": 11, "name": "Scene 12"}
    existing_meta = {"generation_attempt": 5}
    existing_artifact = {
        "content_hash": "hash123",
        "status": "queued",
        "metadata": existing_meta,
    }

    with patch.object(video_production, "db", mock_db), \
         patch.object(video_production, "_cached_reference_hash", return_value="refhash"), \
         patch.object(video_production, "_scene_hash", return_value="hash123"):
        
        with pytest.raises(FlowImageNeedsAttention) as excinfo:
            import asyncio
            asyncio.run(
                video_production._generate_scene_image_async(
                    video_id=999,
                    scene=mock_scene,
                    scene_count=20,
                    profile={},
                    settings={},
                    reference_path=None,
                    reference_id="",
                    progress=MagicMock(),
                    cancel_check=MagicMock(),
                    worker=MagicMock(),
                    artifact_snapshot={"scene:11": existing_artifact},
                )
            )

        assert "thất bại sau 5 lượt gửi Google Flow" in str(excinfo.value)
        # Verify db.upsert_video_artifact was called with status="failed" and terminal=True
        mock_db.upsert_video_artifact.assert_called_once()
        _, kwargs = mock_db.upsert_video_artifact.call_args
        assert kwargs["video_id"] == 999
        assert kwargs["artifact_type"] == "scene:11"
        assert kwargs["status"] == "failed"
        assert kwargs["metadata"]["terminal"] is True
        assert kwargs["metadata"]["reason_code"] == "flow_max_attempts_exceeded"
        assert kwargs["metadata"]["submission_count"] == 5
        assert len(kwargs["metadata"]["failure_history"]) == 1


def test_cached_terminal_failed_scene_raises_immediately():
    mock_db = MagicMock()
    mock_scene = {"index": 11, "name": "Scene 12"}
    existing_meta = {
        "generation_attempt": 6,
        "terminal": True,
        "reason_code": "flow_max_attempts_exceeded",
        "error": "Cảnh 12 đã vượt quá giới hạn thử tối đa (5 lần).",
    }
    existing_artifact = {
        "content_hash": "hash123",
        "status": "failed",
        "metadata": existing_meta,
    }

    with patch.object(video_production, "db", mock_db), \
         patch.object(video_production, "_cached_reference_hash", return_value="refhash"), \
         patch.object(video_production, "_scene_hash", return_value="hash123"):
        
        with pytest.raises(FlowImageNeedsAttention) as excinfo:
            import asyncio
            asyncio.run(
                video_production._generate_scene_image_async(
                    video_id=999,
                    scene=mock_scene,
                    scene_count=20,
                    profile={},
                    settings={},
                    reference_path=None,
                    reference_id="",
                    progress=MagicMock(),
                    cancel_check=MagicMock(),
                    worker=MagicMock(),
                    artifact_snapshot={"scene:11": existing_artifact},
                )
            )

        assert "vượt quá giới hạn thử tối đa" in str(excinfo.value)
        # Should not upsert again
        mock_db.upsert_video_artifact.assert_not_called()



def test_force_new_flow_project_bypasses_cached_terminal_failure():
    from unittest.mock import AsyncMock
    mock_db = MagicMock()
    mock_scene = {"index": 11, "name": "Scene 12"}
    existing_meta = {
        "generation_attempt": 6,
        "terminal": True,
        "reason_code": "flow_max_attempts_exceeded",
        "error": "Cảnh 12 đã vượt quá giới hạn thử tối đa (5 lần).",
    }
    existing_artifact = {
        "content_hash": "hash123",
        "status": "failed",
        "metadata": existing_meta,
    }
    mock_worker = MagicMock()
    from nexus_engine.services.google_flow_worker import FlowImagePending
    mock_worker.generate_scene = AsyncMock(side_effect=FlowImagePending("Simulated submission", reason_code="pending"))

    with patch.object(video_production, "db", mock_db), \
         patch.object(video_production, "_cached_reference_hash", return_value="refhash"), \
         patch.object(video_production, "_scene_hash", return_value="hash123"), \
         patch.object(video_production, "SCENES_DIR", Path("e:/yt/Tool/Nexus_Studio_Engine/data/scenes")):

        with pytest.raises(FlowImagePending):
            import asyncio
            asyncio.run(
                video_production._generate_scene_image_async(
                    video_id=999,
                    scene=mock_scene,
                    scene_count=20,
                    profile={},
                    settings={},
                    reference_path=None,
                    reference_id="",
                    progress=MagicMock(),
                    cancel_check=MagicMock(),
                    worker=mock_worker,
                    force_new_flow_project=True,
                    artifact_snapshot={"scene:11": existing_artifact},
                )
            )

def test_circuit_breaker_stagnant_reconciles_pauses_job():
    from nexus_engine.main import _handle_production_job_error

    mock_db = MagicMock()
    mock_db.list_latest_video_artifacts.return_value = {
        "scene:0": {"status": "completed"},
        "scene:1": {"status": "completed"},
    }
    mock_db.get_youtube_publish_workflow_by_job.return_value = None

    job = {
        "id": "flow-media-test-123",
        "job_type": "flow_media_generation",
        "video_id": 379,
        "result": {
            "reconcile_last_completed_count": 2,
            "reconcile_stagnant_count": 14,  # will reach 15
        },
        "payload": {"video_id": 379},
    }

    mock_db.get_system_job.return_value = job

    exc = FlowImagePending(
        "Google Flow còn 2 ảnh đang chờ",
        retry_delay_seconds=30.0,
        pending_image_scene_indices=[11, 13],
    )

    with patch("nexus_engine.main.db", mock_db):
        _handle_production_job_error(job, exc)

    # Verify job was paused
    mock_db.update_system_job.assert_called()
    call_args = mock_db.update_system_job.call_args[1]
    assert call_args["status"] == "paused"
    assert call_args["result_json"]["attention_required"] == "flow_reconcile_stalled"
    assert "15 lần liên tiếp mà không có ảnh/video mới" in call_args["progress"]

    # Verify video state was updated
    mock_db.update_video_production_state.assert_called_with(
        379,
        render_status="paused",
        current_stage="flow_reconcile_stalled",
        production_progress=call_args["progress"],
        blocking_reason=call_args["error"],
    )


def test_submission_budget_counts_actual_requests_and_is_bounded():
    metadata = {"submission_count": 0, "submission_count_total": 9}
    for expected in range(1, 6):
        metadata, count = video_production._reserve_flow_image_submission(metadata)
        assert count == expected
    assert metadata["submission_count_total"] == 14
    with pytest.raises(FlowImageNeedsAttention) as excinfo:
        video_production._reserve_flow_image_submission(metadata)
    assert excinfo.value.reason_code == "flow_max_attempts_exceeded"


def test_manual_retry_epoch_adds_new_current_canvas_composition():
    prompt = video_production._build_manual_recovery_prompt(
        "Base prompt",
        {"action": "subject walks forward"},
        1,
    )
    assert "Manual recovery composition 1" in prompt
    assert "current Canvas" in prompt
    assert "subject walks forward" in prompt


def test_failure_history_is_redacted_and_limited_to_five():
    metadata = {}
    for index in range(7):
        metadata = video_production._flow_failure_metadata(
            metadata,
            RuntimeError(
                f"failure {index} authorization: bearer secret-{index} "
                f"prompt=private visual prompt {index}"
            ),
            stage="submit",
            reason_code="flow_submission_error",
        )
    assert len(metadata["failure_history"]) == 5
    assert "secret-" not in str(metadata["failure_history"])
    assert "private visual prompt" not in str(metadata["failure_history"])
    assert "[REDACTED]" in str(metadata["failure_history"])


@pytest.mark.anyio
async def test_reconciliation_does_not_increase_submission_count(tmp_path, monkeypatch):
    future = "2999-01-01T00:00:00+00:00"
    artifact = {
        "content_hash": "hash123",
        "status": "queued",
        "metadata": {
            "submission_count": 3,
            "submission_count_total": 8,
            "generation_attempt": 3,
            "flow_receipt": {
                "request_key": "existing-request",
                "deadline_at": future,
                "submitted_at": "2026-01-01T00:00:00+00:00",
            },
        },
    }

    class PendingWorker:
        generate_scene = AsyncMock(
            side_effect=FlowImagePending(
                "still pending",
                reason_code="flow_image_pending",
                receipt=artifact["metadata"]["flow_receipt"],
            )
        )

    mock_db = MagicMock()
    mock_db.utc_now.return_value = "2026-01-01T00:00:00+00:00"
    monkeypatch.setattr(video_production, "db", mock_db)
    monkeypatch.setattr(video_production, "SCENES_DIR", tmp_path)
    monkeypatch.setattr(video_production, "_cached_reference_hash", lambda *args: "ref")
    monkeypatch.setattr(video_production, "_scene_hash", lambda *args: "hash123")
    monkeypatch.setattr(video_production, "_flow_mock_enabled", lambda: False)

    with pytest.raises(FlowImagePending):
        await video_production._generate_scene_image_async(
            video_id=7,
            scene={"index": 2, "prompt": "scene"},
            scene_count=3,
            profile={},
            settings={},
            reference_path=None,
            reference_id="",
            progress=lambda *args: None,
            cancel_check=lambda: None,
            worker=PendingWorker(),
            artifact_snapshot={"scene:2": artifact},
        )

    final_metadata = mock_db.upsert_video_artifact.call_args.kwargs["metadata"]
    assert final_metadata["submission_count"] == 3
    assert final_metadata["submission_count_total"] == 8
    assert PendingWorker.generate_scene.await_count == 1


@pytest.mark.anyio
async def test_duplicate_retry_loop_is_finite_and_counts_each_submission(tmp_path, monkeypatch):
    from PIL import Image

    duplicate = tmp_path / "duplicate.png"
    Image.new("RGB", (1024, 576), color=(20, 40, 60)).save(duplicate)
    duplicate_hash = video_production._sha256_file(duplicate)

    class DuplicateWorker:
        def __init__(self):
            self.generate_scene = AsyncMock(return_value="asset://duplicate")

        async def download_image(self, asset_url, target):
            Image.new("RGB", (1024, 576), color=(20, 40, 60)).save(target)

    worker = DuplicateWorker()
    mock_db = MagicMock()
    monkeypatch.setattr(video_production, "db", mock_db)
    monkeypatch.setattr(video_production, "SCENES_DIR", tmp_path)
    monkeypatch.setattr(video_production, "_cached_reference_hash", lambda *args: "ref")
    monkeypatch.setattr(video_production, "_scene_hash", lambda *args: "hash456")
    monkeypatch.setattr(video_production, "_flow_mock_enabled", lambda: False)

    with pytest.raises(video_production.SceneVisualDuplicateError):
        await video_production._generate_scene_image_async(
            video_id=8,
            scene={"index": 0, "prompt": "scene"},
            scene_count=1,
            profile={},
            settings={},
            reference_path=None,
            reference_id="",
            progress=lambda *args: None,
            cancel_check=lambda: None,
            worker=worker,
            existing_hashes={duplicate_hash},
            artifact_snapshot={},
        )

    assert worker.generate_scene.await_count == video_production.MAX_PERCEPTUAL_DUPLICATE_RETRIES + 1
    failed_calls = [
        call for call in mock_db.upsert_video_artifact.call_args_list
        if call.kwargs.get("status") == "failed"
    ]
    assert failed_calls[-1].kwargs["metadata"]["submission_count"] == 3


@pytest.mark.anyio
async def test_safe_prompt_retry_counts_as_second_submission(tmp_path, monkeypatch):
    from PIL import Image
    from nexus_engine.services.google_flow_worker import FlowGenerationError

    class SafePromptWorker:
        def __init__(self):
            self.generate_scene = AsyncMock(
                side_effect=[FlowGenerationError("explicit rejection"), "asset://safe"]
            )

        async def download_image(self, asset_url, target):
            Image.new("RGB", (1024, 576), color=(90, 120, 150)).save(target)

    worker = SafePromptWorker()
    mock_db = MagicMock()
    monkeypatch.setattr(video_production, "db", mock_db)
    monkeypatch.setattr(video_production, "SCENES_DIR", tmp_path)
    monkeypatch.setattr(video_production, "_cached_reference_hash", lambda *args: "ref")
    monkeypatch.setattr(video_production, "_scene_hash", lambda *args: "hash-safe")
    monkeypatch.setattr(video_production, "_flow_mock_enabled", lambda: False)

    result = await video_production._generate_scene_image_async(
        video_id=10,
        scene={"index": 0, "prompt": "scene", "action": "action"},
        scene_count=1,
        profile={},
        settings={},
        reference_path=None,
        reference_id="",
        progress=lambda *args: None,
        cancel_check=lambda: None,
        worker=worker,
        artifact_snapshot={},
    )

    assert Path(result).is_file()
    assert worker.generate_scene.await_count == 2
    completed_call = [
        call for call in mock_db.upsert_video_artifact.call_args_list
        if call.kwargs.get("status") == "completed"
    ][-1]
    assert completed_call.kwargs["metadata"]["submission_count"] == 2
    assert completed_call.kwargs["metadata"]["submission_count_total"] == 2


@pytest.mark.anyio
async def test_cdp_disconnect_keeps_checkpoint_and_enters_bounded_backoff(tmp_path, monkeypatch):
    from playwright.async_api import Error as PlaywrightError

    class DisconnectedWorker:
        generate_scene = AsyncMock(
            side_effect=PlaywrightError("Target page, context or browser has been closed")
        )

    mock_db = MagicMock()
    monkeypatch.setattr(video_production, "db", mock_db)
    monkeypatch.setattr(video_production, "SCENES_DIR", tmp_path)
    monkeypatch.setattr(video_production, "_cached_reference_hash", lambda *args: "ref")
    monkeypatch.setattr(video_production, "_scene_hash", lambda *args: "hash789")
    monkeypatch.setattr(video_production, "_flow_mock_enabled", lambda: False)

    with pytest.raises(FlowImagePending):
        await video_production._generate_scene_image_async(
            video_id=9,
            scene={"index": 0, "prompt": "scene"},
            scene_count=1,
            profile={},
            settings={},
            reference_path=None,
            reference_id="",
            progress=lambda *args: None,
            cancel_check=lambda: None,
            worker=DisconnectedWorker(),
            artifact_snapshot={},
        )

    retry_call = [
        call for call in mock_db.upsert_video_artifact.call_args_list
        if call.kwargs.get("status") == "retry_wait"
    ][-1]
    metadata = retry_call.kwargs["metadata"]
    assert metadata["submission_count"] == 1
    assert metadata["reason_code"] == "flow_cdp_disconnected"
    assert metadata["flow_submission_intent"]["request_key"]
    assert DisconnectedWorker.generate_scene.await_count == 1
