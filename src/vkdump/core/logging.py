"""Loguru file-sink configuration. The log file lives next to the SQLite
store so all writable state is co-located.

Idempotent: safe to call from every entry point on startup.
"""
from pathlib import Path

from loguru import logger

from .db import resolve_db_path

_configured: bool = False
_log_path: Path | None = None


def configure_logging() -> Path:
    """Attach a rotating file sink to loguru. Returns the log file path."""
    global _configured, _log_path
    if _configured and _log_path is not None:
        return _log_path
    path = resolve_db_path().parent / "vkdump.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        path,
        rotation="10 MB",
        retention=5,
        encoding="utf-8",
        level="DEBUG",
        backtrace=True,
        diagnose=False,  # avoid leaking values from frames into the log
    )
    _configured = True
    _log_path = path
    return path


def log_path() -> Path:
    return _log_path or configure_logging()
