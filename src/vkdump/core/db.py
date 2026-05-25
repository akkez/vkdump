import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from loguru import logger

from .settings import get_settings


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def resolve_db_path() -> Path:
    p = Path(get_settings().db_path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


def _migrations_dir() -> Path:
    return _repo_root() / "migrations"


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Open a short-lived SQLite connection. Commit on success, rollback on error."""
    db_path = resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _split_statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]


def apply_migrations() -> list[str]:
    """Apply pending SQL migrations in lexicographic order. Returns names applied this run."""
    applied_now: list[str] = []

    with connection() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS _migrations (
                name TEXT PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        already = {row["name"] for row in conn.execute("SELECT name FROM _migrations").fetchall()}

    for sql_file in sorted(_migrations_dir().glob("*.sql")):
        if sql_file.name in already:
            continue
        logger.info("Applying migration: {}", sql_file.name)
        sql = sql_file.read_text(encoding="utf-8")
        with connection() as conn:
            for stmt in _split_statements(sql):
                conn.execute(stmt)
            conn.execute("INSERT INTO _migrations (name) VALUES (?)", (sql_file.name,))
        applied_now.append(sql_file.name)
    return applied_now
