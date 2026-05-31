"""Module: compute a brief summary over stored messages.

When ``app_config['active_account_id']`` is set, every aggregate is
narrowed to that account — chats from other accounts, their messages,
their attachments and their parse errors fall out of scope. Unset →
whole-DB rollup as before.

Returns a dict that the CLI / GUI can render as the run's result, and
that shows up in `runs list` (truncated) and `runs` history. Cheap
aggregates only — heavy per-user / per-chat breakdowns belong in a
future module.
"""
from ..core import app_config
from ..core.db import connection
from ..core.progress import ProgressReporter


def run(params: dict, progress: ProgressReporter) -> dict:
    active = (app_config.get("active_account_id") or "").strip() or None
    # SQL fragment + binding tuple, reused per query. Empty string when
    # there is no filter, so we keep one parametrised query shape per
    # rollup instead of two diverging variants.
    if active:
        chats_where = "WHERE account_id = ?"
        chats_args: tuple = (active,)
        msg_where = "WHERE account_id = ?"
        msg_args: tuple = (active,)
        att_join = (
            " FROM attachments a JOIN messages m ON m.id = a.message_id"
            " WHERE m.account_id = ?"
        )
        att_args: tuple = (active,)
        err_join = (
            " FROM parse_errors pe JOIN chats c ON c.id = pe.chat_id"
            " WHERE c.account_id = ?"
        )
        err_args: tuple = (active,)
        users_join = (
            " FROM users u"
            " WHERE u.id IN (SELECT DISTINCT sender_user_id FROM messages"
            "                WHERE sender_user_id IS NOT NULL"
            "                  AND account_id = ?)"
        )
        users_args: tuple = (active,)
    else:
        chats_where = ""
        chats_args = ()
        msg_where = ""
        msg_args = ()
        att_join = " FROM attachments a"
        att_args = ()
        err_join = " FROM parse_errors pe"
        err_args = ()
        users_join = " FROM users u"
        users_args = ()

    progress.report(0, 6, "Counting…")
    with connection() as conn:
        chats_total = conn.execute(
            f"SELECT COUNT(*) FROM chats {chats_where}", chats_args,
        ).fetchone()[0]
        progress.report(1, 6, "chats counted")

        users_total = conn.execute(
            f"SELECT COUNT(*){users_join}", users_args,
        ).fetchone()[0]
        users_deleted = conn.execute(
            f"SELECT COUNT(*){users_join} AND u.is_deleted = 1"
            if active else
            "SELECT COUNT(*) FROM users u WHERE u.is_deleted = 1",
            users_args,
        ).fetchone()[0]
        progress.report(2, 6, "users counted")

        msg_row = conn.execute(
            f"""
            SELECT COUNT(*) AS cnt,
                   SUM(sender_is_self) AS self_cnt,
                   SUM(has_forwards) AS fwd_cnt,
                   SUM(forwarded_count) AS fwd_items,
                   MIN(sent_at) AS first_at,
                   MAX(sent_at) AS last_at
              FROM messages {msg_where}
            """,
            msg_args,
        ).fetchone()
        progress.report(3, 6, "messages counted")

        attachments_total = conn.execute(
            f"SELECT COUNT(*){att_join}", att_args,
        ).fetchone()[0]
        att_by_kind = {
            r["kind"]: r["c"]
            for r in conn.execute(
                f"SELECT a.kind AS kind, COUNT(*) AS c{att_join}"
                " GROUP BY a.kind ORDER BY c DESC",
                att_args,
            )
        }
        progress.report(4, 6, "attachments counted")

        errors_total = conn.execute(
            f"SELECT COUNT(*){err_join}", err_args,
        ).fetchone()[0]
        progress.report(5, 6, "errors counted")

        top_chats = [
            {
                "title": r["title"],
                "source_folder": r["source_folder"],
                "messages": r["message_count"],
            }
            for r in conn.execute(
                f"""
                SELECT title, source_folder, message_count
                  FROM chats {chats_where}
                 ORDER BY message_count DESC
                 LIMIT 10
                """,
                chats_args,
            )
        ]
        if active:
            top_senders_sql = """
                SELECT u.vk_id, u.display_name, u.is_deleted, COUNT(*) AS c
                  FROM messages m
                  JOIN users u ON u.id = m.sender_user_id
                 WHERE m.account_id = ?
                 GROUP BY u.id
                 ORDER BY c DESC
                 LIMIT 10
            """
            top_senders_args: tuple = (active,)
        else:
            top_senders_sql = """
                SELECT u.vk_id, u.display_name, u.is_deleted, COUNT(*) AS c
                  FROM messages m
                  JOIN users u ON u.id = m.sender_user_id
                 GROUP BY u.id
                 ORDER BY c DESC
                 LIMIT 10
            """
            top_senders_args = ()
        top_senders = [
            {
                "vk_id": r["vk_id"],
                "display_name": r["display_name"],
                "is_deleted": bool(r["is_deleted"]),
                "messages": r["c"],
            }
            for r in conn.execute(top_senders_sql, top_senders_args)
        ]
        progress.report(6, 6, "Done")

    summary = {
        "scope": (
            {"active_account_id": active} if active else {"active_account_id": None}
        ),
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
    scope = s.get("scope") or {}
    acct = scope.get("active_account_id")
    header = (
        f"── stats (account: {acct}) ───────────"
        if acct else "── stats (all accounts) ──────────────"
    )
    lines = [
        header,
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
