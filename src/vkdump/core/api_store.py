import json

from .db import connection


def persist_messages(account_pk: int, items: list[dict]) -> None:
    rows = []
    for m in items:
        vk_id = m.get("id")
        peer_id = m.get("peer_id")
        if not isinstance(vk_id, int) or not isinstance(peer_id, int):
            continue
        attachments = m.get("attachments") or None
        rows.append(
            (
                account_pk,
                peer_id,
                vk_id,
                json.dumps(m, ensure_ascii=False),
                json.dumps(attachments, ensure_ascii=False) if attachments else None,
            )
        )
    if not rows:
        return
    with connection() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO api_messages"
            " (account_id, peer_id, vk_message_id, message_data, attachment_data)"
            " VALUES (?, ?, ?, ?, ?)",
            rows,
        )
