"""Module: parse a local VK dump into the SQLite store.

The input may be a directory or a ZIP archive — the parser layer wraps it in
a `Source` so this module never touches files directly and never extracts
anything to disk.

Two optional pre-parse steps fire when the corresponding files are present
in the dump:

- `profile/page-info.html` → upsert the dump owner's `users` row with their
  real display name and avatar URL.
- `messages/index-messages.html` → upsert `chats` rows with titles, peer ids
  (parsed from the folder name where numeric), and a type slug — even before
  any message bodies are loaded. Lets the GUI show all chats immediately,
  with deep parsing filling in counters per chat.

Per-message failures are isolated into `parse_errors` so a single broken
item never blocks the rest.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Iterable

from loguru import logger

from ..core.db import connection
from ..core.progress import ProgressReporter
from ..parsers.vk import (
    ChatIndexEntry,
    Discovery,
    ParsedChatMeta,
    ParsedMessage,
    ParseError,
    ProfileInfo,
    Source,
    chat_type_for_peer,
    discover,
    list_message_pages,
    parse_chat_meta,
    parse_messages_index_file,
    parse_page,
    parse_profile_file,
    read_page,
)

PROVIDER = "vk"


def run(params: dict, progress: ProgressReporter) -> dict:
    source_input = Path(params["source"]).expanduser()
    source_tz = (params.get("source_timezone") or "UTC").strip() or "UTC"

    discovery = discover(source_input)
    try:
        return _run_with_discovery(discovery, source_tz, progress)
    finally:
        discovery.source.close()


def _run_with_discovery(
    discovery: Discovery, source_tz: str, progress: ProgressReporter
) -> dict:
    progress.log(_format_discovery(discovery))
    source = discovery.source

    # ---------- optional: profile (dump owner) ----------
    owner_vk_id: int | None = None
    if discovery.profile_file is not None:
        try:
            profile = parse_profile_file(source, discovery.profile_file)
            owner_vk_id = _apply_profile(profile)
            progress.log(
                f"profile: owner vk_id={profile.vk_id} name={profile.display_name!r} "
                f"avatar={'yes' if profile.avatar_url else 'no'}"
            )
        except Exception:
            logger.exception("profile parsing failed for {}", discovery.profile_file)
            progress.log(f"profile: failed to parse {discovery.profile_file} (continuing)")

    # ---------- optional: messages index (chat titles + peer ids) ----------
    indexed_entries: dict[str, ChatIndexEntry] = {}
    if discovery.messages_index_file is not None:
        try:
            entries = parse_messages_index_file(source, discovery.messages_index_file)
            indexed_entries = {e.peer_folder: e for e in entries}
            _preload_chats_from_index(entries, source_tz)
            progress.log(f"messages-index: preloaded {len(entries)} chat row(s) with titles")
        except Exception:
            logger.exception("messages-index parsing failed for {}", discovery.messages_index_file)
            progress.log(
                f"messages-index: failed to parse {discovery.messages_index_file} (continuing)"
            )

    # ---------- chat folders ----------
    total_messages = 0
    total_errors = 0
    chat_summaries: list[dict] = []

    root_label: str | None = None
    if discovery.initial_input is not None:
        if discovery.initial_input.is_file():
            # Single-file inputs: the chat folder is the file's parent dir.
            root_label = discovery.initial_input.parent.name
        else:
            root_label = discovery.initial_input.name

    if discovery.chat_folders:
        progress.log(f"chat folders: {len(discovery.chat_folders)}")
        logger.info("parse_dump: scanning {} chat folder(s)", len(discovery.chat_folders))
        for chat_idx, chat_rel in enumerate(discovery.chat_folders, start=1):
            progress.check_cancelled()
            chat_name = chat_rel.rsplit("/", 1)[-1] or root_label or "chat"
            progress.log(f"[{chat_idx}/{len(discovery.chat_folders)}] chat: {chat_name}")
            index_entry = indexed_entries.get(chat_name)
            summary = _parse_one_chat(
                source=source,
                chat_rel=chat_rel,
                chat_name=chat_name,
                progress=progress,
                source_tz=source_tz,
                index_entry=index_entry,
            )
            total_messages += summary["parsed_messages"]
            total_errors += summary["errors"]
            chat_summaries.append(summary)
        progress.report(len(discovery.chat_folders), len(discovery.chat_folders), "Done")

    # ---------- single-file mode ----------
    if discovery.single_html_files:
        for page_rel in discovery.single_html_files:
            progress.check_cancelled()
            chat_rel = page_rel.rsplit("/", 1)[0] if "/" in page_rel else ""
            chat_name = (
                chat_rel.rsplit("/", 1)[-1]
                if chat_rel
                else (root_label or "chat")
            )
            progress.log(f"single page: {page_rel}")
            summary = _parse_single_page(
                source=source,
                page_rel=page_rel,
                chat_rel=chat_rel,
                chat_name=chat_name,
                source_tz=source_tz,
                index_entry=indexed_entries.get(chat_name),
            )
            total_messages += summary["parsed_messages"]
            total_errors += summary["errors"]
            chat_summaries.append(summary)

    if not discovery.has_anything:
        raise ValueError(
            f"Nothing recognisable to parse under {source.describe()}. "
            "Expected an archive root (with index.html + messages/), a messages root "
            "(with index-messages.html), a single chat folder, or a standalone "
            "messages*.html / index-messages.html / page-info.html file."
        )

    return {
        "source": source.describe(),
        "owner_vk_id": owner_vk_id,
        "chats": len(chat_summaries),
        "chats_in_index": len(indexed_entries),
        "messages": total_messages,
        "errors": total_errors,
        "chat_summaries": chat_summaries,
    }


def _format_discovery(d: Discovery) -> str:
    parts = [f"discovery: source={d.source.describe()}"]
    if d.profile_file:
        parts.append(f"profile={d.profile_file}")
    if d.messages_index_file:
        parts.append(f"messages-index={d.messages_index_file}")
    if d.chat_folders:
        parts.append(f"chat-folders={len(d.chat_folders)}")
    if d.single_html_files:
        parts.append(f"single-html-files={d.single_html_files}")
    return " | ".join(parts)


# ---------- profile / index pre-load ----------


def _apply_profile(profile: ProfileInfo) -> int | None:
    """Upsert the dump owner into `users`. Returns the owner's vk_id."""
    if profile.vk_id is None:
        return None
    with connection() as conn:
        _upsert_user_full(
            conn,
            vk_id=profile.vk_id,
            display_name=profile.display_name,
            avatar_url=profile.avatar_url,
        )
    return profile.vk_id


def _preload_chats_from_index(entries: list[ChatIndexEntry], source_tz: str) -> None:
    """Insert chat rows we know about from the index, even before parsing any
    individual chat folder. Title + peer_id + type land immediately; counters
    stay at their defaults until the per-folder parse runs.
    """
    with connection() as conn:
        for e in entries:
            chat_type = chat_type_for_peer(e.peer_folder, e.peer_id)
            conn.execute(
                """
                INSERT INTO chats (
                    provider, account_id, source_folder, title,
                    peer_id, source_timezone, type
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, account_id, source_folder) DO UPDATE SET
                    title = COALESCE(excluded.title, chats.title),
                    peer_id = COALESCE(excluded.peer_id, chats.peer_id),
                    type = COALESCE(excluded.type, chats.type)
                """,
                (
                    PROVIDER,
                    "",  # account_id unknown at this stage; per-folder parse fills it.
                    e.peer_folder,
                    e.title,
                    str(e.peer_id) if e.peer_id is not None else None,
                    source_tz,
                    chat_type,
                ),
            )


# ---------- per-chat ----------


def _parse_one_chat(
    source: Source,
    chat_rel: str,
    chat_name: str,
    progress: ProgressReporter,
    source_tz: str,
    index_entry: ChatIndexEntry | None,
) -> dict:
    pages = list_message_pages(source, chat_rel)
    if not pages:
        return {
            "source_folder": chat_name,
            "title": index_entry.title if index_entry else None,
            "parsed_messages": 0,
            "errors": 0,
        }

    first_html = read_page(source, pages[0])
    meta = parse_chat_meta(first_html, source_folder=chat_name)
    chat_id = _upsert_chat(meta, source_tz=source_tz, index_entry=index_entry)

    page_count = len(pages)
    parsed_messages = 0
    errors = 0

    for page_idx, page_rel in enumerate(pages, start=1):
        progress.check_cancelled()
        page_name = page_rel.rsplit("/", 1)[-1]
        try:
            html = read_page(source, page_rel)
            messages, parse_errors = parse_page(html, source_file=page_name)
        except Exception as exc:
            # Catastrophic page-level failure (decode error, etc.). Log it
            # and move on — never let one bad file kill the whole chat.
            logger.exception("page read failed: {}", page_rel)
            _insert_parse_errors(
                chat_id=chat_id,
                source_folder=meta.source_folder,
                errors=[
                    ParseError(
                        source_file=page_name,
                        raw_html="",
                        error=f"{type(exc).__name__}: {exc}",
                        traceback="",
                    )
                ],
            )
            errors += 1
            continue

        inserted = _insert_messages(
            chat_id=chat_id,
            account_id=meta.account_id or "",
            messages=messages,
        )
        if parse_errors:
            _insert_parse_errors(
                chat_id=chat_id,
                source_folder=meta.source_folder,
                errors=parse_errors,
            )

        parsed_messages += inserted
        errors += len(parse_errors)

        _bump_chat_progress(chat_id, delta_messages=inserted, delta_errors=len(parse_errors))
        progress.report(
            page_idx, page_count,
            f"{chat_name}: page {page_idx}/{page_count} (+{inserted} msgs, {len(parse_errors)} err)",
        )

    _finalize_chat(chat_id=chat_id)

    return {
        "source_folder": meta.source_folder,
        "title": meta.title or (index_entry.title if index_entry else None),
        "parsed_messages": parsed_messages,
        "errors": errors,
    }


def _parse_single_page(
    source: Source,
    page_rel: str,
    chat_rel: str,
    chat_name: str,
    source_tz: str,
    index_entry: ChatIndexEntry | None,
) -> dict:
    """Parse one stand-alone messagesN.html page. Chat row is upserted using
    metadata read from the page; counters reflect only what this page
    contributed (the rest of the chat is not loaded).
    """
    page_name = page_rel.rsplit("/", 1)[-1]
    first_html = read_page(source, page_rel)
    meta = parse_chat_meta(first_html, source_folder=chat_name)
    chat_id = _upsert_chat(meta, source_tz=source_tz, index_entry=index_entry)
    try:
        messages, parse_errors = parse_page(first_html, source_file=page_name)
    except Exception as exc:
        logger.exception("page read failed: {}", page_rel)
        _insert_parse_errors(
            chat_id=chat_id,
            source_folder=meta.source_folder,
            errors=[
                ParseError(
                    source_file=page_name,
                    raw_html="",
                    error=f"{type(exc).__name__}: {exc}",
                    traceback="",
                )
            ],
        )
        return {
            "source_folder": meta.source_folder,
            "title": meta.title,
            "parsed_messages": 0,
            "errors": 1,
        }

    inserted = _insert_messages(
        chat_id=chat_id, account_id=meta.account_id or "", messages=messages
    )
    if parse_errors:
        _insert_parse_errors(
            chat_id=chat_id,
            source_folder=meta.source_folder,
            errors=parse_errors,
        )
    _bump_chat_progress(chat_id, delta_messages=inserted, delta_errors=len(parse_errors))
    _finalize_chat(chat_id=chat_id)
    return {
        "source_folder": meta.source_folder,
        "title": meta.title or (index_entry.title if index_entry else None),
        "parsed_messages": inserted,
        "errors": len(parse_errors),
    }


# ---------- DB helpers ----------


def _peer_id_from_folder(name: str) -> int | None:
    """Return the numeric peer id encoded in a chat folder name, or None
    if the folder uses a non-numeric slug.
    """
    if not name:
        return None
    body = name[1:] if name[0] in "-+" else name
    if not body.isdigit():
        return None
    return int(name) if name[0] != "+" else int(body)


def _upsert_chat(
    meta: ParsedChatMeta, source_tz: str, index_entry: ChatIndexEntry | None
) -> int:
    """Insert or update the chat row, return its primary key."""
    title = meta.title or (index_entry.title if index_entry else None)
    if index_entry is not None:
        peer_int = index_entry.peer_id
        peer_folder_name = index_entry.peer_folder
    else:
        peer_int = _peer_id_from_folder(meta.source_folder)
        peer_folder_name = meta.source_folder
    peer_id: str | None = str(peer_int) if peer_int is not None else None
    chat_type = chat_type_for_peer(peer_folder_name, peer_int)

    with connection() as conn:
        row = conn.execute(
            """
            INSERT INTO chats (
                provider, account_id, source_folder, title,
                total_expected_count, source_timezone, dump_generated_at,
                peer_id, type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (provider, account_id, source_folder) DO UPDATE SET
                title = COALESCE(excluded.title, chats.title),
                total_expected_count = COALESCE(excluded.total_expected_count, chats.total_expected_count),
                source_timezone = excluded.source_timezone,
                dump_generated_at = COALESCE(excluded.dump_generated_at, chats.dump_generated_at),
                peer_id = COALESCE(excluded.peer_id, chats.peer_id),
                type = COALESCE(excluded.type, chats.type)
            RETURNING id
            """,
            (
                PROVIDER,
                meta.account_id or "",
                meta.source_folder,
                title,
                meta.total_expected_count,
                source_tz,
                meta.dump_generated_at,
                peer_id,
                chat_type,
            ),
        ).fetchone()
        chat_id = int(row["id"])

        # If a chat row was pre-created from the messages index with
        # account_id='', migrate it to the real account_id we just learned
        # (i.e. delete the dangling stub so we don't carry two rows for the
        # same chat).
        if meta.account_id:
            conn.execute(
                """
                DELETE FROM chats
                 WHERE provider = ?
                   AND account_id = ''
                   AND source_folder = ?
                   AND id != ?
                """,
                (PROVIDER, meta.source_folder, chat_id),
            )
        return chat_id


def _bump_chat_progress(chat_id: int, delta_messages: int, delta_errors: int) -> None:
    """Live progress for GUI polling: persist per-page deltas to the chat row.

    The final values are recomputed authoritatively by :func:`_finalize_chat`.
    """
    if delta_messages == 0 and delta_errors == 0:
        return
    with connection() as conn:
        conn.execute(
            """
            UPDATE chats
               SET parsed_message_count = parsed_message_count + ?,
                   error_count = error_count + ?
             WHERE id = ?
            """,
            (delta_messages, delta_errors, chat_id),
        )


def _upsert_user_full(
    conn: sqlite3.Connection,
    vk_id: int,
    display_name: str | None,
    avatar_url: str | None = None,
) -> int:
    """Insert or update a users row for the given VK id, returning users.id."""
    profile_url = f"https://vk.com/id{vk_id}"
    is_deleted = 1 if (display_name or "").strip() == "DELETED" else 0
    row = conn.execute(
        """
        INSERT INTO users (provider, vk_id, display_name, profile_url, is_deleted, avatar_url)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (provider, vk_id) DO UPDATE SET
            display_name = COALESCE(excluded.display_name, users.display_name),
            profile_url = COALESCE(excluded.profile_url, users.profile_url),
            is_deleted = MAX(users.is_deleted, excluded.is_deleted),
            avatar_url = COALESCE(excluded.avatar_url, users.avatar_url)
        RETURNING id
        """,
        (PROVIDER, vk_id, display_name, profile_url, is_deleted, avatar_url),
    ).fetchone()
    return int(row["id"])


def _resolve_sender(
    conn: sqlite3.Connection, m: ParsedMessage, account_vk_id: int | None
) -> tuple[int | None, int | None]:
    """Resolve a message's sender to (users.id, sender_vk_id).

    For "Вы" messages the parsed HTML carries no explicit vk_id; we substitute
    the dump owner's id (extracted earlier from the jd meta) so self-messages
    link to a real user row instead of getting a NULL sender.
    """
    vk_id = m.sender_vk_id
    if vk_id is None and m.sender_is_self:
        vk_id = account_vk_id
    if vk_id is None:
        return None, None
    return _upsert_user_full(conn, vk_id, m.sender_display_name), vk_id


def _insert_messages(
    chat_id: int, account_id: str, messages: Iterable[ParsedMessage]
) -> int:
    """Insert messages (and their attachments) for one page in a single
    transaction. Returns the count of *newly inserted* messages.
    """
    inserted = 0
    account_vk_id: int | None = int(account_id) if account_id.isdigit() else None
    with connection() as conn:
        for m in messages:
            user_id, resolved_vk_id = _resolve_sender(conn, m, account_vk_id)
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    provider, account_id, chat_id, vk_message_id,
                    sender_user_id, sender_vk_id, sender_display_name, sender_is_self,
                    sent_at, text, has_forwards, forwarded_count,
                    is_reply, reply_to_message_id, attachment_count,
                    source_file, raw_html
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    PROVIDER,
                    account_id,
                    chat_id,
                    m.vk_message_id,
                    user_id,
                    resolved_vk_id,
                    m.sender_display_name,
                    1 if m.sender_is_self else 0,
                    m.sent_at,
                    m.text,
                    1 if m.has_forwards else 0,
                    m.forwarded_count,
                    1 if m.is_reply else 0,
                    m.reply_to_message_id,
                    len(m.attachments),
                    m.source_file,
                    m.raw_html,
                ),
            )
            if cur.rowcount == 1 and cur.lastrowid:
                message_pk = cur.lastrowid
                inserted += 1
                for a in m.attachments:
                    conn.execute(
                        """
                        INSERT INTO attachments
                            (message_id, kind, description, url, forward_count, position)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message_pk,
                            a.kind,
                            a.description,
                            a.url,
                            a.forward_count,
                            a.position,
                        ),
                    )
    return inserted


def _insert_parse_errors(
    chat_id: int, source_folder: str, errors: Iterable[ParseError]
) -> None:
    with connection() as conn:
        for e in errors:
            conn.execute(
                """
                INSERT INTO parse_errors
                    (chat_id, source_folder, source_file, raw_html, error, traceback)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, source_folder, e.source_file, e.raw_html, e.error, e.traceback),
            )


def _finalize_chat(chat_id: int) -> None:
    """Refresh chat-level counters and denormalised summary fields from the
    authoritative row counts in `messages` / `parse_errors`.
    """
    with connection() as conn:
        agg = conn.execute(
            """
            SELECT COUNT(*) AS cnt,
                   MIN(sent_at) AS first_at,
                   MAX(sent_at) AS last_at
              FROM messages
             WHERE chat_id = ?
            """,
            (chat_id,),
        ).fetchone()
        last = conn.execute(
            """
            SELECT sent_at, text, sender_display_name
              FROM messages
             WHERE chat_id = ?
             ORDER BY sent_at DESC, id DESC
             LIMIT 1
            """,
            (chat_id,),
        ).fetchone()
        err_total = conn.execute(
            "SELECT COUNT(*) FROM parse_errors WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()[0]

        conn.execute(
            """
            UPDATE chats
               SET parsed_message_count = ?,
                   message_count = ?,
                   error_count = ?,
                   last_message_at = ?,
                   last_message_text = ?,
                   last_message_sender = ?,
                   created_at = ?,
                   last_parsed_at = CURRENT_TIMESTAMP
             WHERE id = ?
            """,
            (
                agg["cnt"],
                agg["cnt"],
                err_total,
                last["sent_at"] if last else None,
                (last["text"][:280] if last and last["text"] else None),
                last["sender_display_name"] if last else None,
                agg["first_at"],
                chat_id,
            ),
        )
