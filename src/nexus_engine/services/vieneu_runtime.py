"""On-demand lifecycle manager for the local VieNeu speech API."""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from nexus_engine.paths import DATA_DIR, LOGS_DIR, PROJECT_ROOT
from nexus_engine.services import process_registry
from nexus_engine.services.secret_store import write_private_text


logger = logging.getLogger(__name__)

ENV_RUNTIME_ROOT = "NEXUS_VIENEU_ROOT"
PROCESS_KEY = "vieneu-tts-runtime"
STATE_PATH = DATA_DIR / "vieneu_runtime.json"
IDLE_SHUTDOWN_SECONDS = 30.0
STARTUP_TIMEOUT_SECONDS = 120.0
SHUTDOWN_TIMEOUT_SECONDS = 5.0


class VieNeuRuntimeError(RuntimeError):
    """Base error for the managed VieNeu runtime."""


class VieNeuRuntimeConfigurationError(VieNeuRuntimeError):
    """The local VieNeu installation is missing or invalid."""


class VieNeuRuntimeBusy(VieNeuRuntimeError):
    """The configured port is occupied by an incompatible service."""


class VieNeuRuntimeUnavailable(VieNeuRuntimeError):
    """The managed process failed to become ready."""


_LOCK = threading.RLock()
_PROCESS: subprocess.Popen[Any] | None = None
_INSTANCE_ID = ""
_RUNTIME_ROOT: Path | None = None
_LEASES: set[str] = set()
_IDLE_TIMER: threading.Timer | None = None
_IDLE_SHUTDOWN_AT = ""
_STDOUT_HANDLE = None
_STDERR_HANDLE = None


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _runtime_settings() -> dict:
    from nexus_engine.services import vieneu_tts

    try:
        if vieneu_tts.VIENEU_TTS_SETTINGS_PATH.is_file():
            payload = json.loads(
                vieneu_tts.VIENEU_TTS_SETTINGS_PATH.read_text(encoding="utf-8")
            )
            return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}
    return {}


def _api_base_url() -> str:
    from nexus_engine.services import vieneu_tts

    return vieneu_tts.api_base_url()


def _candidate_runtime_roots() -> list[Path]:
    configured = str(
        os.environ.get(ENV_RUNTIME_ROOT)
        or _runtime_settings().get("runtime_root")
        or ""
    ).strip()
    if configured:
        return [Path(configured).expanduser()]

    parent = PROJECT_ROOT.parent
    exact = parent / "VieNeu‑TTS v3_Turbo"
    candidates = [exact]
    try:
        candidates.extend(
            path
            for path in sorted(parent.glob("VieNeu*TTS v3_Turbo"))
            if path != exact
        )
    except OSError:
        pass
    return candidates


def runtime_root() -> Path:
    checked: list[str] = []
    for candidate in _candidate_runtime_roots():
        try:
            root = candidate.resolve()
        except OSError:
            root = candidate.absolute()
        checked.append(str(root))
        runtime_dir = root / "runtime"
        python_executable = root / ".venv" / "Scripts" / "python.exe"
        api_module = runtime_dir / "apps" / "openai_speech.py"
        if root.is_dir() and python_executable.is_file() and api_module.is_file():
            return root
    locations = "; ".join(checked) or "<không có>"
    raise VieNeuRuntimeConfigurationError(
        f"Không tìm thấy runtime VieNeu hợp lệ. Đã kiểm tra: {locations}."
    )


def validate_runtime() -> dict:
    root = runtime_root()
    return {
        "configured": True,
        "runtime_root": str(root),
        "python": str(root / ".venv" / "Scripts" / "python.exe"),
        "api_base_url": _api_base_url(),
    }


def _endpoint() -> tuple[str, int]:
    parsed = urlparse(_api_base_url())
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 80)
    return host, port


def _port_in_use() -> bool:
    host, port = _endpoint()
    connect_host = "127.0.0.1" if host == "localhost" else host
    family = socket.AF_INET6 if ":" in connect_host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex((connect_host, port)) == 0


def _probe_health(*, timeout_seconds: float = 1.0) -> dict:
    request = urllib.request.Request(
        f"{_api_base_url()}/health",
        headers={"Accept": "application/json", "User-Agent": "NexusStudio/1.0"},
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(0.25, float(timeout_seconds)),
        ) as response:
            payload = json.loads(response.read(256 * 1024).decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise VieNeuRuntimeUnavailable("VieNeu API chưa sẵn sàng.") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise VieNeuRuntimeUnavailable("VieNeu API trả health không sẵn sàng.")
    return payload


def _cancel_idle_timer_locked() -> None:
    global _IDLE_TIMER, _IDLE_SHUTDOWN_AT
    timer = _IDLE_TIMER
    _IDLE_TIMER = None
    _IDLE_SHUTDOWN_AT = ""
    if timer is not None:
        timer.cancel()


def _close_log_handles_locked() -> None:
    global _STDOUT_HANDLE, _STDERR_HANDLE
    for handle in (_STDOUT_HANDLE, _STDERR_HANDLE):
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
    _STDOUT_HANDLE = None
    _STDERR_HANDLE = None


def _remove_state() -> None:
    try:
        STATE_PATH.unlink(missing_ok=True)
    except OSError:
        logger.warning("Không thể xóa state marker VieNeu: %s", STATE_PATH)


def _write_state(process: subprocess.Popen[Any], root: Path) -> None:
    payload = {
        "schema_version": 1,
        "owned": True,
        "pid": int(process.pid),
        "instance_id": _INSTANCE_ID,
        "runtime_root": str(root),
        "python": str(root / ".venv" / "Scripts" / "python.exe"),
        "module": "apps.openai_speech",
        "api_base_url": _api_base_url(),
        "started_at": _utc_now(),
    }
    write_private_text(
        STATE_PATH,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )


def _process_is_alive(process: subprocess.Popen[Any] | None) -> bool:
    return bool(process is not None and process.poll() is None)


def _launch_environment(root: Path) -> dict[str, str]:
    runtime_dir = root / "runtime"
    environment = dict(os.environ)
    host, port = _endpoint()
    python_path_parts = [str(runtime_dir / "src"), str(runtime_dir)]
    existing_python_path = str(environment.get("PYTHONPATH") or "").strip()
    if existing_python_path:
        python_path_parts.append(existing_python_path)
    environment.update(
        {
            "VIENEU_ROOT": str(root),
            "VIENEU_BACKEND": "onnx",
            "VIENEU_DEVICE": "cpu",
            "VIENEU_PRECISION": "fp32",
            "VIENEU_BACKBONE_DIR": str(root / "models" / "vieneu-v3-turbo"),
            "VIENEU_ONNX_DIR": str(
                root / "models" / "vieneu-v3-turbo" / "onnx_update"
            ),
            "VIENEU_CODEC_DIR": str(root / "models" / "moss-audio-tokenizer"),
            "VIENEU_HOME": str(root / "voices"),
            "HF_HOME": str(root / "cache"),
            "HF_HUB_CACHE": str(root / "cache" / "hub"),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HOST": host,
            "PORT": str(port),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONPATH": os.pathsep.join(python_path_parts),
        }
    )
    return environment


def _start_owned_process(root: Path) -> subprocess.Popen[Any]:
    global _STDOUT_HANDLE, _STDERR_HANDLE
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    _STDOUT_HANDLE = open(
        LOGS_DIR / "vieneu.current.stdout.log", "a", encoding="utf-8"
    )
    _STDERR_HANDLE = open(
        LOGS_DIR / "vieneu.current.stderr.log", "a", encoding="utf-8"
    )
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    try:
        return subprocess.Popen(
            [
                str(root / ".venv" / "Scripts" / "python.exe"),
                "-m",
                "apps.openai_speech",
            ],
            cwd=str(root / "runtime"),
            env=_launch_environment(root),
            stdin=subprocess.DEVNULL,
            stdout=_STDOUT_HANDLE,
            stderr=_STDERR_HANDLE,
            creationflags=creationflags,
        )
    except Exception:
        _close_log_handles_locked()
        raise


def _runtime_snapshot_locked() -> dict:
    process = _PROCESS
    owned = _process_is_alive(process)
    return {
        "configured": False,
        "running": owned,
        "ready": False,
        "owned": owned,
        "external": False,
        "pid": int(process.pid) if owned and process is not None else 0,
        "instance_id": _INSTANCE_ID if owned else "",
        "runtime_root": str(_RUNTIME_ROOT or ""),
        "api_base_url": "",
        "state": "starting" if owned else "idle",
        "active_lease_count": len(_LEASES),
        "idle_shutdown_at": _IDLE_SHUTDOWN_AT,
        "idle_timeout_seconds": int(IDLE_SHUTDOWN_SECONDS),
        "error": "",
    }


def status(*, probe: bool = True) -> dict:
    with _LOCK:
        result = _runtime_snapshot_locked()
    try:
        validated = validate_runtime()
        result.update(validated)
    except VieNeuRuntimeError as exc:
        result["state"] = "unconfigured"
        result["error"] = str(exc)
        return result

    port_in_use = _port_in_use()
    if not port_in_use:
        if result["owned"]:
            result["state"] = "starting"
        else:
            result["state"] = "idle"
        return result

    result["running"] = True
    result["external"] = not result["owned"]
    if not probe:
        result["state"] = "running" if result["owned"] else "external"
        return result
    try:
        health = _probe_health(timeout_seconds=1.0)
        result.update(
            {
                "ready": True,
                "state": "ready" if result["owned"] else "external_ready",
                "backend": str(health.get("backend") or ""),
                "sample_rate": int(health.get("sample_rate") or 0),
                "active": int(health.get("active") or 0),
                "waiting": int(health.get("waiting") or 0),
            }
        )
    except VieNeuRuntimeError as exc:
        result["state"] = "conflict" if result["external"] else "starting"
        result["error"] = str(exc)
    return result


def ensure_started(*, timeout_seconds: float = STARTUP_TIMEOUT_SECONDS) -> dict:
    global _PROCESS, _INSTANCE_ID, _RUNTIME_ROOT
    timeout_seconds = max(1.0, min(float(timeout_seconds), STARTUP_TIMEOUT_SECONDS))
    with _LOCK:
        _cancel_idle_timer_locked()
        root = runtime_root()
        process = _PROCESS if _process_is_alive(_PROCESS) else None
        if process is None:
            if _PROCESS is not None:
                process_registry.unregister_process(PROCESS_KEY, _PROCESS)
                _PROCESS = None
                _close_log_handles_locked()
                _remove_state()

            if _port_in_use():
                try:
                    health = _probe_health(timeout_seconds=min(2.0, timeout_seconds))
                except VieNeuRuntimeError as exc:
                    raise VieNeuRuntimeBusy(
                        "Cổng VieNeu đang bị tiến trình không tương thích chiếm dụng."
                    ) from exc
                return {
                    "configured": True,
                    "running": True,
                    "ready": True,
                    "owned": False,
                    "external": True,
                    "pid": 0,
                    "instance_id": "",
                    "runtime_root": str(root),
                    "api_base_url": _api_base_url(),
                    "state": "external_ready",
                    "active_lease_count": len(_LEASES),
                    "idle_shutdown_at": "",
                    "idle_timeout_seconds": int(IDLE_SHUTDOWN_SECONDS),
                    "backend": str(health.get("backend") or ""),
                    "sample_rate": int(health.get("sample_rate") or 0),
                    "error": "",
                }

            _RUNTIME_ROOT = root
            _INSTANCE_ID = uuid.uuid4().hex
            _PROCESS = _start_owned_process(root)
            process_registry.register_process(PROCESS_KEY, _PROCESS)
            _write_state(_PROCESS, root)
            process = _PROCESS

    deadline = time.monotonic() + timeout_seconds
    max_attempts = max(1, int(math.ceil(timeout_seconds / 0.25)))
    last_error: Exception | None = None
    for _attempt in range(max_attempts):
        if time.monotonic() >= deadline:
            break
        if process.poll() is not None:
            last_error = VieNeuRuntimeUnavailable(
                f"VieNeu API dừng trong lúc khởi động (exit code {process.returncode})."
            )
            break
        try:
            health = _probe_health(timeout_seconds=0.75)
            with _LOCK:
                result = _runtime_snapshot_locked()
            result.update(
                {
                    "configured": True,
                    "ready": True,
                    "state": "ready",
                    "api_base_url": _api_base_url(),
                    "backend": str(health.get("backend") or ""),
                    "sample_rate": int(health.get("sample_rate") or 0),
                    "error": "",
                }
            )
            logger.info("VieNeu API ready pid=%s instance_id=%s", process.pid, _INSTANCE_ID)
            return result
        except VieNeuRuntimeError as exc:
            last_error = exc
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    shutdown_owned_service(force=True)
    raise VieNeuRuntimeUnavailable(
        "VieNeu API không sẵn sàng trong thời gian cho phép; xem vieneu.current.stderr.log."
    ) from last_error


class VieNeuRuntimeLease:
    def __init__(self, token: str, runtime_status: dict) -> None:
        self._token = token
        self.status = runtime_status
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        release(self._token)

    def __enter__(self) -> "VieNeuRuntimeLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.close()


def acquire(*, timeout_seconds: float = STARTUP_TIMEOUT_SECONDS) -> VieNeuRuntimeLease:
    token = uuid.uuid4().hex
    runtime_status = ensure_started(timeout_seconds=timeout_seconds)
    with _LOCK:
        _cancel_idle_timer_locked()
        _LEASES.add(token)
    return VieNeuRuntimeLease(token, runtime_status)


def _idle_shutdown_callback() -> None:
    global _IDLE_TIMER, _IDLE_SHUTDOWN_AT
    with _LOCK:
        _IDLE_TIMER = None
        _IDLE_SHUTDOWN_AT = ""
        if _LEASES:
            return
    shutdown_owned_service()


def release(token: str) -> None:
    global _IDLE_TIMER, _IDLE_SHUTDOWN_AT
    with _LOCK:
        _LEASES.discard(str(token or ""))
        if _LEASES or not _process_is_alive(_PROCESS):
            return
        _cancel_idle_timer_locked()
        shutdown_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=IDLE_SHUTDOWN_SECONDS
        )
        _IDLE_SHUTDOWN_AT = shutdown_at.isoformat()
        timer = threading.Timer(IDLE_SHUTDOWN_SECONDS, _idle_shutdown_callback)
        timer.daemon = True
        _IDLE_TIMER = timer
        timer.start()


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(int(process.pid))],
            capture_output=True,
            timeout=SHUTDOWN_TIMEOUT_SECONDS,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        process.kill()


def shutdown_owned_service(*, force: bool = False) -> dict:
    global _PROCESS, _INSTANCE_ID, _RUNTIME_ROOT
    with _LOCK:
        if _LEASES and not force:
            result = _runtime_snapshot_locked()
            result["stop_blocked"] = True
            return result
        _cancel_idle_timer_locked()
        process = _PROCESS

    if process is not None:
        try:
            if process.poll() is None:
                logger.info("Stopping owned VieNeu API pid=%s", process.pid)
                process.terminate()
                try:
                    process.wait(timeout=SHUTDOWN_TIMEOUT_SECONDS)
                except subprocess.TimeoutExpired:
                    _terminate_process_tree(process)
        finally:
            process_registry.unregister_process(PROCESS_KEY, process)

    with _LOCK:
        _PROCESS = None
        _INSTANCE_ID = ""
        _RUNTIME_ROOT = None
        _LEASES.clear()
        _close_log_handles_locked()
        _remove_state()
    return status(probe=False)
