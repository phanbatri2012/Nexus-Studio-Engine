"""Safe error reporting helpers that avoid leaking local secrets to the UI."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
import re
import threading
import traceback
import uuid

from nexus_engine.paths import DATA_DIR


LOGGER = logging.getLogger("nexus_engine.security")
_HANDLER_LOCK = threading.Lock()
ERROR_LOG_PATH = DATA_DIR / "logs" / "backend_errors.log"
_SECRET_PATTERNS = (
    re.compile(r"(?i)\b((?:https?|socks5?)://)[^/\s:@]+:[^@\s/]+@"),
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|password|cookie)\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"(?i)(\bproxy(?:_url|_info)?\s*[:=]\s*)[^\s,;]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"EAA[A-Za-z0-9_-]{16,}"),
)
_WINDOWS_PATH = re.compile(r"(?i)\b[A-Z]:\\[^\r\n]*")


def _ensure_error_file_handler() -> None:
    """Attach a bounded rotating file handler so error IDs survive service restarts."""
    with _HANDLER_LOCK:
        target = str(ERROR_LOG_PATH)
        for existing in LOGGER.handlers:
            if isinstance(existing, RotatingFileHandler) and getattr(existing, "baseFilename", "") == target:
                return
        try:
            ERROR_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                ERROR_LOG_PATH,
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            handler.setLevel(logging.ERROR)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
            )
            LOGGER.addHandler(handler)
            if LOGGER.level == logging.NOTSET or LOGGER.level > logging.ERROR:
                LOGGER.setLevel(logging.ERROR)
        except OSError:
            pass


def redact_sensitive(value: object) -> str:
    text = str(value or "")
    for pattern in _SECRET_PATTERNS:
        replacement = r"\1[REDACTED]" if pattern.groups else "[REDACTED]"
        text = pattern.sub(replacement, text)
    return _WINDOWS_PATH.sub("[LOCAL_PATH]", text)[:2_000]


def report_exception(context: str, exc: BaseException) -> str:
    _ensure_error_file_handler()
    error_id = uuid.uuid4().hex[:10]
    tb_text = ""
    try:
        formatted = traceback.format_exc()
        if formatted and "NoneType: None" not in formatted:
            tb_text = f"\n{redact_sensitive(formatted)}"
    except Exception:
        tb_text = ""
    LOGGER.error(
        "%s failed (error_id=%s, error_type=%s): %s%s",
        context,
        error_id,
        type(exc).__name__,
        redact_sensitive(exc),
        tb_text,
    )
    return error_id


def safe_user_error(context: str, exc: BaseException, message: str) -> str:
    error_id = report_exception(context, exc)
    return f"{message} Mã lỗi: {error_id}."
