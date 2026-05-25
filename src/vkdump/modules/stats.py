"""Module: compute a brief summary over stored messages.

Returns a dict that the CLI / GUI can render as the run's result, and that
shows up in `runs list` (truncated) and `runs` history. Cheap aggregates
only — heavy per-user / per-chat breakdowns belong in a future module.
"""
from ..core.db import connection
from ..core.progress import ProgressReporter


def run(params: dict, progress: ProgressReporter) -> dict:
    progress.report(0, 6, "Counting…")
    with connection() as conn:
        chats_total = conn.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        progress.report(1, 6, "chats counted")

        users_total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        users_deleted = conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_deleted = 1"
        ).fetchone()[0]
        progress.report(2, 6, "users counted")

        msg_row = conn.execute(
            """
            SELECT COUNT(*) AS cnt,
                   SUM(sender_is_self) AS self_cnt,
                   SUM(has_forwards) AS fwd_cnt,
                   SUM(forwarded_count) AS fwd_items,
                   MIN(sent_at) AS first_at,
                   MAX(sent_at) AS last_at
              FROM messages
            """
        ).fetchone()
        progress.report(3, 6, "messages counted")

        attachments_total = conn.execute(
            "SELECT COUNT(*) FROM attachments"
        ).fetchone()[0]
        att_by_kind = {
            r["kind"]: r["c"]
            for r in conn.execute(
                "SELECT kind, COUNT(*) AS c FROM attachments GROUP BY kind ORDER BY c DESC"
            )
        }
        progress.report(4, 6, "attachments counted")

        errors_total = conn.execute("SELECT COUNT(*) FROM parse_errors").fetchone()[0]
        progress.report(5, 6, "errors counted")

        top_chats = [
            {
                "title": r["title"],
                "source_folder": r["source_folder"],
                "messages": r["message_count"],
            }
            for r in conn.execute(
                """
                SELECT title, source_folder, message_count
                  FROM chats
                 ORDER BY message_count DESC
                 LIMIT 10
                """
            )
        ]
        top_senders = [
            {
                "vk_id": r["vk_id"],
                "display_name": r["display_name"],
                "is_deleted": bool(r["is_deleted"]),
                "messages": r["c"],
            }
            for r in conn.execute(
                """
                SELECT u.vk_id, u.display_name, u.is_deleted, COUNT(*) AS c
                  FROM messages m
                  JOIN users u ON u.id = m.sender_user_id
                 GROUP BY u.id
                 ORDER BY c DESC
                 LIMIT 10
                """
            )
        ]
        progress.report(6, 6, "Done")

    summary = {
        "chats": chats_total,
        "users": {"total": users_total, "deleted": users_deleted},
        "messages": {
            "total": msg_row["cnt"] or 0,
            "self": msg_row["self_cnt"] or 0,
            "with_forwards": msg_row["fwd_cnt"] or 0,
            "forwarded_items": msg_row["fwd_items"] or 0,
            "first_at": msg_row["first_at"],
            "last_at": msg_row["last_at"],
        },
        "attachments": {"total": attachments_total, "by_kind": att_by_kind},
        "parse_errors": errors_total,
        "top_chats_by_messages": top_chats,
        "top_senders_by_messages": top_senders,
    }
    progress.log(_format_human_summary(summary))
    return summary


def _format_human_summary(s: dict) -> str:
    lines = [
        "── stats ─────────────────────────────",
        f"chats: {s['chats']}",
        f"users: {s['users']['total']} (deleted: {s['users']['deleted']})",
        (
            f"messages: {s['messages']['total']} "
            f"(self: {s['messages']['self']}, "
            f"with forwards: {s['messages']['with_forwards']}, "
            f"forwarded items: {s['messages']['forwarded_items']})"
        ),
        f"date range: {s['messages']['first_at']} → {s['messages']['last_at']}",
        f"attachments: {s['attachments']['total']}",
    ]
    for kind, cnt in s["attachments"]["by_kind"].items():
        lines.append(f"  {kind}: {cnt}")
    lines.append(f"parse errors: {s['parse_errors']}")
    if s["top_chats_by_messages"]:
        lines.append("top chats:")
        for c in s["top_chats_by_messages"]:
            lines.append(f"  [{c['messages']:>6}] {c['title']}  ({c['source_folder']})")
    if s["top_senders_by_messages"]:
        lines.append("top senders:")
        for u in s["top_senders_by_messages"]:
            tag = " (DELETED)" if u["is_deleted"] else ""
            lines.append(f"  [{u['messages']:>6}] {u['display_name']}{tag}  vk_id={u['vk_id']}")
    return "\n".join(lines)
