"""save-chat orchestrator.

End-to-end:
1. Resolve which chat to render (by peer_id) → its `chats.source_folder`.
2. Open the original VK dump via the parser's `discover()` so we can read
   the raw `messagesN.html` bytes — same Source abstraction used by
   parse-dump, so ZIPs and directories work identically.
3. For every page: walk message blocks with `_iter_message_blocks`, parse
   each into a `ParsedMessage`, run the Transform pipeline (currently
   just inline photos), splice the (possibly mutated) `raw_html` blocks
   back where they came from. Everything *between* blocks (header, page
   footer, pagination links) is forwarded verbatim — none of it ever
   passes through Python string manipulation.
4. Write each transformed page to `<output>/<chat_slug>/messagesN.html`,
   copy assets to `<output>/<chat_slug>/assets/...`, update the
   top-level `index.html` + `_export.json` manifest so re-exporting the
   same chat updates the entry instead of duplicating it.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from loguru import logger

from ...core.app_config import set as cfg_set
from ...core.db import app_dir, connection
from ...core.progress import Cancelled, ProgressReporter, Throttle
from ...parsers.vk.discovery import discover
from ...parsers.vk.messages import (
    _iter_message_blocks,
    _parse_one_message,
    parse_chat_meta,
    read_page,
)
from ...parsers.vk.pages import is_message_page_filename, list_message_pages
from ...parsers.vk.sources import join as source_join
from .index import find_slug_for_chat, upsert as upsert_index
from .pipeline import AttMeta, TransformContext, apply_pipeline
from .transforms import DEFAULT_TRANSFORMS


# VK dumps declare `windows-1251` in their `<meta>` tag. We decode them
# correctly on read, but write the output as UTF-8 — so the meta must
# follow or browsers mojibake. Both attribute orderings show up in the
# wild; case-insensitive match handles both.
_META_CHARSET_RE = re.compile(
    r"(?i)(<meta[^>]*charset\s*=\s*['\"]?)(windows-1251|cp1251)(['\"]?)"
)

# CSS/JS bundled at the archive root that messagesN.html references.
# We copy these per-chat-folder so each export dir is self-contained,
# then rewrite stylesheet/script refs in the HTML to just the basename.
_ASSET_EXTS = (".css", ".js")

# Anything outside letters (any script, so Cyrillic counts) / digits
# / one of these few safe punctuation marks gets collapsed to a single
# `x` in the folder slug. `\w` already includes `_`; we add `-` and `.`
# explicitly. Length-capped downstream.
_TITLE_SAFE_RE = re.compile(r"[^\w\-.]+", re.UNICODE)
_TITLE_MAX = 60  # leaves headroom under Windows MAX_PATH after the
                 # "<chatid>_" prefix and the longest expected
                 # `assets/<year>/photos/<sha[:2]>/<sha>.jpg` suffix.


def _safe_title_slug(title: str | None) -> str:
    """Sanitise a chat title for use inside a folder name.

    Keeps Latin + Cyrillic letters (`\\w` under re.UNICODE), digits, `_`,
    `-` and `.`. Whitespace runs collapse to a single underscore (so
    word boundaries stay readable). Runs of anything else collapse to
    a single `x`. Length-capped and trimmed.
    """
    if not title:
        return ""
    # Whitespace → underscore first, so "Иван Петров" reads as
    # "Иван_Петров" rather than "ИванxПетров".
    s = re.sub(r"\s+", "_", title.strip())
    s = _TITLE_SAFE_RE.sub("x", s)
    # Strip leading dots / dashes so the folder doesn't read as hidden
    # on POSIX or trip path-traversal heuristics.
    s = s.lstrip(".-_x")[:_TITLE_MAX].rstrip(".-_x")
    return s


def _chat_folder_slug(chat_id: int, title: str | None) -> str:
    """Combine the DB id with a sanitised title into the on-disk slug.
    `chat_id` always leads so the folder is alphabetically stable and
    re-exports of the same chat with a renamed title collide cleanly
    on the chat_id prefix.
    """
    safe = _safe_title_slug(title)
    return f"{chat_id}_{safe}" if safe else str(chat_id)


def run(params: dict, progress: ProgressReporter) -> dict:
    source_input = Path(params["source"]).expanduser()
    chat_pick = str(params["chat"]).strip()
    output_input = Path(params["output"]).expanduser()
    if not chat_pick:
        raise ValueError("Chat is required")
    output_dir = output_input.resolve()

    # Remember inputs so the next save-chat run pre-fills them.
    try:
        cfg_set("save_chat.last_source", str(source_input.resolve()))
        cfg_set("save_chat.last_output", str(output_dir))
        cfg_set("save_chat.last_chat", chat_pick)
    except Exception:  # noqa: BLE001
        pass

    # The picker hands us `chats.id` (unambiguous across accounts).
    try:
        chat_id = int(chat_pick)
    except ValueError as exc:
        raise ValueError(f"Bad chat id from picker: {chat_pick!r}") from exc
    chat_meta = _lookup_chat(chat_id)
    if chat_meta is None:
        raise ValueError(f"No chat in DB with id={chat_id}")

    discovery = discover(source_input)
    try:
        return _render_chat(
            discovery=discovery,
            chat_meta=chat_meta,
            output_dir=output_dir,
            progress=progress,
        )
    finally:
        discovery.source.close()


# ---------- internals ----------


def _copy_archive_assets(source, chat_out: Path) -> list[str]:
    """Copy `*.css` / `*.js` from the dump root into the chat output
    folder. Returns the basenames copied — used to drive the href
    rewrite that flattens `../../style.css` to plain `style.css`.

    Why per-chat instead of one shared `<output>/style.css`: each chat
    folder ends up self-contained, so the user can zip/move/share one
    chat without dragging an out-of-folder dependency.
    """
    copied: list[str] = []
    try:
        names = source.listdir("")
    except Exception:  # noqa: BLE001
        return copied
    for name in names:
        if not name.lower().endswith(_ASSET_EXTS):
            continue
        if not source.is_file(name):
            continue
        try:
            data = source.read_bytes(name)
        except Exception:  # noqa: BLE001
            continue
        dst = chat_out / name
        if dst.exists() and dst.stat().st_size == len(data):
            copied.append(name)
            continue
        dst.write_bytes(data)
        copied.append(name)
    return copied


def _assert_dump_matches_chat(source, first_page_rel: str, chat_meta: dict) -> None:
    """Read the first page's jd meta and confirm its account_id matches
    the DB chat we're about to render. Different accounts can re-use
    `source_folder` values so this is the only way to spot a wrong-dump
    pick before bytes hit the output dir.
    """
    try:
        html = read_page(source, first_page_rel)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"Could not read {first_page_rel} to verify dump identity: {exc}"
        ) from exc
    meta = parse_chat_meta(html, source_folder=chat_meta["source_folder"])
    dump_account = (meta.account_id or "").strip()
    db_account = str(chat_meta["account_id"] or "").strip()
    if not dump_account:
        # No meta in the page — let it through with a warning; older
        # exports / partial archives sometimes lack it.
        logger.warning(
            "save-chat: dump page {} has no account_id in meta; cannot cross-check",
            first_page_rel,
        )
        return
    if dump_account != db_account:
        raise ValueError(
            "Account mismatch: the dump at "
            f"{source.describe()!r} belongs to account_id={dump_account!r},"
            f" but the picked chat (id={chat_meta['id']},"
            f" peer_id={chat_meta['peer_id']!r}, source_folder="
            f"{chat_meta['source_folder']!r}) is stored under"
            f" account_id={db_account!r}."
            " Re-pick the chat (the picker label shows acct=…) or point"
            " at the matching dump."
        )


def _lookup_chat(chat_id: int) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT id, peer_id, account_id, provider, title, type,"
            " source_folder, message_count"
            " FROM chats WHERE id = ?",
            (chat_id,),
        ).fetchone()
    return dict(row) if row else None


def _build_url_meta(chat_id: int) -> dict[str, AttMeta]:
    """One DB pass over this chat's downloaded attachments. Returns
    `{url: AttMeta(local_path, resolution, file_size)}` so the renderer
    can build a richer `<img alt>` without per-attachment lookups.
    """
    out: dict[str, AttMeta] = {}
    with connection() as conn:
        for row in conn.execute(
            "SELECT a.url, a.local_path, a.resolution, a.file_size"
            " FROM attachments a"
            " JOIN messages m ON m.id = a.message_id"
            " WHERE m.chat_id = ?"
            "   AND a.download_status = 'ok'"
            "   AND a.local_path IS NOT NULL"
            "   AND a.url IS NOT NULL",
            (chat_id,),
        ):
            url = row[0]
            local = row[1]
            if url and local:
                out[url] = AttMeta(
                    local_path=local,
                    resolution=row[2],
                    file_size=row[3],
                )
    return out


def _find_chat_rel(discovery, source_folder: str) -> str | None:
    """Match a DB-stored `source_folder` (basename) to one of the rel
    paths discovery returned (which include the `messages/` prefix).
    Falls back to a direct basename probe if discovery missed the
    folder for some reason.
    """
    target = source_folder.strip("/")
    for rel in discovery.chat_folders:
        if rel == target or rel.endswith("/" + target) or Path(rel).name == target:
            return rel
    # Direct probe — `discover` may have skipped folders that don't
    # currently contain messagesN.html (rare on partial archives).
    candidates = [target, source_join("messages", target)]
    for c in candidates:
        if discovery.source.is_dir(c):
            return c
    return None


def _render_chat(
    *,
    discovery,
    chat_meta: dict,
    output_dir: Path,
    progress: ProgressReporter,
) -> dict:
    source = discovery.source
    chat_rel = _find_chat_rel(discovery, chat_meta["source_folder"])
    if chat_rel is None:
        raise FileNotFoundError(
            f"Chat folder {chat_meta['source_folder']!r} not found under {source.describe()}"
        )

    pages = list_message_pages(source, chat_rel)
    if not pages:
        raise FileNotFoundError(f"No messagesN.html pages under {chat_rel!r}")

    # Hard sanity check: the same source_folder name (e.g. "2000000004")
    # can exist under TWO different dumps from TWO different VK
    # accounts. Cross-check the dump's own jd-meta account_id against
    # what the DB row says — bail loudly before writing anything if
    # they disagree, otherwise we'd silently render a stranger's chat
    # into the user's export.
    _assert_dump_matches_chat(source, pages[0], chat_meta)

    url_to_meta = _build_url_meta(chat_meta["id"])
    progress.log(
        f"save-chat: chat {chat_meta['peer_id']!r} → {len(pages)} page(s),"
        f" {len(url_to_meta)} downloaded photo URL(s) to inline"
    )

    # Folder layout (mirrors the VK archive's relative depth so the
    # original `<link href="../../style.css">` resolves without href
    # rewriting):
    #   <output>/
    #     index.html
    #     style.css          ← shared, copied from archive root
    #     <chat_id>/
    #       assets/<year>/photos/<sha[:2]>/...
    #       messages/messages0.html, messages1.html, …
    # Slug = `<chats.id>_<sanitised title>` so two different accounts'
    # chats with the same peer_id can't overwrite each other, and the
    # folder name is human-skimmable. If this chat was exported here
    # before under a different title, reuse the original slug instead
    # of stranding the old folder.
    chat_slug = (
        find_slug_for_chat(output_dir, chat_meta["id"])
        or _chat_folder_slug(chat_meta["id"], chat_meta.get("title"))
    )
    chat_out = (output_dir / chat_slug).resolve()
    pages_dir = chat_out / "messages"
    pages_dir.mkdir(parents=True, exist_ok=True)

    # CSS/JS at the shared output root (same place VK puts it relative
    # to the archive). Pages keep their original `../../style.css`-style
    # refs verbatim — they resolve correctly at this depth.
    copied_assets = _copy_archive_assets(source, output_dir)
    if copied_assets:
        progress.log(
            f"save-chat: copied {len(copied_assets)} CSS/JS file(s) from dump root"
        )

    static_root = app_dir() / "data" / "static"
    ctx = TransformContext(
        chat_id=chat_meta["id"],
        chat_source_folder=chat_meta["source_folder"],
        output_chat_dir=chat_out,
        pages_dir=pages_dir,
        static_root=static_root,
        url_to_meta=url_to_meta,
        log=progress.log,
    )

    total = len(pages)
    throttle = Throttle()
    blocks_total = 0
    blocks_changed = 0
    first_page_name = ""
    progress.report(0, total, "Rendering pages…")
    for i, page_rel in enumerate(pages, start=1):
        progress.check_cancelled()
        try:
            # VK dumps are Windows-1251 — same helper parse-dump uses,
            # so cyrillic month names parse correctly.
            html = read_page(source, page_rel)
        except Exception as exc:  # noqa: BLE001
            logger.exception("save-chat: failed to read {}", page_rel)
            progress.log(f"save-chat: skipping {page_rel}: {exc}")
            continue
        new_html, n_blocks, n_changed = _transform_page(html, page_rel, ctx)
        blocks_total += n_blocks
        blocks_changed += n_changed
        # Rewrite the source's `<meta charset=windows-1251>` to utf-8
        # so the browser doesn't mojibake what we just decoded cleanly.
        new_html = _META_CHARSET_RE.sub(r"\1utf-8\3", new_html)
        page_name = Path(page_rel).name
        if not is_message_page_filename(page_name):
            page_name = f"messages{i}.html"
        (pages_dir / page_name).write_text(new_html, encoding="utf-8")
        if not first_page_name:
            first_page_name = page_name
        if throttle(i, total):
            progress.report(
                i, total,
                f"page {i}/{total} · blocks {blocks_total} · inlined {blocks_changed}",
            )

    progress.report(
        total, total,
        f"done · {blocks_total} blocks · {blocks_changed} inlined",
    )

    # Match-rate summary. Counters were bumped during the render pass
    # itself (free, no extra DB scan), so this is instant on big chats.
    # Photo-only for now — when video/audio enrich lands the same dict
    # gains new keys without any rewrite here.
    #
    # The three numbers live in different units, which used to confuse
    # readers (inlined > unique-downloaded looks impossible at first
    # glance):
    #   - mentions: per-attachment occurrences in messages — a single
    #               photo forwarded 5 times counts as 5
    #   - inlined:  same per-attachment unit, how many of those mentions
    #               got a local <img> tag written
    #   - unique:   distinct URLs with a local file on disk (deduped) —
    #               a forwarded photo at the same URL is 1 file
    photo_mentions = ctx.candidates_by_kind.get("photo", 0)
    photo_inlined = ctx.injected_by_kind.get("photo", 0)
    photo_unique = len(ctx.url_to_meta)
    photo_pct = (
        f"{(photo_inlined / photo_mentions * 100):.1f}%"
        if photo_mentions else "n/a"
    )
    progress.log(
        f"save-chat: photos — {photo_inlined} of {photo_mentions} photos inlined"
        f" ({photo_pct}) · backed by {photo_unique} unique local file(s)"
    )

    manifest = upsert_index(
        output_dir=output_dir,
        chat_slug=chat_slug,
        peer_id=str(chat_meta["peer_id"]),
        title=chat_meta["title"] or chat_slug,
        type_=chat_meta["type"] or "?",
        message_count=int(chat_meta["message_count"] or 0),
        first_page=f"messages/{first_page_name}" if first_page_name else "",
        chat_id=int(chat_meta["id"]),
    )
    progress.log(
        f"save-chat: wrote {chat_out} · index now lists {len(manifest.chats)} chat(s)"
    )

    return {
        "chat_slug": chat_slug,
        "peer_id": str(chat_meta["peer_id"]),
        "pages": total,
        "blocks": blocks_total,
        "blocks_changed": blocks_changed,
        "output_dir": str(output_dir),
        "chat_dir": str(chat_out),
        "assets_copied": len(ctx.copied_assets),
        # Per-attachment mention counts (a photo forwarded N times = N).
        "photo_mentions": photo_mentions,
        "photo_inlined": photo_inlined,
        # Distinct URLs backed by a downloaded local file (deduped).
        "photo_unique_files": photo_unique,
        "photo_match_pct": (
            round(photo_inlined / photo_mentions * 100, 1)
            if photo_mentions else None
        ),
    }


def _transform_page(html: str, page_rel: str, ctx: TransformContext) -> tuple[str, int, int]:
    """Walk `<div class="message">` blocks in `html`; for each, parse +
    pipeline, splice the (possibly mutated) raw_html back. Returns
    (new_page_html, total_blocks, blocks_that_changed).
    """
    out_parts: list[str] = []
    cursor = 0
    total = 0
    changed = 0
    for vk_id, b_start, b_end, inner in _iter_message_blocks(html):
        total += 1
        out_parts.append(html[cursor:b_start])
        raw_block = html[b_start:b_end]
        try:
            msg = _parse_one_message(vk_id, inner, raw_block, page_rel)
        except Exception as exc:  # noqa: BLE001
            logger.warning("save-chat: parse failed on block {} ({}): {}", vk_id, page_rel, exc)
            out_parts.append(raw_block)
            cursor = b_end
            continue
        before = msg.raw_html
        msg = apply_pipeline(msg, DEFAULT_TRANSFORMS, ctx)
        if msg.raw_html != before:
            changed += 1
        out_parts.append(msg.raw_html)
        cursor = b_end
    out_parts.append(html[cursor:])
    return "".join(out_parts), total, changed
