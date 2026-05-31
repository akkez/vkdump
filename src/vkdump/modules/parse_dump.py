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

import json
import sqlite3
import time
import unicodedata
from pathlib import Path
from typing import Callable, Iterable

from loguru import logger

from ..core.db import connection
from ..core.i18n import plural as _plural
from ..core.progress import ProgressReporter, Throttle
from ..parsers.vk import (
    ArchiveFooter,
    ChatIndexEntry,
    Discovery,
    ParsedChatMeta,
    ParsedMessage,
    ParseError,
    ProfileInfo,
    Source,
    chat_type_for_peer,
    decode_dump_bytes,
    discover,
    extract_account_id,
    list_message_pages,
    parse_archive_footer,
    parse_chat_meta,
    parse_messages_index,
    parse_messages_index_file,
    parse_page,
    parse_profile_file,
    read_page,
)
# `_parse_messages_index_str` reuses the same string-parsing function as
# `parse_messages_index_file`, but we pass the raw HTML in directly so we
# don't have to read the file twice (once for entries, once for jd).
_parse_messages_index_str = parse_messages_index

PROVIDER = "vk"


def run(params: dict, progress: ProgressReporter) -> dict:
    source_input = Path(params["source"]).expanduser()
    source_tz = (params.get("source_timezone") or "UTC").strip() or "UTC"

    # Remember the absolute source path so downstream tasks (save-chat)
    # can pre-fill their own dumpsource field from app_config instead of
    # asking the user to re-type it. Saved before the heavy work so even
    # a cancelled run leaves the hint behind.
    try:
        from ..core import app_config
        app_config.set("parse_dump.last_source", str(source_input.resolve()))
    except Exception:  # noqa: BLE001
        pass

    discovery = discover(source_input)
    try:
        return _run_with_discovery(discovery, source_tz, progress)
    finally:
        discovery.source.close()


def _run_with_discovery(
    discovery: Discovery, source_tz: str, progress: ProgressReporter
) -> dict:
    started_at = time.perf_counter()
    progress.log(_format_discovery(discovery))
    source = discovery.source

    # ---------- optional: profile (dump owner) ----------
    owner_vk_id: int | None = None
    owner_display_name: str | None = None
    owner_avatar_url: str | None = None
    if discovery.profile_file is not None:
        try:
            profile = parse_profile_file(source, discovery.profile_file)
            owner_vk_id = _apply_profile(profile)
            owner_display_name = profile.display_name
            owner_avatar_url = profile.avatar_url
            progress.log(
                f"profile: owner vk_id={profile.vk_id} name={profile.display_name!r} "
                f"avatar={'yes' if profile.avatar_url else 'no'}"
            )
        except Exception:
            logger.exception("profile parsing failed for {}", discovery.profile_file)
            progress.log(f"profile: failed to parse {discovery.profile_file} (continuing)")

    # ---------- optional: archive footer (signature + duration) ----------
    archive_footer: ArchiveFooter | None = None
    if discovery.index_file is not None:
        try:
            index_html = decode_dump_bytes(source.read_bytes(discovery.index_file))
            archive_footer = parse_archive_footer(index_html)
            if archive_footer is not None:
                progress.log(
                    f"archive footer: lang={archive_footer.lang} "
                    f"generated_at={archive_footer.generated_at} "
                    f"duration={archive_footer.duration_seconds}s"
                )
                if archive_footer.lang == "unknown" and archive_footer.raw_text:
                    # New locale variant — surface the raw text loudly
                    # (both progress log and a logger.warning) so the
                    # next regex extension knows what to match.
                    progress.log(
                        "archive footer: UNRECOGNISED — please extend"
                        " parsers/vk/footer.py with a regex covering:"
                        f" {archive_footer.raw_text!r}"
                    )
                    logger.warning(
                        "archive footer locale not recognised; raw_text={!r}",
                        archive_footer.raw_text,
                    )
        except Exception:
            logger.exception("archive footer parsing failed for {}", discovery.index_file)
            progress.log(f"archive footer: failed to parse {discovery.index_file} (continuing)")

    # Record the archive (and its owner) up front so the row exists even
    # if the parse below is cancelled mid-way.
    archive_id: int | None = None
    if owner_vk_id is not None and discovery.initial_input is not None:
        archive_id = _upsert_archive(
            owner_vk_id=owner_vk_id,
            owner_display_name=owner_display_name,
            owner_avatar_url=owner_avatar_url,
            initial_input=discovery.initial_input,
            source_tz=source_tz,
            footer=archive_footer,
        )

    # ---------- optional: messages index (chat titles + peer ids) ----------
    indexed_entries: dict[str, ChatIndexEntry] = {}
    # Dump owner stringified. Same value sits in the jd meta of every
    # HTML page in one archive, so any of profile / messages-index /
    # first chat page will yield it. We resolve it once and propagate.
    account_id: str = str(owner_vk_id) if owner_vk_id else ""
    if discovery.messages_index_file is not None:
        try:
            raw = decode_dump_bytes(source.read_bytes(discovery.messages_index_file))
            entries = _parse_messages_index_str(raw)
            if not account_id:
                index_owner = extract_account_id(raw)
                if index_owner:
                    account_id = index_owner
                    progress.log(f"account_id resolved from messages-index: {account_id}")
            indexed_entries = {e.peer_folder: e for e in entries}
            _preload_chats_from_index(entries, account_id=account_id)
            progress.log(f"messages-index: preloaded {_plural(len(entries), 'chat row')} with titles")
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
        # Pre-scan: list every messagesN.html across every chat folder up
        # front so the global progress bar reflects the real workload and
        # doesn't just tick over `len(chat_folders)` (which would be 1/40
        # while one big chat eats 99% of the time).
        chat_plans: list[tuple[str, str, list[str]]] = []
        global_total = 0
        for chat_rel in discovery.chat_folders:
            chat_name = chat_rel.rsplit("/", 1)[-1] or root_label or "chat"
            pages = list_message_pages(source, chat_rel)
            chat_plans.append((chat_rel, chat_name, pages))
            global_total += len(pages)
        progress.log(
            f"pre-scan: {_plural(len(chat_plans), 'chat folder')}, "
            f"{_plural(global_total, 'HTML page')} total"
        )
        logger.info(
            "parse_dump: scanning {}, {} pages",
            _plural(len(chat_plans), "chat folder"), global_total,
        )
        # Last-resort account_id resolution: if neither profile nor
        # messages-index supplied it, peek at the first chat's first
        # page jd meta. Same id, same archive.
        if not account_id and chat_plans:
            first_pages = chat_plans[0][2]
            if first_pages:
                try:
                    raw_first = decode_dump_bytes(source.read_bytes(first_pages[0]))
                    first_owner = extract_account_id(raw_first)
                    if first_owner:
                        account_id = first_owner
                        progress.log(f"account_id resolved from first chat jd: {account_id}")
                except Exception:
                    logger.exception("failed to peek at jd of {}", first_pages[0])

        # Clear prior parse errors only for chats we're about to re-parse.
        # Errors from chats outside this run (e.g. from a previous parse of
        # a different archive) remain inspectable via `vkdump logs list`.
        _truncate_parse_errors_for([name for _, name, _ in chat_plans])

        # We own our own bars; the orchestrator-level "Parse VK dump" row
        # would just be noise above them.
        progress.hide_main()
        global_done = 0
        global_total_or_one = max(global_total, 1)
        # One global-bar throttle for the whole run; per-chat chat-bar
        # throttle gets created inside the chat scope below.
        global_throttle = Throttle()

        # Two persistent bars rendered top-down in the terminal:
        #   1. [cyan]  current chat — relabelled per chat, progresses per page;
        #   2. [green] global       — total pages across the whole dump.
        # Sub-tasks render in the order they're added, so the chat scope is
        # opened first to land on top.
        with progress.sub(
            "[cyan]chat:[/cyan]", total=max(1, len(chat_plans[0][2]) if chat_plans else 1),
        ) as chat_bar:
            with progress.sub(
                "[green]Total progress:[/green]",
                total=global_total_or_one,
            ) as global_bar:
                for chat_idx, (chat_rel, chat_name, pages) in enumerate(chat_plans, start=1):
                    progress.check_cancelled()
                    index_entry = indexed_entries.get(chat_name)
                    chat_label = _format_chat_label(chat_name, index_entry)
                    title_suffix = (
                        f" — {_strip_invisible(index_entry.title).strip()}"
                        if index_entry and index_entry.title
                        else ""
                    )
                    progress.log(
                        f"[{chat_idx}/{len(chat_plans)}] chat: {chat_name}"
                        f"{title_suffix} ({_plural(len(pages), 'page')})"
                    )
                    page_count_for_chat = max(len(pages), 1)
                    chat_bar.report(
                        0, page_count_for_chat,
                        f"[cyan]chat:[/cyan] {chat_label}",
                    )

                    def _on_page_done(
                        _label: str = chat_label,
                    ) -> None:
                        nonlocal global_done
                        global_done += 1
                        if global_throttle(global_done, global_total_or_one):
                            global_bar.report(
                                global_done, global_total_or_one,
                                "",
                            )

                    summary = _parse_one_chat(
                        source=source,
                        chat_rel=chat_rel,
                        chat_name=chat_name,
                        pages=pages,
                        progress=chat_bar,
                        index_entry=index_entry,
                        account_id=account_id,
                        on_page_done=_on_page_done,
                    )
                    total_messages += summary["parsed_messages"]
                    total_errors += summary["errors"]
                    chat_summaries.append(summary)
                global_bar.report(
                    global_total_or_one, global_total_or_one,
                    "",
                )

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
                index_entry=indexed_entries.get(chat_name),
                account_id=account_id,
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

    elapsed = time.perf_counter() - started_at
    aggregates = _aggregate_db_summary()
    return {
        "source": source.describe(),
        "owner_vk_id": owner_vk_id,
        "elapsed_seconds": round(elapsed, 2),
        "chats_parsed_this_run": len(chat_summaries),
        "chats_in_index": len(indexed_entries),
        "messages_inserted_this_run": total_messages,
        "errors_this_run": total_errors,
        "totals": aggregates["totals"],
        "top_chats": aggregates["top_chats"],
        "top_senders": aggregates["top_senders"],
    }


def _aggregate_db_summary() -> dict:
    """Compute a small summary over the current DB state — used as the
    final result of parse-dump so the CLI doesn't have to dump hundreds of
    per-chat entries.
    """
    with connection() as conn:
        totals_row = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM chats)          AS chats,
                (SELECT COUNT(*) FROM users)          AS users,
                (SELECT COUNT(*) FROM messages)       AS messages,
                (SELECT COUNT(*) FROM attachments)    AS attachments,
                (SELECT COUNT(*) FROM parse_errors)   AS parse_errors
            """
        ).fetchone()
        top_chats = [
            {
                "title": r["title"] or r["source_folder"],
                "messages": r["message_count"],
                "type": r["type"],
            }
            for r in conn.execute(
                """
                SELECT title, source_folder, message_count, type
                  FROM chats
                 WHERE message_count > 0
                 ORDER BY message_count DESC
                 LIMIT 10
                """
            )
        ]
        top_senders = [
            {
                "vk_id": r["vk_id"],
                "display_name": r["display_name"],
                "messages": r["c"],
            }
            for r in conn.execute(
                """
                SELECT u.vk_id, u.display_name, COUNT(*) AS c
                  FROM messages m
                  JOIN users u ON u.id = m.sender_user_id
                 GROUP BY u.id
                 ORDER BY c DESC
                 LIMIT 10
                """
            )
        ]
    return {
        "totals": dict(totals_row),
        "top_chats": top_chats,
        "top_senders": top_senders,
    }


def _format_discovery(d: Discovery) -> str:
    parts = [f"discovery: source={d.source.describe()}"]
    if d.index_file:
        parts.append(f"index={d.index_file}")
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


def _upsert_archive(
    owner_vk_id: int,
    owner_display_name: str | None,
    owner_avatar_url: str | None,
    initial_input: Path,
    source_tz: str,
    footer: ArchiveFooter | None,
) -> int:
    """Upsert one row each into `accounts` and `archives`, return the
    archive's primary key.

    Identity for archives is the footer signature
    (``account_id + generated_at + duration``) when available — that
    lets the same dump re-imported from a different path collapse onto
    one row. When no signature is parsed we fall back to
    ``UNIQUE(account_id, path)``.
    """
    abs_path = initial_input.expanduser().resolve()
    kind = "zip" if abs_path.is_file() else "folder"
    with connection() as conn:
        conn.execute(
            """
            INSERT INTO accounts (provider, vk_id, display_name, avatar_url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (provider, vk_id) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, accounts.display_name),
                avatar_url   = COALESCE(excluded.avatar_url,   accounts.avatar_url)
            """,
            (PROVIDER, owner_vk_id, owner_display_name, owner_avatar_url),
        )
        account_row = conn.execute(
            "SELECT id FROM accounts WHERE provider=? AND vk_id=?",
            (PROVIDER, owner_vk_id),
        ).fetchone()
        account_id = int(account_row["id"])

        path_str = str(abs_path)
        lang = footer.lang if footer else "unknown"
        sig = footer.raw_text if footer else None
        gen_at = footer.generated_at if footer else None
        dur = footer.duration_seconds if footer else None

        existing = None
        if gen_at is not None and dur is not None:
            existing = conn.execute(
                """
                SELECT id FROM archives
                 WHERE account_id=? AND generated_at=? AND generation_duration_seconds=?
                """,
                (account_id, gen_at, dur),
            ).fetchone()
        if existing is None:
            existing = conn.execute(
                "SELECT id FROM archives WHERE account_id=? AND path=?",
                (account_id, path_str),
            ).fetchone()

        if existing is not None:
            arch_id = int(existing["id"])
            conn.execute(
                """
                UPDATE archives
                   SET kind=?, path=?, path_is_absolute=1, lang=?,
                       signature_text=COALESCE(?, signature_text),
                       generated_at=COALESCE(?, generated_at),
                       generation_duration_seconds=COALESCE(?, generation_duration_seconds),
                       source_timezone=?,
                       last_seen_at=CURRENT_TIMESTAMP
                 WHERE id=?
                """,
                (kind, path_str, lang, sig, gen_at, dur, source_tz, arch_id),
            )
            return arch_id

        cur = conn.execute(
            """
            INSERT INTO archives (
                account_id, kind, path, path_is_absolute, lang,
                signature_text, generated_at, generation_duration_seconds,
                source_timezone, last_seen_at
            )
            VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (account_id, kind, path_str, lang, sig, gen_at, dur, source_tz),
        )
        return int(cur.lastrowid)


def _preload_chats_from_index(
    entries: list[ChatIndexEntry], account_id: str
) -> None:
    """Insert chat rows we know about from the index, even before parsing any
    individual chat folder. Title + peer_id + type land immediately; counters
    stay at their defaults until the per-folder parse runs.

    `account_id` is the dump owner's vk_id (or "" only as a last-resort
    fallback when we couldn't resolve it from profile / index jd).
    """
    with connection() as conn:
        for e in entries:
            chat_type = chat_type_for_peer(e.peer_folder, e.peer_id)
            conn.execute(
                """
                INSERT INTO chats (
                    provider, account_id, source_folder, title,
                    peer_id, type
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (provider, account_id, source_folder) DO UPDATE SET
                    title = COALESCE(excluded.title, chats.title),
                    peer_id = COALESCE(excluded.peer_id, chats.peer_id),
                    type = COALESCE(excluded.type, chats.type)
                """,
                (
                    PROVIDER,
                    account_id,
                    e.peer_folder,
                    e.title,
                    str(e.peer_id) if e.peer_id is not None else None,
                    chat_type,
                ),
            )


# ---------- per-chat ----------


def _parse_one_chat(
    source: Source,
    chat_rel: str,
    chat_name: str,
    pages: list[str],
    progress: ProgressReporter,
    index_entry: ChatIndexEntry | None,
    account_id: str,
    on_page_done: "Callable[[], None] | None" = None,
) -> dict:
    if not pages:
        return {
            "source_folder": chat_name,
            "title": index_entry.title if index_entry else None,
            "parsed_messages": 0,
            "errors": 0,
        }

    first_html = read_page(source, pages[0])
    meta = parse_chat_meta(first_html, source_folder=chat_name)
    # Trust the resolved-once `account_id` over whatever jd says inside
    # this particular chat — same archive must reduce to one owner.
    effective_account_id = account_id or meta.account_id or ""
    chat_id = _upsert_chat(
        meta, index_entry=index_entry,
        account_id=effective_account_id,
    )

    page_count = len(pages)
    parsed_messages = 0
    errors = 0

    # Keep a single sqlite connection open for the entire chat. Profiling
    # showed connection open/close/commit was ~50% of total wall time when
    # we opened a fresh conn per page; one conn per chat with explicit
    # per-page commits roughly halves it while keeping live progress
    # visible (each page's data lands as soon as the page completes).
    chat_throttle = Throttle()
    with connection() as conn:
        for page_idx, page_rel in enumerate(pages, start=1):
            progress.check_cancelled()
            page_name = page_rel.rsplit("/", 1)[-1]
            try:
                html = read_page(source, page_rel)
                messages, parse_errors = parse_page(html, source_file=page_name)
            except Exception as exc:
                logger.exception("page read failed: {}", page_rel)
                _insert_parse_errors_conn(
                    conn,
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
                conn.commit()
                continue

            inserted = _insert_messages_conn(
                conn,
                chat_id=chat_id,
                account_id=effective_account_id,
                messages=messages,
            )
            if parse_errors:
                _insert_parse_errors_conn(
                    conn,
                    chat_id=chat_id,
                    source_folder=meta.source_folder,
                    errors=parse_errors,
                )

            parsed_messages += inserted
            errors += len(parse_errors)

            _bump_chat_progress_conn(
                conn, chat_id, delta_messages=inserted, delta_errors=len(parse_errors)
            )
            conn.commit()
            if chat_throttle(page_idx, page_count):
                # Update the label every tick with the live `<done>/<total>`
                # so the GUI's sub-label tracks progress (the bar widget
                # itself shows `%v / %m %p%` but the label is the headline
                # users glance at). Monotonic, not flickery.
                chat_disp = _format_chat_label(chat_name, index_entry)
                progress.report(
                    page_idx, page_count,
                    f"[cyan]chat:[/cyan] {chat_disp}",
                )
            if on_page_done is not None:
                on_page_done()

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
    index_entry: ChatIndexEntry | None,
    account_id: str,
) -> dict:
    """Parse one stand-alone messagesN.html page. Chat row is upserted using
    metadata read from the page; counters reflect only what this page
    contributed (the rest of the chat is not loaded).
    """
    page_name = page_rel.rsplit("/", 1)[-1]
    first_html = read_page(source, page_rel)
    meta = parse_chat_meta(first_html, source_folder=chat_name)
    effective_account_id = account_id or meta.account_id or ""
    chat_id = _upsert_chat(
        meta, index_entry=index_entry,
        account_id=effective_account_id,
    )
    with connection() as conn:
        try:
            messages, parse_errors = parse_page(first_html, source_file=page_name)
        except Exception as exc:
            logger.exception("page read failed: {}", page_rel)
            _insert_parse_errors_conn(
                conn,
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

        inserted = _insert_messages_conn(
            conn, chat_id=chat_id, account_id=effective_account_id, messages=messages
        )
        if parse_errors:
            _insert_parse_errors_conn(
                conn,
                chat_id=chat_id,
                source_folder=meta.source_folder,
                errors=parse_errors,
            )
        _bump_chat_progress_conn(
            conn, chat_id, delta_messages=inserted, delta_errors=len(parse_errors)
        )
    _finalize_chat(chat_id=chat_id)
    return {
        "source_folder": meta.source_folder,
        "title": meta.title or (index_entry.title if index_entry else None),
        "parsed_messages": inserted,
        "errors": len(parse_errors),
    }


# ---------- DB helpers ----------


def _format_chat_label(chat_name: str, index_entry: ChatIndexEntry | None) -> str:
    """`<peer_id> <title>`, truncated so it fits on a typical terminal line.

    Title comes from the messages index when we have it (better than the
    folder name for human-named confs). The peer id stays in front so you
    can grep the live progress feed for a specific chat.

    Control / format / surrogate / private-use characters are stripped from
    the title before display — they tend to render as zero width in the
    terminal while still being counted by Rich's cell-width measurement,
    which makes the progress bar overflow by exactly one cell and wrap to
    a new line on every update.
    """
    title = index_entry.title if index_entry and index_entry.title else None
    if not title or title == chat_name:
        return chat_name
    title = _strip_invisible(title).strip()
    if not title:
        return chat_name
    max_total = 60
    head = f"{chat_name} "
    remaining = max_total - len(head)
    if remaining <= 3:
        return chat_name
    if len(title) > remaining:
        title = title[: remaining - 1].rstrip() + "…"
    return head + title


def _strip_invisible(s: str) -> str:
    """Remove unicode categories C* — control, format (ZWJ/ZWNJ/ZWSP/BOM
    and friends), surrogate, private-use, unassigned. They confuse
    terminal width measurement.
    """
    return "".join(ch for ch in s if not unicodedata.category(ch).startswith("C"))


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
    meta: ParsedChatMeta,
    index_entry: ChatIndexEntry | None,
    account_id: str,
) -> int:
    """Insert or update the chat row, return its primary key.

    `account_id` is the resolved dump owner — same value the index
    preload used, so the per-folder parse hits the SAME row (no
    dangling stub with account_id='').
    """
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
                total_expected_count, peer_id, type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (provider, account_id, source_folder) DO UPDATE SET
                title = COALESCE(excluded.title, chats.title),
                total_expected_count = COALESCE(excluded.total_expected_count, chats.total_expected_count),
                peer_id = COALESCE(excluded.peer_id, chats.peer_id),
                type = COALESCE(excluded.type, chats.type)
            RETURNING id
            """,
            (
                PROVIDER,
                account_id,
                meta.source_folder,
                title,
                meta.total_expected_count,
                peer_id,
                chat_type,
            ),
        ).fetchone()
        chat_id = int(row["id"])

        # Sweep up any leftover account_id='' stub from an older run
        # that pre-loaded before we could resolve the owner.
        if account_id:
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


def _truncate_parse_errors_for(source_folders: list[str]) -> None:
    """Wipe parse_errors and reset error_count only for the given chats.

    Errors from chats outside this list (e.g. parsed earlier from a
    different dump) stay in place, so `vkdump logs list` still shows them.
    """
    if not source_folders:
        return
    placeholders = ",".join("?" * len(source_folders))
    with connection() as conn:
        conn.execute(
            f"DELETE FROM parse_errors WHERE source_folder IN ({placeholders})",
            source_folders,
        )
        conn.execute(
            f"""
            UPDATE chats
               SET error_count = 0
             WHERE source_folder IN ({placeholders})
            """,
            source_folders,
        )


def _bump_chat_progress_conn(
    conn: sqlite3.Connection, chat_id: int, delta_messages: int, delta_errors: int
) -> None:
    """Live progress for GUI polling: persist per-page deltas to the chat row.

    The final values are recomputed authoritatively by :func:`_finalize_chat`.
    """
    if delta_messages == 0 and delta_errors == 0:
        return
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


def _insert_messages_conn(
    conn: sqlite3.Connection,
    chat_id: int,
    account_id: str,
    messages: Iterable[ParsedMessage],
) -> int:
    """Insert messages (and their attachments) for one page on the given
    connection. Caller owns commit / rollback. Returns count of *newly
    inserted* messages.
    """
    inserted = 0
    account_vk_id: int | None = int(account_id) if account_id.isdigit() else None
    for m in messages:
            user_id, resolved_vk_id = _resolve_sender(conn, m, account_vk_id)
            # Skip storing raw_html when the parser captured every field
            # losslessly — the SQLite row is then a complete representation
            # on its own and the duplicate HTML would just bloat the store.
            persisted_raw = None if m.fully_parsed else m.raw_html
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO messages (
                    provider, account_id, chat_id, vk_message_id,
                    sender_user_id, sender_vk_id, sender_display_name, sender_is_self,
                    sent_at, text, has_forwards, forwarded_count,
                    is_reply, reply_to_message_id, attachment_count,
                    is_edited, edited_at,
                    source_file, raw_html
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    1 if m.is_edited else 0,
                    m.edited_at,
                    m.source_file,
                    persisted_raw,
                ),
            )
            if cur.rowcount == 1 and cur.lastrowid:
                message_pk = cur.lastrowid
                inserted += 1
                for a in m.attachments:
                    conn.execute(
                        """
                        INSERT INTO attachments
                            (message_id, kind, description, url, forward_count, position, data_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            message_pk,
                            a.kind,
                            a.description,
                            a.url,
                            a.forward_count,
                            a.position,
                            json.dumps(a.data, ensure_ascii=False) if a.data else None,
                        ),
                    )
    return inserted


def _insert_parse_errors_conn(
    conn: sqlite3.Connection,
    chat_id: int,
    source_folder: str,
    errors: Iterable[ParseError],
) -> None:
    for e in errors:
        # Mirror into the file logger so the failure shows up in
        # vkdump.log / vkdump-errors.log on disk, not only in the DB.
        # The DB row carries raw_html + traceback; the log line is the
        # one-shot summary humans need when tailing.
        logger.warning(
            "parse error in {}/{}: {}",
            source_folder, e.source_file, e.error,
        )
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
