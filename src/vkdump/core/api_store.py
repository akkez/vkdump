import json

from .db import connection


def persist_messages(account_pk: int, items: list[dict], response: dict | None = None) -> None:
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
            "INSERT INTO api_messages"
            " (account_id, peer_id, vk_message_id, message_data, attachment_data)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT(account_id,vk_message_id) DO UPDATE SET "
            "message_data=excluded.message_data,attachment_data=excluded.attachment_data,fetched_at=CURRENT_TIMESTAMP",
            rows,
        )

        from .message_store import upsert_chat, write_messages
        from ..parsers.vk.api import normalize_message, save_profiles
        save_profiles(conn, response or {})
        from .message_store import upsert_user
        account = conn.execute('SELECT vk_id,display_name,avatar_url FROM accounts WHERE id=?',(account_pk,)).fetchone()
        owner = account['vk_id']
        upsert_user(conn,owner,account['display_name'],account['avatar_url'])
        names = {r['vk_id']:r['display_name'] for r in conn.execute("SELECT vk_id,display_name FROM users WHERE provider='vk'")}
        by_peer = {}
        for item in items:
            if isinstance(item.get('id'),int) and isinstance(item.get('peer_id'),int):
                by_peer.setdefault(item['peer_id'],[]).append(item)
        for peer,messages in by_peer.items():
            type_ = 'group_chat' if peer >= 2000000000 else 'community' if peer < 0 else 'dm'
            chat_id = upsert_chat(conn,account_pk,peer,type_=type_)
            normalized = []
            for item in messages:
                if not isinstance(item.get('date'),int):
                    conn.execute("INSERT OR REPLACE INTO chat_data_issues(chat_id,vk_message_id,source,field,reason) VALUES(?,?,'api','sent_at','missing_timestamp')", (chat_id,item['id']))
                    continue
                conn.execute("DELETE FROM chat_data_issues WHERE chat_id=? AND vk_message_id=? AND source='api' AND field='sent_at'", (chat_id,item['id']))
                known={'id','peer_id','from_id','date','text','out','attachments','fwd_messages','reply_message',
                       'geo','action','update_time','conversation_message_id','random_id','important','is_hidden',
                       'is_deleted','deleted','read_state','is_unavailable','is_cropped','version','admin_author_id'}
                for field in item.keys()-known:
                    conn.execute("INSERT OR REPLACE INTO chat_data_issues(chat_id,vk_message_id,source,field,reason) VALUES(?,?,'api',?,'unknown_api_field')",(chat_id,item['id'],field))
                normalized.append(normalize_message(item,owner,names))
            write_messages(conn,chat_id,normalized)
