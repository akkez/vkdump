import sqlite3
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from loguru import logger

from .settings import get_settings


class CancelHandle:
    """Cross-thread SQLite-query cancellation token.

    `sqlite3.Connection.interrupt()` is safe to call from any thread and
    aborts the currently-executing statement on that connection with
    `OperationalError: interrupted`. A handle is attached to whatever
    connection the worker thread happens to be using; calling `cancel()`
    from another thread (e.g. the GUI on window close) interrupts that
    statement and marks the handle so any subsequent `attach()` from the
    same worker also interrupts immediately. Idempotent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancelled = False
        self._conn: sqlite3.Connection | None = None

    def attach(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            self._conn = conn
            already = self._cancelled
        if already:
            try:
                conn.interrupt()
            except sqlite3.ProgrammingError:
                pass

    def detach(self, conn: sqlite3.Connection) -> None:
        with self._lock:
            if self._conn is conn:
                self._conn = None

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            conn = self._conn
        if conn is not None:
            try:
                conn.interrupt()
            except sqlite3.ProgrammingError:
                pass

    def is_cancelled(self) -> bool:
        return self._cancelled


_active_cancel = threading.local()


def set_active_cancel(handle: CancelHandle | None) -> None:
    """Install (or clear) the cancel handle that subsequent `connection()`
    calls on the current thread auto-attach to. Workers call this at the
    top of their `run()`; library code that opens connections doesn't
    need to know whether cancellation is enabled.
    """
    _active_cancel.handle = handle


def _current_cancel() -> CancelHandle | None:
    return getattr(_active_cancel, "handle", None)


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Writable app directory: next to the executable when frozen, repo root otherwise."""
    if not _frozen():
        return Path(__file__).resolve().parents[3]
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app":
            return parent.parent
    return exe.parent


def resource_dir() -> Path:
    """Read-only bundled resources: sys._MEIPASS when frozen, repo root otherwise."""
    if _frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[3]


def resolve_db_path() -> Path:
    p = Path(get_settings().db_path)
    if not p.is_absolute():
        p = app_dir() / p
    return p


def _migrations_dir() -> Path:
    return resource_dir() / "migrations"


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    """Open a short-lived SQLite connection. Commit on success, rollback on error.

    If a `CancelHandle` is installed via `set_active_cancel()` on the
    current thread, the new connection is attached to it so a concurrent
    `handle.cancel()` (typically from the GUI thread on window close)
    can interrupt an in-flight query.
    """
    db_path = resolve_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    cancel = _current_cancel()
    if cancel is not None:
        cancel.attach(conn)
    try:
        if cancel is not None and cancel.is_cancelled():
            # Cancel fired before we even opened the conn (or between
            # open and yield); `conn.interrupt()` is a no-op when no
            # query is running, so we'd otherwise leak a free query.
            raise sqlite3.OperationalError("interrupted")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        if cancel is not None:
            cancel.detach(conn)
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
