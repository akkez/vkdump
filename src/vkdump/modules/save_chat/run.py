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
from ...parsers.vk.messages import _iter_message_blocks, _parse_one_message
from ...parsers.vk.pages import is_message_page_filename, list_message_pages
from ...parsers.vk.sources import join as source_join
from .index import upsert as upsert_index
from .pipeline import TransformContext, apply_pipeline
from .transforms import DEFAULT_TRANSFORMS


# Strip path components VK never uses (`..`, leading `/`) so a hostile
# source_folder can't escape the output dir.
_SLUG_SAFE = re.compile(r"[^A-Za-z0-9._+\-]")


def run(params: dict, progress: ProgressReporter) -> dict:
    source_input = Path(params["source"]).expanduser()
    peer_id = str(params["chat"]).strip()
    output_input = Path(params["output"]).expanduser()
    if not peer_id:
        raise ValueError("Chat is required")
    output_dir = output_input.resolve()

    # Remember inputs so the next save-chat run pre-fills them.
    try:
        cfg_set("save_chat.last_source", str(source_input.resolve()))
        cfg_set("save_chat.last_output", str(output_dir))
    except Exception:  # noqa: BLE001
        pass

    chat_meta = _lookup_chat(peer_id)
    if chat_meta is None:
        raise ValueError(f"No chat in DB with peer_id={peer_id!r}")

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


def _lookup_chat(peer_id: str) -> dict | None:
    with connection() as conn:
        row = conn.execute(
            "SELECT id, peer_id, title, type, source_folder, message_count"
            " FROM chats WHERE peer_id = ? LIMIT 1",
            (peer_id,),
        ).fetchone()
    return dict(row) if row else None


def _build_url_map(chat_id: int) -> dict[str, str]:
    """One pass over this chat's downloaded attachments. Returns
    {original_url: local_path_under_static_root}.
    """
    out: dict[str, str] = {}
    with connection() as conn:
        for row in conn.execute(
            "SELECT a.url, a.local_path FROM attachments a"
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
                out[url] = local
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


def _slugify(name: str) -> str:
    s = _SLUG_SAFE.sub("_", name.strip()) or "chat"
    # No leading dots, no path traversal, length cap so very long
    # community names don't blow past Windows MAX_PATH.
    return s.lstrip(".")[:80] or "chat"


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

    url_to_local = _build_url_map(chat_meta["id"])
    progress.log(
        f"save-chat: chat {chat_meta['peer_id']!r} → {len(pages)} page(s),"
        f" {len(url_to_local)} downloaded photo URL(s) to inline"
    )

    chat_slug = _slugify(chat_meta["source_folder"])
    chat_out = (output_dir / chat_slug).resolve()
    chat_out.mkdir(parents=True, exist_ok=True)

    static_root = app_dir() / "data" / "static"
    ctx = TransformContext(
        chat_id=chat_meta["id"],
        chat_source_folder=chat_meta["source_folder"],
        output_chat_dir=chat_out,
        static_root=static_root,
        url_to_local=url_to_local,
        log=progress.log,
    )

    total = len(pages)
    throttle = Throttle()
    blocks_total = 0
    blocks_changed = 0
    progress.report(0, total, "Rendering pages…")
    for i, page_rel in enumerate(pages, start=1):
        progress.check_cancelled()
        try:
            page_bytes = source.read_bytes(page_rel)
        except Exception as exc:  # noqa: BLE001
            logger.exception("save-chat: failed to read {}", page_rel)
            progress.log(f"save-chat: skipping {page_rel}: {exc}")
            continue
        html = page_bytes.decode("utf-8", errors="replace")
        new_html, n_blocks, n_changed = _transform_page(html, page_rel, ctx)
        blocks_total += n_blocks
        blocks_changed += n_changed
        page_name = Path(page_rel).name
        if not is_message_page_filename(page_name):
            page_name = f"messages{i}.html"
        (chat_out / page_name).write_text(new_html, encoding="utf-8")
        if throttle(i, total):
            progress.report(
                i, total,
                f"page {i}/{total} · blocks {blocks_total} · inlined {blocks_changed}",
            )

    progress.report(
        total, total,
        f"done · {blocks_total} blocks · {blocks_changed} inlined",
    )

    manifest = upsert_index(
        output_dir=output_dir,
        chat_slug=chat_slug,
        peer_id=str(chat_meta["peer_id"]),
        title=chat_meta["title"] or chat_slug,
        type_=chat_meta["type"] or "?",
        message_count=int(chat_meta["message_count"] or 0),
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
