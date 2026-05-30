"""Tiny key/value store backed by SQLite (`_app_config` table).

Replaces the classic ini-in-the-cwd pattern: any module that wants to
remember its last input across runs (and across CLI/GUI) writes here
and reads on next launch. Keys are dotted strings, namespaced by the
writing module: e.g. `parse_dump.last_source`, `save_chat.last_output`.

Values are stored as TEXT; callers serialise non-strings (json, str())
on the way in and parse on the way out — keeps the schema trivial and
the read path branch-free.
"""
from __future__ import annotations

from .db import connection


def get(key: str, default: str | None = None) -> str | None:
    """Return the stored value for `key`, or `default` if not set."""
    with connection() as conn:
        row = conn.execute(
            "SELECT value FROM _app_config WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return default
    val = row[0]
    return val if val is not None else default


def set(key: str, value: str | None) -> None:
    """Upsert `key=value`. Passing `None` clears the key."""
    with connection() as conn:
        if value is None:
            conn.execute("DELETE FROM _app_config WHERE key = ?", (key,))
            return
        conn.execute(
            "INSERT INTO _app_config (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value,"
            "                                updated_at = CURRENT_TIMESTAMP",
            (key, value),
        )
