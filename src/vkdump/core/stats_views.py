"""Aggregated views the results dialog displays.

Three flat tables, each returned as a list of dicts so the Qt model
that backs the GUI table doesn't need to know SQL.
"""
from __future__ import annotations

from typing import Any

from .db import connection


# Slugs we always project as separate columns in the chats / users
# tables. Anything outside this whitelist falls into `other_count`.
_KIND_COLS = ("photo", "video", "audio", "file", "forward", "link", "sticker")


def _kind_case(prefix: str = "") -> str:
    """Build the `SUM(CASE WHEN kind='photo' THEN 1 ELSE 0 END) AS photo_count`
    block for the whitelisted kinds. `prefix` is the alias of the
    attachments row (e.g. 'a').
    """
    p = f"{prefix}." if prefix else ""
    parts = [
        f"SUM(CASE WHEN {p}kind = '{kind}' THEN 1 ELSE 0 END) AS {kind}_count"
        for kind in _KIND_COLS
    ]
    others = " AND ".join(f"{p}kind != '{k}'" for k in _KIND_COLS)
    parts.append(
        f"SUM(CASE WHEN {others} THEN 1 ELSE 0 END) AS other_count"
    )
    return ",\n               ".join(parts)


def chats_rollup() -> list[dict[str, Any]]:
    """One row per chat: identity + message + per-kind attachment counts."""
    sql = f"""
    WITH att AS (
        SELECT m.chat_id AS chat_id,
               {_kind_case('a')},
               COUNT(*) AS total_attachments
          FROM attachments a
          JOIN messages m ON m.id = a.message_id
         GROUP BY m.chat_id
    )
    SELECT c.id,
           c.title,
           c.source_folder,
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
     ORDER BY c.message_count DESC, c.id
    """
    with connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


def users_rollup() -> list[dict[str, Any]]:
    """One row per user-with-at-least-one-message."""
    sql = f"""
    WITH att AS (
        SELECT m.sender_user_id AS sender_user_id,
               {_kind_case('a')},
               COUNT(*) AS total_attachments
          FROM attachments a
          JOIN messages m ON m.id = a.message_id
         GROUP BY m.sender_user_id
    ),
    msg AS (
        SELECT sender_user_id, COUNT(*) AS message_count
          FROM messages
         GROUP BY sender_user_id
    )
    SELECT u.id,
           u.vk_id,
           u.display_name,
           u.is_deleted,
           u.profile_url,
           COALESCE(msg.message_count, 0)    AS message_count,
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
      LEFT JOIN msg ON msg.sender_user_id = u.id
      LEFT JOIN att ON att.sender_user_id = u.id
     WHERE COALESCE(msg.message_count, 0) > 0
     ORDER BY msg.message_count DESC, u.id
    """
    with connection() as conn:
        return [dict(r) for r in conn.execute(sql)]


def attachments_rollup() -> list[dict[str, Any]]:
    """One row per attachment kind, with count + first/last message dates."""
    sql = """
    SELECT a.kind,
           COUNT(*)         AS total,
           MIN(m.sent_at)   AS first_seen_at,
           MAX(m.sent_at)   AS last_seen_at
      FROM attachments a
      JOIN messages m ON m.id = a.message_id
     GROUP BY a.kind
     ORDER BY total DESC
    """
    with connection() as conn:
        return [dict(r) for r in conn.execute(sql)]
