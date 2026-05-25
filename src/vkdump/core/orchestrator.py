"""Shared task execution: open run record, call module, persist outcome.

Used by both the CLI and the GUI so that run-history bookkeeping lives in one place.
"""
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any
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
