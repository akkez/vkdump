"""Aggregated views the results dialog displays.

Three flat tables, each returned as a list of dicts so the Qt model
that backs the GUI table doesn't need to know SQL.

Chats / users are pre-filtered by `min_messages` (default 10) in SQL,
which keeps the heavy attachments × messages JOIN scoped to the
visible rows. The hidden count is returned alongside so the dialog
can footer-label it.
"""
from __future__ import annotations

from typing import Any

from .db import connection


# Slugs we always project as separate columns in the chats / users
# tables. Anything outside this whitelist falls into `other_count`.
_KIND_COLS = ("photo", "video", "audio", "file", "forward", "link", "sticker")


def _kind_case(prefix: str = "") -> str:
    """Build the `SUM(CASE WHEN kind='photo' THEN 1 ELSE 0 END) AS photo_count`
    block for the whitelisted kinds.
    """
    p = f"{prefix}." if prefix else ""
    parts = [
        f"SUM(CASE WHEN {p}kind = '{kind}' THEN 1 ELSE 0 END) AS {kind}_count"
        for kind in _KIND_COLS
    ]
    others = " AND ".join(f"{p}kind != '{k}'" for k in _KIND_COLS)
    parts.append(f"SUM(CASE WHEN {others} THEN 1 ELSE 0 END) AS other_count")
    return ",\n               ".join(parts)


def chats_rollup(min_messages: int = 10) -> tuple[list[dict[str, Any]], int]:
    """Visible chats + count of those hidden below the threshold."""
    with connection() as conn:
        hidden = conn.execute(
            "SELECT COUNT(*) FROM chats WHERE message_count < ?",
            (min_messages,),
        ).fetchone()[0]
        sql = f"""
        WITH visible AS (
            SELECT id FROM chats WHERE message_count >= ?
        ),
        att AS (
            SELECT m.chat_id AS chat_id,
                   {_kind_case('a')},
                   COUNT(*) AS total_attachments
              FROM attachments a
              JOIN messages m ON m.id = a.message_id
             WHERE m.chat_id IN (SELECT id FROM visible)
             GROUP BY m.chat_id
        )
        SELECT c.id,
               c.title,
               c.peer_id,
               c.type,
               c.message_count,
               COALESCE(att.total_attachments, 0) AS attachment_count,
               COALESCE(att.photo_count,    0) AS photo_count,
               COALESCE(att.video_count,    0) AS video_count,
               COALESCE(att.audio_count,    0) AS audio_count,
               COALESCE(att.file_count,     0) AS file_count,
               COALESCE(att.forward_count,  0) AS forward_count,
               COALESCE(att.link_count,     0) AS link_count,
               COALESCE(att.sticker_count,  0) AS sticker_count,
               COALESCE(att.other_count,    0) AS other_count,
               c.last_message_at,
               c.created_at
          FROM chats c
          LEFT JOIN att ON att.chat_id = c.id
         WHERE c.id IN (SELECT id FROM visible)
         ORDER BY c.message_count DESC, c.id
        """
        rows = [dict(r) for r in conn.execute(sql, (min_messages,))]
        return rows, hidden


def users_rollup(min_messages: int = 10) -> tuple[list[dict[str, Any]], int]:
    """Visible users (≥ min_messages messages) + hidden count."""
    with connection() as conn:
        msg_counts_sql = """
            SELECT sender_user_id, COUNT(*) AS message_count
              FROM messages
             WHERE sender_user_id IS NOT NULL
             GROUP BY sender_user_id
        """
        # Two passes against msg_counts: one to count hidden, one to
        # drive the JOIN. Materialising into a temp table would be
        # cleaner but stdlib sqlite3 doesn't carry one across queries.
        hidden = conn.execute(
            f"""SELECT COUNT(*) FROM ({msg_counts_sql})
                 WHERE message_count > 0 AND message_count < ?""",
            (min_messages,),
        ).fetchone()[0]

        sql = f"""
        WITH msg AS ({msg_counts_sql}),
        visible AS (
            SELECT sender_user_id FROM msg WHERE message_count >= ?
        ),
        att AS (
            SELECT m.sender_user_id AS sender_user_id,
                   {_kind_case('a')},
                   COUNT(*) AS total_attachments
              FROM attachments a
              JOIN messages m ON m.id = a.message_id
             WHERE m.sender_user_id IN (SELECT sender_user_id FROM visible)
             GROUP BY m.sender_user_id
        )
        SELECT u.id,
               u.vk_id,
               u.display_name,
               u.profile_url,
               msg.message_count       AS message_count,
               COALESCE(att.total_attachments, 0) AS attachment_count,
               COALESCE(att.photo_count,    0) AS photo_count,
               COALESCE(att.video_count,    0) AS video_count,
               COALESCE(att.audio_count,    0) AS audio_count,
               COALESCE(att.file_count,     0) AS file_count,
               COALESCE(att.forward_count,  0) AS forward_count,
               COALESCE(att.link_count,     0) AS link_count,
               COALESCE(att.sticker_count,  0) AS sticker_count,
               COALESCE(att.other_count,    0) AS other_count
          FROM users u
          JOIN msg ON msg.sender_user_id = u.id
          LEFT JOIN att ON att.sender_user_id = u.id
         WHERE u.id IN (SELECT sender_user_id FROM visible)
         ORDER BY msg.message_count DESC, u.id
        """
        rows = [dict(r) for r in conn.execute(sql, (min_messages,))]
        return rows, hidden


def attachments_rollup() -> list[dict[str, Any]]:
    """One row per attachment kind: count, first/last `sent_at`, and the
    title of the chat where the most-recent one of that kind landed.
    """
    sql = """
    WITH ranked AS (
        SELECT a.kind,
               m.sent_at,
               c.title    AS chat_title,
               c.peer_id  AS chat_peer,
               ROW_NUMBER() OVER (
                   PARTITION BY a.kind
                   ORDER BY m.sent_at DESC, a.id DESC
               ) AS rn
          FROM attachments a
          JOIN messages m ON m.id = a.message_id
          LEFT JOIN chats c ON c.id = m.chat_id
    )
    SELECT a.kind,
           COUNT(*)       AS total,
           MIN(m.sent_at) AS first_seen_at,
           MAX(m.sent_at) AS last_seen_at,
           (SELECT chat_title FROM ranked WHERE kind = a.kind AND rn = 1)
                          AS last_chat_title,
           (SELECT chat_peer  FROM ranked WHERE kind = a.kind AND rn = 1)
                          AS last_chat_peer
      FROM attachments a
      JOIN messages m ON m.id = a.message_id
     GROUP BY a.kind
     ORDER BY total DESC
    """
    with connection() as conn:
        return [dict(r) for r in conn.execute(sql)]
