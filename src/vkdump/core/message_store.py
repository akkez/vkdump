from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Iterable

from ..parsers.vk.models import ParsedMessage
from . import app_config
from .db import connection


def account_pk(vk_id: str | int | None, conn=None, *, create=False) -> int | None:
    if conn is None:
        with connection() as opened:
            return account_pk(vk_id, opened, create=create)
    if vk_id is None or not str(vk_id).strip():
        return None
    if create:
        conn.execute("INSERT OR IGNORE INTO accounts(provider,vk_id) VALUES('vk',?)", (int(vk_id),))
    row = conn.execute("SELECT id FROM accounts WHERE provider='vk' AND vk_id=?", (vk_id,)).fetchone()
    return int(row[0]) if row else None


def active_account_pk() -> int | None:
    selected = app_config.get('active_account_id')
    return (account_pk(selected) or -1) if selected else None


def upsert_user(conn, vk_id, name=None, avatar=None):
    if vk_id is None:
        return None
    if name in ("Вы", "You"):
        name = None
    url = f'https://vk.com/{"club" if vk_id < 0 else "id"}{abs(vk_id)}'
    return conn.execute(
        "INSERT INTO users(provider,vk_id,display_name,profile_url,avatar_url,is_deleted) "
        "VALUES('vk',?,?,?,?,?) ON CONFLICT(provider,vk_id) DO UPDATE SET "
        "display_name=COALESCE(NULLIF(excluded.display_name,''),users.display_name), "
        "avatar_url=COALESCE(NULLIF(excluded.avatar_url,''),users.avatar_url), "
        "profile_url=excluded.profile_url,is_deleted=MAX(users.is_deleted,excluded.is_deleted) RETURNING id",
        (vk_id, name, url, avatar, int(name == 'DELETED')),
    ).fetchone()[0]


def upsert_chat(conn, account_id, peer_id, *, title=None, type_=None, source_folder=None, expected=None):
    peer_id = str(peer_id) if peer_id is not None else None
    if peer_id is not None:
        row = conn.execute('SELECT id FROM chats WHERE provider=? AND account_id IS ? AND peer_id=?',
                           ('vk', account_id, peer_id)).fetchone()
    else:
        row = conn.execute('SELECT id FROM chats WHERE provider=? AND account_id IS ? AND source_folder IS ?',
                           ('vk', account_id, source_folder)).fetchone()
    if row:
        conn.execute('UPDATE chats SET title=COALESCE(NULLIF(?,\'\'),title),type=COALESCE(?,type),'
                     'source_folder=COALESCE(?,source_folder),total_expected_count=COALESCE(?,total_expected_count) WHERE id=?',
                     (title, type_, source_folder, expected, row[0]))
        return row[0]
    return conn.execute('INSERT INTO chats(provider,account_id,peer_id,title,type,source_folder,total_expected_count) '
                        "VALUES('vk',?,?,?,?,?,?) RETURNING id",
                        (account_id, peer_id, title, type_, source_folder, expected)).fetchone()[0]


def _utc(dt: datetime | None):
    if dt is None:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def write_messages(conn: sqlite3.Connection, chat_id: int, messages: Iterable[ParsedMessage]) -> int:
    chat = conn.execute('SELECT account_id FROM chats WHERE id=?', (chat_id,)).fetchone()
    owner = conn.execute('SELECT vk_id FROM accounts WHERE id=?', (chat['account_id'],)).fetchone()
    inserted = 0
    for m in messages:
        sender = m.sender_vk_id if m.sender_vk_id is not None else (owner[0] if owner and m.sender_is_self else None)
        user_id = upsert_user(conn, sender, m.sender_display_name)
        old = conn.execute('SELECT * FROM messages WHERE chat_id=? AND vk_message_id=?', (chat_id, m.vk_message_id)).fetchone()
        edited = _utc(m.edited_at)
        newer = old is None or (edited is not None and (old['edited_at'] is None or edited > old['edited_at']))
        values = dict(provider='vk',account_id=chat['account_id'],chat_id=chat_id,vk_message_id=m.vk_message_id,
                      sender_user_id=user_id,sender_vk_id=sender,sender_display_name=m.sender_display_name,
                      sender_is_self=int(m.sender_is_self),sent_at=_utc(m.sent_at),text=m.text,
                      has_forwards=int(m.has_forwards),forwarded_count=m.forwarded_count,is_reply=int(m.is_reply),
                      reply_to_message_id=m.reply_to_message_id,attachment_count=len(m.attachments),
                      source_file=m.source_file or None,raw_html=m.raw_html if not m.fully_parsed else None,
                      is_edited=int(m.is_edited),edited_at=edited,conversation_message_id=m.conversation_message_id,
                      reply_data_json=json.dumps(m.reply_data,ensure_ascii=False) if m.reply_data else None,
                      source_kind=m.source_kind)
        if old is None:
            columns = ','.join(values)
            mid = conn.execute(f'INSERT INTO messages({columns}) VALUES({",".join("?" for _ in values)}) RETURNING id',
                               tuple(values.values())).fetchone()[0]
            inserted += 1
        else:
            mid = old['id']
            if old['edited_at'] is not None and m.source_kind == 'api' and (edited is None or edited < old['edited_at']):
                continue
            if old['source_kind'] != m.source_kind and old['source_kind'] != 'mixed':
                conn.execute("UPDATE messages SET source_kind='mixed' WHERE id=?",(mid,))
            updates = {k:v for k,v in values.items() if k not in ('provider','account_id','chat_id','vk_message_id','attachment_count','source_kind')
                       and (v not in (None,'',0) or (newer and k == 'text'))}
            if old['source_kind'] != m.source_kind and old['sent_at'] != _utc(m.sent_at):
                conn.execute("INSERT OR REPLACE INTO chat_data_issues(chat_id,vk_message_id,source,field,reason) VALUES(?,?,?,'sent_at','source_value_conflict')",(chat_id,m.vk_message_id,m.source_kind))
                if m.source_kind == 'archive':
                    updates.pop('sent_at',None)
            if not newer and old['text']:
                updates.pop('text',None)
            if updates:
                conn.execute('UPDATE messages SET '+','.join(f'{k}=?' for k in updates)+' WHERE id=?', (*updates.values(),mid))
        existing = [dict(r) for r in conn.execute('SELECT * FROM attachments WHERE message_id=? ORDER BY position', (mid,))]
        used = set()
        for a in m.attachments:
            identity = a.external_id
            same = next((r for r in existing if r['id'] not in used and r['kind']==a.kind and
                         ((identity and identity==r['external_id']) or (a.url and a.url==r['url']) or
                          (not identity and not a.url and a.position==r['position']))), None)
            data = json.dumps(a.data,ensure_ascii=False) if a.data else None
            if same:
                used.add(same['id'])
                previous = json.loads(same['data_json'] or '{}')
                if a.data:
                    previous.update(a.data)
                data = json.dumps(previous,ensure_ascii=False) if previous else None
                conn.execute('UPDATE attachments SET description=COALESCE(?,description),url=COALESCE(?,url),'
                             'data_json=COALESCE(?,data_json),external_id=COALESCE(?,external_id),access_key=COALESCE(?,access_key) WHERE id=?',
                             (a.description,a.url,data,identity,a.access_key,same['id']))
            else:
                position = conn.execute('SELECT COALESCE(MAX(position),-1)+1 FROM attachments WHERE message_id=?',(mid,)).fetchone()[0]
                conn.execute('INSERT INTO attachments(message_id,kind,description,url,forward_count,position,data_json,external_id,access_key) '
                             'VALUES(?,?,?,?,?,?,?,?,?)',(mid,a.kind,a.description,a.url,a.forward_count,position,data,identity,a.access_key))
        if old and newer and m.source_kind == 'api':
            for prior in existing:
                if prior['id'] not in used:
                    conn.execute('DELETE FROM attachments WHERE id=?',(prior['id'],))
        conn.execute('UPDATE messages SET attachment_count=(SELECT COUNT(*) FROM attachments WHERE message_id=?) WHERE id=?',(mid,mid))
    refresh_chat(conn,chat_id,inserted)
    return inserted


def refresh_chat(conn, chat_id, inserted):
    conn.execute('UPDATE chats SET message_count=message_count+?,'
                 'parsed_message_count=parsed_message_count+?, '
                 'created_at=(SELECT MIN(sent_at) FROM messages WHERE chat_id=?),'
                 'last_message_at=(SELECT MAX(sent_at) FROM messages WHERE chat_id=?),'
                 'last_message_text=(SELECT text FROM messages WHERE chat_id=? ORDER BY sent_at DESC,id DESC LIMIT 1),'
                 'last_message_sender=(SELECT sender_display_name FROM messages WHERE chat_id=? ORDER BY sent_at DESC,id DESC LIMIT 1),'
                 'last_parsed_at=CURRENT_TIMESTAMP WHERE id=?', (inserted,inserted,*(chat_id,)*5))
