"""Loguru file sinks. Two files next to the SQLite store:

- `vkdump.log`         — everything (DEBUG and up).
- `vkdump-errors.log`  — WARNING and up only, so you can `tail -f` a
                         focused stream when chasing parse failures.

Idempotent: safe to call from every entry point on startup.
"""
from pathlib import Path

from loguru import logger

from .db import resolve_db_path

_configured: bool = False
_log_path: Path | None = None
_errors_log_path: Path | None = None


def configure_logging() -> Path:
    """Attach rotating file sinks to loguru. Returns the main log path."""
    global _configured, _log_path, _errors_log_path
    if _configured and _log_path is not None:
        return _log_path
    main = resolve_db_path().parent / "vkdump.log"
    errors = resolve_db_path().parent / "vkdump-errors.log"
    main.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        main,
        rotation="10 MB",
        retention=5,
        encoding="utf-8",
        level="DEBUG",
        backtrace=True,
        diagnose=False,
    )
    logger.add(
        errors,
        rotation="10 MB",
        retention=5,
        encoding="utf-8",
        level="WARNING",
        backtrace=True,
        diagnose=False,
    )
    _configured = True
    _log_path = main
    _errors_log_path = errors
    return main


def log_path() -> Path:
    return _log_path or configure_logging()


def errors_log_path() -> Path:
    if _errors_log_path is None:
        configure_logging()
    assert _errors_log_path is not None
    return _errors_log_path
