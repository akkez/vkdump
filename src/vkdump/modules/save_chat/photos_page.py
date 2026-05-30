"""Per-chat photo gallery: one `photos.html` at the chat root that
shows every successfully-downloaded photo for the chat, ordered by
date. Sibling to (not replacement for) the inline-into-messages
transform — they share the same `assets/` tree via `copied_assets`.

Layout: photos grouped into year sections (latest year first, latest
photo first inside it). Each section renders a server-balanced masonry
of `_N_COLUMNS` fixed columns: the next-by-date photo always lands in
whichever column currently has the smallest sum of aspect ratios
(`height / width`), so all six columns grow at roughly the same speed
instead of one column finishing while the others idle. No JS — the page
works offline as plain HTML.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape as html_escape
from pathlib import Path

from ...core.db import connection
from ...core.i18n import plural
from ...parsers.vk.models import KIND_PHOTO
from .pipeline import TransformContext
from .transforms import materialise_photo_asset

_PHOTOS_PAGE = "photos.html"
_PER_SENDER_PREFIX = "photos-"
# Length cap on the sender-name portion of the per-sender filename.
# The trailing `-<vk_id>` segment is always appended so collisions on
# the same display name (and on truncations) are still disambiguated.
_SENDER_SLUG_MAX = 40
# Anything outside word chars (Unicode → cyrillic kept), `-` or `.`
# collapses to a single `x`, same convention as the chat-folder slug.
_SENDER_SAFE_RE = re.compile(r"[^\w\-.]+", re.UNICODE)

# Resolution column is stored as "<W>x<H>" (see enrich URL scrape).
# Tolerant of stray whitespace / case so backfills with slightly
# different shape still parse.
_RES_RE = re.compile(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$")

# Fixed bucket count. 6 reads well on a typical wide-screen monitor;
# narrower viewports get each column scaled down by `flex: 1 1 0`
# rather than re-balanced. Balancing is a one-pass O(n·k) greedy.
_N_COLUMNS = 6
# Fallback aspect when the photo has no resolution stored — assume
# square so unknown-size items don't all pile into one column.
_UNKNOWN_ASPECT = 1.0


@dataclass(frozen=True)
class _Photo:
    href: str           # POSIX path relative to photos.html
    sent_at: datetime
    width: int | None
    height: int | None
    description: str | None
    file_size: int | None
    sender_vk_id: int | None
    sender_display_name: str | None


@dataclass(frozen=True)
class _Sender:
    vk_id: int
    display_name: str
    count: int
    slug: str           # `<safe-name>-<vk_id>` — fed into the filename


def render(
    chat_meta: dict,
    ctx: TransformContext,
    first_page: str | None = None,
) -> tuple[str | None, int]:
    """Write `<chat>/photos.html` plus one `photos-<sender-slug>.html`
    per sender who contributed at least one downloaded photo.

    `first_page` is the basename of the first rendered messages file
    (e.g. `messages0.html`); when present, every gallery page's
    back-link points straight at it, otherwise it falls back to the
    parent export index. Returns `(main_filename, total_photo_count)`;
    `main_filename` is `None` when the chat has zero usable photos and
    every page was skipped.
    """
    rows = _fetch_photos(chat_meta["id"])
    if not rows:
        return None, 0
    photos: list[_Photo] = []
    for sent_at, local_path, resolution, description, file_size, sender_vk_id, sender_display_name in rows:
        src = ctx.static_root / local_path
        if not src.is_file():
            continue
        year = str(sent_at.year) if sent_at else "unknown"
        dst = materialise_photo_asset(src, year, ctx)
        href = os.path.relpath(dst, ctx.output_chat_dir).replace(os.sep, "/")
        w, h = _parse_resolution(resolution)
        photos.append(_Photo(
            href=href,
            sent_at=sent_at,
            width=w,
            height=h,
            description=description,
            file_size=file_size,
            sender_vk_id=sender_vk_id,
            sender_display_name=sender_display_name,
        ))
    if not photos:
        return None, 0

    senders = _build_senders(photos)
    out_dir = ctx.output_chat_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    main_html = _render_html(
        chat_meta, photos, senders,
        current=None, first_page=first_page,
    )
    (out_dir / _PHOTOS_PAGE).write_text(main_html, encoding="utf-8")

    for sender in senders:
        filtered = [p for p in photos if p.sender_vk_id == sender.vk_id]
        page_html = _render_html(
            chat_meta, filtered, senders,
            current=sender, first_page=first_page,
        )
        (out_dir / _sender_filename(sender)).write_text(page_html, encoding="utf-8")

    return _PHOTOS_PAGE, len(photos)


def _fetch_photos(chat_id: int) -> list[tuple]:
    """All ok'd photo attachments for a chat, newest first. One DB pass.

    Reverse-chronological so a casual visitor sees the freshest pic at
    the top of the page; year sections downstream stay grouped because
    DESC sort still keeps contiguous-by-year runs together. Sender
    columns come along so per-sender pages can filter without a second
    query.
    """
    with connection() as conn:
        cur = conn.execute(
            "SELECT m.sent_at, a.local_path, a.resolution, a.description,"
            "       a.file_size, m.sender_vk_id, m.sender_display_name"
            " FROM attachments a"
            " JOIN messages m ON m.id = a.message_id"
            " WHERE m.chat_id = ?"
            "   AND a.kind = ?"
            "   AND a.download_status = 'ok'"
            "   AND a.local_path IS NOT NULL"
            " ORDER BY m.sent_at DESC, a.position DESC",
            (chat_id, KIND_PHOTO),
        )
        return cur.fetchall()


def _sender_slug(display_name: str | None, vk_id: int) -> str:
    """Build `<safe-name>-<vk_id>`. The id suffix disambiguates two
    senders with the same display name (e.g. multiple "DELETED")
    while keeping the human-readable part of the filename intact.
    """
    raw = re.sub(r"\s+", "_", (display_name or "").strip())
    safe = _SENDER_SAFE_RE.sub("x", raw)
    safe = safe.lstrip(".-_x")[:_SENDER_SLUG_MAX].rstrip(".-_x")
    return f"{safe}-{vk_id}" if safe else str(vk_id)


def _sender_filename(sender: _Sender) -> str:
    return f"{_PER_SENDER_PREFIX}{sender.slug}.html"


def _build_senders(photos: list[_Photo]) -> list[_Sender]:
    """Aggregate the photo list into one entry per `sender_vk_id`,
    ordered by descending count (then name) so the nav lists the
    most-frequent sender first.
    """
    by_id: dict[int, dict] = {}
    for p in photos:
        if p.sender_vk_id is None:
            continue
        slot = by_id.setdefault(
            p.sender_vk_id,
            {"vk_id": p.sender_vk_id, "name": p.sender_display_name or str(p.sender_vk_id), "count": 0},
        )
        slot["count"] += 1
    senders = [
        _Sender(
            vk_id=v["vk_id"],
            display_name=v["name"],
            count=v["count"],
            slug=_sender_slug(v["name"], v["vk_id"]),
        )
        for v in by_id.values()
    ]
    senders.sort(key=lambda s: (-s.count, s.display_name.lower()))
    return senders


def _parse_resolution(raw: str | None) -> tuple[int | None, int | None]:
    if not raw:
        return None, None
    m = _RES_RE.match(raw)
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _group_by_year(photos: list[_Photo]) -> list[tuple[str, list[_Photo]]]:
    """Preserve the incoming ordering while bucketing into contiguous
    year groups. Caller passes photos already sorted by `sent_at DESC`,
    so the resulting groups read latest-year-first.
    """
    out: list[tuple[str, list[_Photo]]] = []
    current_year: str | None = None
    bucket: list[_Photo] = []
    for p in photos:
        year = str(p.sent_at.year) if p.sent_at else "unknown"
        if year != current_year:
            if bucket:
                out.append((current_year or "unknown", bucket))
            bucket = []
            current_year = year
        bucket.append(p)
    if bucket:
        out.append((current_year or "unknown", bucket))
    return out


def _distribute_into_columns(
    photos: list[_Photo], n_cols: int = _N_COLUMNS,
) -> list[list[_Photo]]:
    """Greedy shortest-column-first masonry packing. Input order is
    preserved within each column (so the page still reads newest-first
    top-down per column), but the next photo always lands in whichever
    column currently has the smallest summed aspect ratio. Result:
    six columns of roughly equal height, no laggard column.

    Weight = `height / width`. For images of unknown resolution we
    use `_UNKNOWN_ASPECT` so they don't accidentally collapse the
    heuristic (`weight = 0` would make every unknown-size pic land in
    the same column).
    """
    cols: list[list[_Photo]] = [[] for _ in range(n_cols)]
    weights = [0.0] * n_cols
    for p in photos:
        if p.width and p.height and p.width > 0:
            w = p.height / p.width
        else:
            w = _UNKNOWN_ASPECT
        idx = min(range(n_cols), key=lambda j: weights[j])
        cols[idx].append(p)
        weights[idx] += w
    return cols


def _fmt_caption(p: _Photo) -> str:
    """Compact one-line caption: date + resolution. Description is left
    out — it's almost always the generic "Фотография" and just adds
    noise across thousands of items.
    """
    parts: list[str] = []
    if p.sent_at:
        parts.append(p.sent_at.strftime("%Y-%m-%d %H:%M"))
    if p.width and p.height:
        parts.append(f"{p.width}×{p.height}")
    return " · ".join(parts)


def _render_html(
    chat_meta: dict,
    photos: list[_Photo],
    senders: list[_Sender],
    *,
    current: _Sender | None = None,
    first_page: str | None = None,
) -> str:
    chat_title = chat_meta.get("title") or str(chat_meta.get("peer_id") or "")
    page_subject = (
        f"{chat_title} — {current.display_name}'s photos"
        if current else f"{chat_title} — photos"
    )
    title = html_escape(page_subject)
    chat_title_html = html_escape(chat_title)
    sections = _group_by_year(photos)
    back_href, back_label = (
        (f"messages/{html_escape(first_page, quote=True)}", "← messages")
        if first_page else ("../index.html", "← all chats")
    )
    year_links = " ".join(
        f'<a href="#y{html_escape(y, quote=True)}">{html_escape(y)}'
        f' <span class="count">({len(items)})</span></a>'
        for y, items in sections
    )
    sender_links_parts: list[str] = []
    # "Everyone" anchor: always points back to photos.html, marked
    # active when the current view is the unfiltered main page.
    all_active = ' class="active"' if current is None else ""
    sender_links_parts.append(
        f'<a{all_active} href="{_PHOTOS_PAGE}">everyone'
        f' <span class="count">({len(photos) if current is None else sum(s.count for s in senders)})</span></a>'
    )
    for s in senders:
        active = ' class="active"' if current and current.vk_id == s.vk_id else ""
        sender_links_parts.append(
            f'<a{active} href="{html_escape(_sender_filename(s), quote=True)}">'
            f'{html_escape(s.display_name)}'
            f' <span class="count">({s.count})</span></a>'
        )
    sender_links = " ".join(sender_links_parts)
    body_parts: list[str] = []
    for year, items in sections:
        cols = _distribute_into_columns(items)
        body_parts.append(
            f'<section class="year" id="y{html_escape(year, quote=True)}">'
            f'<h2>{html_escape(year)} '
            f'<span class="count">· {html_escape(plural(len(items), "photo"))}</span></h2>'
            '<div class="gallery">'
        )
        for col in cols:
            body_parts.append('<div class="col">')
            for p in col:
                href = html_escape(p.href, quote=True)
                caption = html_escape(_fmt_caption(p))
                # Pre-declaring intrinsic size lets the browser reserve
                # the aspect-ratio slot before the JPEG bytes arrive, so
                # the column doesn't reflow as images stream in. Drops
                # to a plain auto-height <img> when resolution is
                # unknown.
                size_attrs = (
                    f' width="{p.width}" height="{p.height}"'
                    if p.width and p.height else ""
                )
                body_parts.append(
                    '<figure>'
                    f'<a href="{href}">'
                    f'<img loading="lazy" decoding="async"'
                    f' src="{href}" alt=""{size_attrs}>'
                    '</a>'
                    f'<figcaption>{caption}</figcaption>'
                    '</figure>'
                )
            body_parts.append('</div>')
        body_parts.append('</div></section>')

    total = len(photos)
    heading = (
        f"{chat_title_html} <span class=\"sub\">— "
        f"{html_escape(current.display_name)}</span>"
        if current else chat_title_html
    )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f'<title>{title}</title>'
        f'<style>{_CSS}</style>'
        '</head><body>'
        f'<header class="page-head">'
        f'<a class="back" href="{back_href}">{back_label}</a>'
        f'<h1>{heading}</h1>'
        f'<p class="summary">{html_escape(plural(total, "photo"))}'
        f' across {html_escape(plural(len(sections), "year"))}</p>'
        f'<nav class="sender-nav">{sender_links}</nav>'
        f'<nav class="year-nav">{year_links}</nav>'
        '</header>'
        f'<main>{"".join(body_parts)}</main>'
        '</body></html>'
    )


_CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,sans-serif;margin:0;background:#fafafa;color:#222}
.page-head{padding:16px 24px;background:#fff;border-bottom:1px solid #e5e5e5;position:sticky;top:0;z-index:10}
.page-head h1{margin:0 0 4px;font-size:18px}
.page-head h1 .sub{font-weight:400;color:#777;font-size:14px}
.page-head .summary{margin:0 0 8px;color:#777;font-size:12px}
.page-head .back{font-size:12px;color:#0a66c2;text-decoration:none;display:inline-block;margin-bottom:6px}
.page-head .back:hover{text-decoration:underline}
.sender-nav,.year-nav{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:13px}
.sender-nav{margin-bottom:6px;padding-bottom:6px;border-bottom:1px dashed #eee}
.sender-nav a,.year-nav a{color:#0a66c2;text-decoration:none}
.sender-nav a:hover,.year-nav a:hover{text-decoration:underline}
.sender-nav a.active,.year-nav a.active{color:#222;font-weight:600;text-decoration:none}
.sender-nav .count,.year-nav .count{color:#999;font-size:11px}
main{padding:16px 24px 64px}
section.year{margin-bottom:24px;scroll-margin-top:120px}
section.year h2{margin:8px 0 12px;font-size:16px;color:#444;font-weight:600}
section.year h2 .count{color:#999;font-weight:400;font-size:12px}
/* Six fixed flex columns side-by-side. Distribution into columns
   happens server-side (`_distribute_into_columns`) so adjacent photos
   from different columns don't drift apart as CSS-columns balancing
   would do. Each `.col` keeps its own vertical stack with `gap`. */
.gallery{display:flex;gap:8px;align-items:flex-start}
.gallery .col{flex:1 1 0;min-width:0;display:flex;flex-direction:column;gap:8px}
.gallery figure{margin:0;background:#fff;border:1px solid #eee;border-radius:4px;overflow:hidden;text-align:center}
.gallery a{display:block;line-height:0}
/* Intrinsic-size cap: tiny pics keep their native pixels instead of
   being stretched to fill the column. max-width clamps wide ones to
   the column; the <img width/height> attrs feed the aspect ratio so
   height is auto-computed and reserved before bytes arrive. */
.gallery img{max-width:100%;height:auto;display:inline-block;background:#f0f0f0;vertical-align:middle}
.gallery figcaption{font-size:11px;color:#888;padding:3px 6px;line-height:1.3}
"""
