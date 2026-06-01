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
from .transforms import _fmt_size, materialise_photo_asset

# Pages live under `<chat>/gallery/` so the chat root stays at two
# entries (`messages/` + `gallery/`) regardless of how many senders the
# chat has — otherwise a 50-person group chat litters the folder with
# 50+ `photos-*.html` files. The link surfaced from the top-level index
# is `gallery/` (folder URL — browsers serve `index.html` from inside).
_GALLERY_DIR = "gallery"
_MAIN_FILE = "index.html"
# Value persisted in `_export.json::photos_page` and used by the
# top-level index to link to a chat's gallery. Explicit path to
# `gallery/index.html` because the export is browsed off the local
# filesystem via `file://` — folder URLs there show a directory
# listing instead of auto-loading `index.html`.
_GALLERY_LINK = f"{_GALLERY_DIR}/{_MAIN_FILE}"
# Sender chips per page in the top-of-page nav. 30 fits comfortably on
# a single row for typical screens; chats with hundreds of senders no
# longer balloon the header into a multi-page-tall block. Pages
# beyond the first land at `everyone-2.html`, `everyone-3.html`, …
_NAV_PAGE_SIZE = 30
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
    href: str               # POSIX path to the local asset (img src)
    message_anchor: str     # POSIX path to messages/<page>#m<vk_message_id>
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
    """Write `<chat>/gallery/index.html` plus one `<slug>.html` per
    sender who contributed at least one downloaded photo.

    Lives under `gallery/` so the chat root keeps its two-entry shape
    (`messages/`, `gallery/`) regardless of how many senders the chat
    has. `first_page` is the basename of the first rendered messages
    file (e.g. `messages0.html`); when present, every gallery page's
    back-link points straight at it, otherwise it falls back to the
    parent export index. Returns `(folder_link, total_photo_count)`;
    `folder_link` is `None` when the chat has zero usable photos and
    every page was skipped.
    """
    rows = _fetch_photos(chat_meta["id"])
    if not rows:
        return None, 0
    gallery_dir = ctx.output_chat_dir / _GALLERY_DIR
    photos: list[_Photo] = []
    for (
        sent_at, local_path, resolution, description, file_size,
        sender_vk_id, sender_display_name, vk_message_id, source_file,
    ) in rows:
        src = ctx.static_root / local_path
        if not src.is_file():
            continue
        year = str(sent_at.year) if sent_at else "unknown"
        dst = materialise_photo_asset(src, year, ctx)
        # Hrefs are computed relative to `gallery/` since every emitted
        # HTML file lives there; assets are one level up, so the result
        # is `../assets/<year>/photos/...`.
        href = os.path.relpath(dst, gallery_dir).replace(os.sep, "/")
        # Source page basename + #m<vk_message_id> — offline-first
        # relative link; clicking the photo jumps into the rendered
        # message context instead of opening the bare asset.
        message_anchor = (
            f"../messages/{source_file}#m{vk_message_id}"
            if source_file and vk_message_id is not None
            else href
        )
        w, h = _parse_resolution(resolution)
        photos.append(_Photo(
            href=href,
            message_anchor=message_anchor,
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
    gallery_dir.mkdir(parents=True, exist_ok=True)

    # Single everyone-view file. The sender-nav at the top shows
    # window 0 plus a 'next' arrow pointing at the first sender of
    # window 1 (when more windows exist); 'prev' is disabled here.
    main_html = _render_html(
        chat_meta, photos, senders,
        current=None, first_page=first_page, nav_page_idx=0,
    )
    (gallery_dir / _MAIN_FILE).write_text(main_html, encoding="utf-8")

    # One file per sender. Each picks the window containing its own
    # chip; the pager arrows jump to the first sender of the adjacent
    # window (or back to index.html for the leftmost case), never
    # re-render the full everyone view.
    for sender in senders:
        filtered = [p for p in photos if p.sender_vk_id == sender.vk_id]
        page_html = _render_html(
            chat_meta, filtered, senders,
            current=sender, first_page=first_page,
            nav_page_idx=_nav_page_for_sender(senders, sender),
        )
        (gallery_dir / _sender_filename(sender)).write_text(page_html, encoding="utf-8")

    return _GALLERY_LINK, len(photos)


def _fetch_photos(chat_id: int) -> list[tuple]:
    """All ok'd photo attachments for a chat, newest first. One DB pass.

    Reverse-chronological so a casual visitor sees the freshest pic at
    the top of the page; year sections downstream stay grouped because
    DESC sort still keeps contiguous-by-year runs together. Sender
    columns come along so per-sender pages can filter without a second
    query.

    Sender display name is read from ``users.display_name`` (left-join
    on ``users.vk_id`` against the message's ``sender_vk_id``) so the
    deleted-labels backfill — ``"DELETED (Real Name)"`` — surfaces in
    the gallery. Falls back to the per-message ``sender_display_name``
    when there is no matching user row (anonymous / unresolved sender).
    """
    with connection() as conn:
        cur = conn.execute(
            "SELECT m.sent_at, a.local_path, a.resolution, a.description,"
            "       a.file_size, m.sender_vk_id,"
            "       COALESCE(u.display_name, m.sender_display_name)"
            "         AS sender_display_name,"
            "       m.vk_message_id, m.source_file"
            " FROM attachments a"
            " JOIN messages m ON m.id = a.message_id"
            " LEFT JOIN users u ON u.vk_id = m.sender_vk_id"
            "                  AND u.provider = m.provider"
            " WHERE m.chat_id = ?"
            "   AND a.kind = ?"
            "   AND a.download_status = 'ok'"
            "   AND a.local_path IS NOT NULL"
            " ORDER BY m.sent_at DESC, a.position DESC",
            (chat_id, KIND_PHOTO),
        )
        return cur.fetchall()


def _nav_page_count(n_senders: int) -> int:
    """Number of sender-nav pages needed to cover ``n_senders``."""
    if n_senders <= 0:
        return 1
    return (n_senders + _NAV_PAGE_SIZE - 1) // _NAV_PAGE_SIZE


def _nav_page_for_sender(senders: list["_Sender"], sender: "_Sender") -> int:
    """Window index containing ``sender`` in the ordered ``senders``
    list. Falls back to 0 if not found (shouldn't happen in practice;
    defensive)."""
    for idx, s in enumerate(senders):
        if s.vk_id == sender.vk_id:
            return idx // _NAV_PAGE_SIZE
    return 0


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
    return f"{sender.slug}.html"


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


def _fmt_tooltip(p: _Photo, *, include_author: bool) -> str:
    """Hover-only metadata strip: author (when this page mixes senders,
    omitted on per-sender pages where it's redundant) · date · WxH ·
    size. Nothing is rendered visibly under the thumbnails — the user
    wanted the metadata behind a hover instead of cluttering the grid.
    """
    parts: list[str] = []
    if include_author and p.sender_display_name:
        parts.append(p.sender_display_name)
    if p.sent_at:
        parts.append(p.sent_at.strftime("%Y-%m-%d %H:%M"))
    if p.width and p.height:
        parts.append(f"{p.width}×{p.height}")
    sz = _fmt_size(p.file_size)
    if sz:
        parts.append(sz)
    return " · ".join(parts)


def _render_sender_nav(
    senders: list[_Sender],
    current: _Sender | None,
    total_photos: int,
    nav_page_idx: int,
) -> str:
    """Single-row nav strip:

    ``[everyone (N)] [<<< prev (X)] <30 chips> [next (Y) >>>]``

    - ``everyone`` always links to ``index.html``. Active class when
      ``current is None`` (i.e. rendering the everyone view itself).
    - Prev arrow on a sender page in window K goes to ``index.html``
      when K==0, else to the first sender of window K-1 (never
      reloads the full everyone view in the middle of pagination).
      Disabled when we're on the everyone view (K==0 there too) —
      there is nothing to the left.
    - Next arrow goes to the first sender of window K+1, or
      disabled when we're already on the last window.
    - X / Y in parentheses are sender counts on the windows to the
      left / right of the current one (a preview of "how many more
      names there are if I click").

    The everyone view (``current is None``) lives in window 0 and
    shows the same chips as the leftmost sender pages.
    """
    everyone_active = ' class="active"' if current is None else ""
    everyone_count = (
        total_photos if current is None
        else sum(s.count for s in senders)
    )
    everyone_chip = (
        f'<a{everyone_active} class="everyone-chip" href="{_MAIN_FILE}">everyone'
        f' <span class="count">({everyone_count})</span></a>'
    )

    total_pages = _nav_page_count(len(senders))
    start = nav_page_idx * _NAV_PAGE_SIZE
    window = senders[start:start + _NAV_PAGE_SIZE]
    chips: list[str] = []
    for s in window:
        active = ' class="active"' if current and current.vk_id == s.vk_id else ""
        chips.append(
            f'<a{active} href="{html_escape(_sender_filename(s), quote=True)}">'
            f'{html_escape(s.display_name)}'
            f' <span class="count">({s.count})</span></a>'
        )
    chips_html = " ".join(chips)

    if total_pages <= 1:
        # Single window — no pager arrows; emit everyone + chips inline.
        return (
            f'<nav class="sender-nav">{everyone_chip} {chips_html}</nav>'
        )

    senders_left = start  # all senders before the current window
    senders_right = len(senders) - (start + len(window))  # all senders past it

    # Arrows are omitted entirely when there is nothing to navigate
    # to — no disabled-state placeholder. Visually that means the
    # leftmost window has only the right arrow, the rightmost has
    # only the left arrow.
    prev_html = ""
    if nav_page_idx > 0:
        prev_target = (
            _MAIN_FILE if nav_page_idx == 1
            else _sender_filename(senders[(nav_page_idx - 1) * _NAV_PAGE_SIZE])
        )
        prev_html = (
            f'<a class="pager-arrow" href="{html_escape(prev_target, quote=True)}">'
            f'&laquo;&laquo;&laquo; prev ({senders_left})</a>'
        )

    next_html = ""
    if nav_page_idx + 1 < total_pages:
        next_target = _sender_filename(senders[(nav_page_idx + 1) * _NAV_PAGE_SIZE])
        next_html = (
            f'<a class="pager-arrow" href="{html_escape(next_target, quote=True)}">'
            f'next ({senders_right}) &raquo;&raquo;&raquo;</a>'
        )

    parts = [everyone_chip]
    if prev_html:
        parts.append(prev_html)
    parts.append(chips_html)
    if next_html:
        parts.append(next_html)
    return f'<nav class="sender-nav">{" ".join(parts)}</nav>'


def _render_html(
    chat_meta: dict,
    photos: list[_Photo],
    senders: list[_Sender],
    *,
    current: _Sender | None = None,
    first_page: str | None = None,
    nav_page_idx: int = 0,
) -> str:
    chat_title = chat_meta.get("title") or str(chat_meta.get("peer_id") or "")
    page_subject = (
        f"{chat_title} — {current.display_name}'s photos"
        if current else f"{chat_title} — photos"
    )
    title = html_escape(page_subject)
    chat_title_html = html_escape(chat_title)
    sections = _group_by_year(photos)
    # Pages live one level deep (`<chat>/gallery/`), so back-links climb
    # an extra `../` to reach the chat root (or two for the export
    # index at the output root).
    back_href, back_label = (
        (f"../messages/{html_escape(first_page, quote=True)}", "← messages")
        if first_page else ("../../index.html", "← all chats")
    )
    year_links = " ".join(
        f'<a href="#y{html_escape(y, quote=True)}">{html_escape(y)}'
        f' <span class="count">({len(items)})</span></a>'
        for y, items in sections
    )
    sender_nav_html = _render_sender_nav(
        senders, current,
        total_photos=len(photos) if current is None else sum(s.count for s in senders),
        nav_page_idx=nav_page_idx,
    )
    # `current is None` ⇒ main gallery page mixes senders, so the
    # tooltip needs the author. Per-sender pages omit it (every photo
    # on that page is by that one person).
    include_author = current is None
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
                anchor = html_escape(p.message_anchor, quote=True)
                tooltip = html_escape(
                    _fmt_tooltip(p, include_author=include_author), quote=True
                )
                # Pre-declaring intrinsic size lets the browser reserve
                # the aspect-ratio slot before the JPEG bytes arrive, so
                # the column doesn't reflow as images stream in. Drops
                # to a plain auto-height <img> when resolution is
                # unknown.
                size_attrs = (
                    f' width="{p.width}" height="{p.height}"'
                    if p.width and p.height else ""
                )
                # `<a>` jumps to the message anchor; `<img src>` stays
                # on the local asset so the thumbnail renders. The `\n`
                # after `<img>` puts each thumbnail on its own line in
                # the source — purely a readability win for anyone
                # opening the gallery HTML in a text editor; whitespace
                # between `<img>` and `</a>` has no rendered effect.
                body_parts.append(
                    '<figure>'
                    f'<a href="{anchor}" title="{tooltip}">'
                    f'<img loading="lazy" decoding="async"'
                    f' src="{href}" alt="" title="{tooltip}"{size_attrs}>\n'
                    '</a>'
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
    year_ints = sorted({
        p.sent_at.year for p in photos if p.sent_at is not None
    })
    if not year_ints:
        span_label = "?"
    elif year_ints[0] == year_ints[-1]:
        span_label = str(year_ints[0])
    else:
        span_label = f"{year_ints[0]}–{year_ints[-1]}"
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
        f' · {html_escape(span_label)}</p>'
        f'{sender_nav_html}'
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
.sender-nav,.year-nav{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:13px;
  align-items:center}
.sender-nav{margin-bottom:6px;padding-bottom:6px;border-bottom:1px dashed #eee}
.sender-nav .everyone-chip{font-weight:600}
.pager-arrow{color:#666;font-size:12px;text-decoration:none;
  padding:0 4px;border-radius:3px}
.pager-arrow:hover{background:#eef4fb;color:#0a66c2;text-decoration:none}
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
"""
