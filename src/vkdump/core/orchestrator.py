"""Shared task execution: open run record, call module, persist outcome.

Used by both the CLI and the GUI so that run-history bookkeeping lives in one place.
"""
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from loguru import logger

from .db import apply_migrations
from .logging import configure_logging
from .progress import ProgressReporter, Cancelled
from .runs import start_run, finish_run
from ..tasks.spec import TaskSpec


@dataclass
class TaskOutcome:
    run_id: int
    status: str
    result: Any = None
    error: str | None = None


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def execute(task: TaskSpec, params: dict, progress: ProgressReporter) -> TaskOutcome:
    # Prerequisite for every module: schema must be current and the file
    # logger must be attached. Both are idempotent — apply_migrations() is a
    # no-op when nothing is pending; configure_logging() short-circuits on
    # repeat calls. Keeps modules from having to think about either.
    configure_logging()
    applied = apply_migrations()
    if applied:
        logger.info("auto-applied {} pending migration(s) before task {}", len(applied), task.name)
    run_id = start_run(task.name, _jsonable(params))
    try:
        result = task.run(params, progress)
    except Cancelled:
        finish_run(run_id, "cancelled", error="Cancelled by user")
        return TaskOutcome(run_id=run_id, status="cancelled")
    except Exception as exc:
        tb = traceback.format_exc()
        finish_run(run_id, "failed", error=tb)
        return TaskOutcome(run_id=run_id, status="failed", error=str(exc))
    finish_run(run_id, "success", result=_jsonable(result) if isinstance(result, dict) else None)
    return TaskOutcome(run_id=run_id, status="success", result=result)
