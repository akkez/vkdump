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


def render(
    chat_meta: dict,
    ctx: TransformContext,
    first_page: str | None = None,
) -> tuple[str | None, int]:
    """Write `<chat>/photos.html` if the chat has any downloaded photos.

    `first_page` is the basename of the first rendered messages file
    (e.g. `messages0.html`); when present, the gallery's back-link
    points straight at it, otherwise it falls back to the parent
    export index. Returns `(filename, photo_count)`; `filename` is
    `None` when the chat has zero usable photos and the page was
    skipped, so the caller can leave the manifest entry empty.
    """
    rows = _fetch_photos(chat_meta["id"])
    if not rows:
        return None, 0
    photos: list[_Photo] = []
    for sent_at, local_path, resolution, description, file_size in rows:
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
        ))
    if not photos:
        return None, 0
    html = _render_html(chat_meta, photos, first_page=first_page)
    out_path = ctx.output_chat_dir / _PHOTOS_PAGE
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return _PHOTOS_PAGE, len(photos)


def _fetch_photos(chat_id: int) -> list[tuple]:
    """All ok'd photo attachments for a chat, newest first. One DB pass.

    Reverse-chronological so a casual visitor sees the freshest pic at
    the top of the page; year sections downstream stay grouped because
    DESC sort still keeps contiguous-by-year runs together.
    """
    with connection() as conn:
        cur = conn.execute(
            "SELECT m.sent_at, a.local_path, a.resolution, a.description,"
            "       a.file_size"
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
    *,
    first_page: str | None = None,
) -> str:
    title = html_escape(chat_meta.get("title") or str(chat_meta.get("peer_id") or ""))
    sections = _group_by_year(photos)
    back_href, back_label = (
        (f"messages/{html_escape(first_page, quote=True)}", "← messages")
        if first_page else ("../index.html", "← all chats")
    )
    nav_links = " ".join(
        f'<a href="#y{html_escape(y, quote=True)}">{html_escape(y)}'
        f' <span class="count">({len(items)})</span></a>'
        for y, items in sections
    )
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
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        f'<title>{title} — photos</title>'
        f'<style>{_CSS}</style>'
        '</head><body>'
        f'<header class="page-head">'
        f'<a class="back" href="{back_href}">{back_label}</a>'
        f'<h1>{title}</h1>'
        f'<p class="summary">{html_escape(plural(total, "photo"))}'
        f' across {html_escape(plural(len(sections), "year"))}</p>'
        f'<nav class="year-nav">{nav_links}</nav>'
        '</header>'
        f'<main>{"".join(body_parts)}</main>'
        '</body></html>'
    )


_CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,sans-serif;margin:0;background:#fafafa;color:#222}
.page-head{padding:16px 24px;background:#fff;border-bottom:1px solid #e5e5e5;position:sticky;top:0;z-index:10}
.page-head h1{margin:0 0 4px;font-size:18px}
.page-head .summary{margin:0 0 8px;color:#777;font-size:12px}
.page-head .back{font-size:12px;color:#0a66c2;text-decoration:none;display:inline-block;margin-bottom:6px}
.page-head .back:hover{text-decoration:underline}
.year-nav{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:13px}
.year-nav a{color:#0a66c2;text-decoration:none}
.year-nav a:hover{text-decoration:underline}
.year-nav .count{color:#999;font-size:11px}
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
