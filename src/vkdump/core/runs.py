import json
from dataclasses import dataclass
from datetime import datetime

from .db import connection


@dataclass
class RunRow:
    id: int
    task: str
    params: dict
    started_at: datetime
    finished_at: datetime | None
    status: str
    error: str | None
    result: dict | None


def start_run(task: str, params: dict) -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO runs (task, params, status) VALUES (?, ?, 'running')",
            (task, json.dumps(params)),
        )
        return int(cur.lastrowid)


def finish_run(
    run_id: int,
    status: str,
    error: str | None = None,
    result: dict | None = None,
) -> None:
    with connection() as conn:
        conn.execute(
            """
            UPDATE runs
               SET finished_at = CURRENT_TIMESTAMP,
                   status = ?,
                   error = ?,
                   result = ?
             WHERE id = ?
            """,
            (status, error, json.dumps(result) if result is not None else None, run_id),
        )


def list_runs(limit: int = 100) -> list[RunRow]:
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT id, task, params, started_at, finished_at, status, error, result
              FROM runs
          ORDER BY started_at DESC
             LIMIT ?
            """,
            (limit,),
        ).fetchall()

    return [
        RunRow(
            id=r["id"],
            task=r["task"],
            params=json.loads(r["params"]),
            started_at=r["started_at"],
            finished_at=r["finished_at"],
            status=r["status"],
            error=r["error"],
            result=json.loads(r["result"]) if r["result"] else None,
        )
        for r in rows
    ]
