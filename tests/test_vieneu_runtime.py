from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nexus_engine.services import vieneu_runtime, vieneu_tts_worker


class _FakeProcess:
    def __init__(self, pid: int = 4321):
        self.pid = pid
        self.returncode = None

    def poll(self):
        return self.returncode

    def send_signal(self, _signal):
        self.returncode = 0

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode


class _FakeTimer:
    instances = []

    def __init__(self, interval, callback):
        self.interval = interval
        self.callback = callback
        self.daemon = False
        self.started = False
        self.canceled = False
        self.__class__.instances.append(self)

    def start(self):
        self.started = True

    def cancel(self):
        self.canceled = True


class VieNeuRuntimeTests(unittest.TestCase):
    def setUp(self):
        _FakeTimer.instances.clear()
        with vieneu_runtime._LOCK:
            vieneu_runtime._cancel_idle_timer_locked()
            vieneu_runtime._PROCESS = None
            vieneu_runtime._INSTANCE_ID = ""
            vieneu_runtime._RUNTIME_ROOT = None
            vieneu_runtime._LEASES.clear()
            vieneu_runtime._close_log_handles_locked()

    def tearDown(self):
        with vieneu_runtime._LOCK:
            vieneu_runtime._cancel_idle_timer_locked()
            vieneu_runtime._PROCESS = None
            vieneu_runtime._INSTANCE_ID = ""
            vieneu_runtime._RUNTIME_ROOT = None
            vieneu_runtime._LEASES.clear()
            vieneu_runtime._close_log_handles_locked()

    def _runtime_root(self, parent: Path) -> Path:
        root = parent / "VieNeu-TTS v3_Turbo"
        (root / ".venv" / "Scripts").mkdir(parents=True)
        (root / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
        (root / "runtime" / "apps").mkdir(parents=True)
        (root / "runtime" / "apps" / "openai_speech.py").write_text(
            "# test\n", encoding="utf-8"
        )
        return root

    def test_status_reports_idle_without_starting_process(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = self._runtime_root(Path(temp_dir))
            with (
                patch.object(vieneu_runtime, "runtime_root", return_value=root),
                patch.object(
                    vieneu_runtime,
                    "_api_base_url",
                    return_value="http://127.0.0.1:8000",
                ),
                patch.object(vieneu_runtime, "_port_in_use", return_value=False),
                patch.object(vieneu_runtime, "_start_owned_process") as start_process,
            ):
                status = vieneu_runtime.status()

        self.assertTrue(status["configured"])
        self.assertEqual(status["state"], "idle")
        self.assertFalse(status["running"])
        start_process.assert_not_called()

    def test_multiple_leases_reuse_one_process_and_schedule_one_idle_stop(self):
        fake_process = _FakeProcess()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = self._runtime_root(Path(temp_dir))
            with (
                patch.object(vieneu_runtime, "runtime_root", return_value=root),
                patch.object(
                    vieneu_runtime,
                    "_api_base_url",
                    return_value="http://127.0.0.1:8000",
                ),
                patch.object(vieneu_runtime, "_port_in_use", return_value=False),
                patch.object(
                    vieneu_runtime,
                    "_start_owned_process",
                    return_value=fake_process,
                ) as start_process,
                patch.object(
                    vieneu_runtime,
                    "_probe_health",
                    return_value={"status": "ok", "backend": "onnx", "sample_rate": 48000},
                ),
                patch.object(vieneu_runtime, "_write_state"),
                patch.object(vieneu_runtime.process_registry, "register_process"),
                patch.object(vieneu_runtime.process_registry, "unregister_process"),
                patch.object(vieneu_runtime.threading, "Timer", _FakeTimer),
            ):
                first = vieneu_runtime.acquire(timeout_seconds=1)
                second = vieneu_runtime.acquire(timeout_seconds=1)
                self.assertEqual(vieneu_runtime.status(probe=False)["active_lease_count"], 2)
                first.close()
                self.assertEqual(_FakeTimer.instances, [])
                second.close()

        start_process.assert_called_once_with(root)
        self.assertEqual(len(_FakeTimer.instances), 1)
        self.assertTrue(_FakeTimer.instances[0].started)
        self.assertGreater(_FakeTimer.instances[0].interval, 0)
        with patch.object(vieneu_runtime, "shutdown_owned_service") as shutdown:
            _FakeTimer.instances[0].callback()
        shutdown.assert_called_once_with()

    def test_owned_process_without_listener_reports_starting(self):
        fake_process = _FakeProcess()
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = self._runtime_root(Path(temp_dir))
            vieneu_runtime._PROCESS = fake_process
            vieneu_runtime._RUNTIME_ROOT = root
            with (
                patch.object(vieneu_runtime, "runtime_root", return_value=root),
                patch.object(
                    vieneu_runtime,
                    "_api_base_url",
                    return_value="http://127.0.0.1:8000",
                ),
                patch.object(vieneu_runtime, "_port_in_use", return_value=False),
            ):
                status = vieneu_runtime.status()

        self.assertEqual(status["state"], "starting")
        self.assertTrue(status["running"])
        self.assertTrue(status["owned"])

    def test_external_ready_service_is_used_but_never_scheduled_for_stop(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = self._runtime_root(Path(temp_dir))
            with (
                patch.object(vieneu_runtime, "runtime_root", return_value=root),
                patch.object(
                    vieneu_runtime,
                    "_api_base_url",
                    return_value="http://127.0.0.1:8000",
                ),
                patch.object(vieneu_runtime, "_port_in_use", return_value=True),
                patch.object(
                    vieneu_runtime,
                    "_probe_health",
                    return_value={"status": "ok", "backend": "onnx", "sample_rate": 48000},
                ),
                patch.object(vieneu_runtime, "_start_owned_process") as start_process,
                patch.object(vieneu_runtime.threading, "Timer", _FakeTimer),
            ):
                lease = vieneu_runtime.acquire(timeout_seconds=1)
                self.assertTrue(lease.status["external"])
                self.assertFalse(lease.status["owned"])
                lease.close()

        start_process.assert_not_called()
        self.assertEqual(_FakeTimer.instances, [])

    def test_incompatible_port_owner_fails_closed(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            root = self._runtime_root(Path(temp_dir))
            with (
                patch.object(vieneu_runtime, "runtime_root", return_value=root),
                patch.object(vieneu_runtime, "_port_in_use", return_value=True),
                patch.object(
                    vieneu_runtime,
                    "_probe_health",
                    side_effect=vieneu_runtime.VieNeuRuntimeUnavailable("bad health"),
                ),
                patch.object(vieneu_runtime, "_start_owned_process") as start_process,
            ):
                with self.assertRaises(vieneu_runtime.VieNeuRuntimeBusy):
                    vieneu_runtime.acquire(timeout_seconds=1)

        start_process.assert_not_called()

    def test_worker_holds_runtime_lease_around_provider_execution(self):
        events = []

        class Lease:
            def __enter__(self):
                events.append("enter")
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                events.append("exit")

        with (
            patch.object(vieneu_tts_worker.db, "update_system_job"),
            patch.object(vieneu_tts_worker.vieneu_runtime, "acquire", return_value=Lease()),
            patch.object(
                vieneu_tts_worker,
                "_execute_with_runtime",
                side_effect=lambda _job: events.append("execute"),
            ),
        ):
            vieneu_tts_worker._execute(
                {"id": "11111111-1111-1111-1111-111111111111"}
            )

        self.assertEqual(events, ["enter", "execute", "exit"])


if __name__ == "__main__":
    unittest.main()
