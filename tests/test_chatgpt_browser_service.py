from __future__ import annotations

import json

from nexus_engine.services import chatgpt_browser_service as service


def _write_state(path, *, ready: bool) -> None:
    path.write_text(
        json.dumps(
            {
                "pid": 1234,
                "instance_id": "test-instance",
                "cdp_url": "http://127.0.0.1:9222",
                "ready": ready,
                "window_visible": False,
            }
        ),
        encoding="utf-8",
    )


def test_cdp_health_does_not_publish_service_before_owner_is_ready(
    tmp_path,
    monkeypatch,
):
    state_path = tmp_path / "chatgpt-browser.json"
    _write_state(state_path, ready=False)
    monkeypatch.setattr(service, "SERVICE_STATE_PATH", state_path)
    monkeypatch.setattr(service, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(service, "_cdp_is_ready", lambda _endpoint: True)

    status = service._read_browser_service_status()

    assert status["process_alive"] is True
    assert status["ready"] is False
    assert status["connected"] is False
    assert status["state"] == "starting"
    assert service.get_browser_service_endpoint() == ""


def test_ready_owner_publishes_healthy_cdp_endpoint(tmp_path, monkeypatch):
    state_path = tmp_path / "chatgpt-browser.json"
    _write_state(state_path, ready=True)
    monkeypatch.setattr(service, "SERVICE_STATE_PATH", state_path)
    monkeypatch.setattr(service, "_pid_is_alive", lambda _pid: True)
    monkeypatch.setattr(service, "_cdp_is_ready", lambda _endpoint: True)

    status = service._read_browser_service_status()

    assert status["ready"] is True
    assert status["connected"] is True
    assert service.get_browser_service_endpoint() == "http://127.0.0.1:9222"
