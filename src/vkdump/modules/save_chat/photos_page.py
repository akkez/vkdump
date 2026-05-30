"""Per-chat photo gallery: one `photos.html` at the chat root that
shows every successfully-downloaded photo for the chat, ordered by
date. Sibling to (not replacement for) the inline-into-messages
transform — they share the same `assets/` tree via `copied_assets`.

Layout: photos grouped into year sections; within a year a CSS-columns
masonry keeps native aspect ratios without horizontal alignment forcing
neighbours to crop or stretch. No JS — the page works offline as plain
HTML.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from html import escape as html_escape
from pathlib import Path

from ...core.db import connection
from ...parsers.vk.models import KIND_PHOTO
from .pipeline import TransformContext
from .transforms import materialise_photo_asset

_PHOTOS_PAGE = "photos.html"

# Resolution column is stored as "<W>x<H>" (see enrich URL scrape).
# Tolerant of stray whitespace / case so backfills with slightly
# different shape still parse.
_RES_RE = re.compile(r"^\s*(\d+)\s*[xX×]\s*(\d+)\s*$")


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
    """All ok'd photo attachments for a chat, ordered by message time
    then by attachment position within the message. One DB pass.
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
            " ORDER BY m.sent_at ASC, a.position ASC",
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
    """Preserve the incoming chronological order while bucketing into
    contiguous year groups. Plain dict preservation works since the
    source query is ORDER BY sent_at.
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
        body_parts.append(
            f'<section class="year" id="y{html_escape(year, quote=True)}">'
            f'<h2>{html_escape(year)} '
            f'<span class="count">· {len(items)} photo(s)</span></h2>'
            '<div class="gallery">'
        )
        for p in items:
            href = html_escape(p.href, quote=True)
            caption = html_escape(_fmt_caption(p))
            # Pre-declaring intrinsic size lets the browser reserve the
            # right aspect-ratio slot before the JPEG bytes arrive, so
            # the column doesn't reflow as images stream in. Drops to a
            # plain auto-height <img> when resolution is unknown.
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
        f'<p class="summary">{total} photo(s) across {len(sections)} year(s)</p>'
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
.gallery{column-count:6;column-gap:8px}
@media (max-width:1700px){.gallery{column-count:5}}
@media (max-width:1400px){.gallery{column-count:4}}
@media (max-width:1100px){.gallery{column-count:3}}
@media (max-width:760px){.gallery{column-count:2}}
@media (max-width:480px){.gallery{column-count:1}}
.gallery figure{break-inside:avoid;margin:0 0 8px;background:#fff;border:1px solid #eee;border-radius:4px;overflow:hidden;text-align:center}
.gallery a{display:block;line-height:0}
/* Intrinsic-size cap: tiny pics keep their native pixels instead of
   being stretched to fill the column. max-width clamps wide ones to
   the column; the <img width/height> attrs feed the aspect ratio so
   height is auto-computed and reserved before bytes arrive. */
.gallery img{max-width:100%;height:auto;display:inline-block;background:#f0f0f0;vertical-align:middle}
.gallery figcaption{font-size:11px;color:#888;padding:3px 6px;line-height:1.3}
"""
