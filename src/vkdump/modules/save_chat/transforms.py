"""Concrete transforms used by save-chat. New media types (video poster,
audio player, …) plug in here without touching the orchestrator.
"""
from __future__ import annotations

import os
import re
import shutil
from html import escape as html_escape
from pathlib import Path
from urllib.parse import urlparse

from ...parsers.vk.models import KIND_PHOTO, ParsedAttachment, ParsedMessage
from .pipeline import AttMeta, Transform, TransformContext


_MESSAGE_OPEN_RE = re.compile(
    r'<div\s+class="message"\s+data-id="(?P<id>\d+)">'
)

# Sender link inside the message header. Same shape as the parser's
# `_HEADER_LINK_RE` but tighter, since here we only care about
# `id<num>` and `public<num>`/`club<num>`/`event<num>` (which we
# represent with a negative peer id in DB).
_SENDER_LINK_RE = re.compile(
    r'(<div class="message__header">[^<]*'
    r'<a href="https://vk\.com/)'
    r'(?P<prefix>id|public|club|event)'
    r'(?P<num>\d+)'
    r'(?P<after>"[^>]*>)'
    r'(?P<name>[^<]*)'
    r'(</a>)',
    re.DOTALL,
)

# Matches an `attachment__description` div ending right before the
# search-region boundary — used to detect a description that sits
# immediately above a given `attachment__link` so the inliner can
# swallow it (the inlined `<img>` is the label now).
_DESC_ABOVE_LINK_RE = re.compile(
    r'<div class="attachment__description">[^<]*</div>\s*\Z'
)


def _fmt_size(n: int | None) -> str | None:
    """Compact bytes label for the img alt — KB integer for small files,
    MB with one decimal for big ones. Returns None when size is unknown.
    """
    if not n or n <= 0:
        return None
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{round(n / 1024)}KB"
    return f"{n / 1024 / 1024:.1f}MB"


def _build_alt(description: str | None, meta: AttMeta) -> str:
    """Compose `alt` text VK-style — description first ("Фотография"),
    then resolution and size when present. Each part is skipped when
    its source value is missing, so output stays clean for partial
    metadata (e.g. older downloads that ran before resolution backfill).
    """
    parts: list[str] = [(description or "photo").strip() or "photo"]
    extras: list[str] = []
    if meta.resolution:
        extras.append(meta.resolution)
    size = _fmt_size(meta.file_size)
    if size:
        extras.append(size)
    if extras:
        parts.append(", ".join(extras))
    return " ".join(parts)


def _link_re(url: str) -> re.Pattern[str]:
    # The VK dump always writes `<a class="attachment__link" href="...">`
    # with the URL verbatim. Match that exact link and capture an open
    # boundary before it so we can inject ahead of the original anchor.
    return re.compile(
        r'<a\s+class=[\'"]attachment__link[\'"]\s+href=[\'"]'
        + re.escape(url)
        + r'[\'"]'
    )


def _full_link_re(url: str) -> re.Pattern[str]:
    """Match the full `<a class="attachment__link" href="URL">TEXT</a>`
    block for a known URL. Used by the photo inliner to absorb the
    original anchor when it can be replaced with a compact hostname
    link inside the caption strip.
    """
    return re.compile(
        r'<a\s+class=[\'"]attachment__link[\'"]\s+href=[\'"]'
        + re.escape(url)
        + r'[\'"]\s*>(?P<text>[^<]*)</a>'
    )


def _link_asset(src: Path, dst: Path) -> None:
    """Hardlink `src` → `dst` when possible (no extra disk usage), copy
    otherwise (cross-filesystem, or hardlink unsupported). Idempotent —
    skips when `dst` already exists.
    """
    if dst.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def materialise_photo_asset(src: Path, year: str, ctx: TransformContext) -> Path:
    """Hardlink/copy a downloaded photo into the chat's asset tree.

    Shared by the inline-into-messages transform and the separate
    photos-gallery page so both flows place files at the same path
    (`assets/<year>/photos/<sha[:2]>/<basename>`) and the dedup map in
    `ctx.copied_assets` covers both callers. Returns the absolute on-disk
    path; callers compute their own relpath because the inline pages
    live one level deeper (`messages/`) than `photos.html`.
    """
    already = ctx.copied_assets.get(src)
    if already is not None:
        return already
    bucket = src.stem[:2] if len(src.stem) >= 2 else "_"
    rel = Path("assets") / year / "photos" / bucket / src.name
    dst = ctx.output_chat_dir / rel
    _link_asset(src, dst)
    ctx.copied_assets[src] = dst
    return dst


class InlinePhotosTransform:
    """For every photo attachment with a successful local download,
    insert a `<img>` tag right before the original `<a class="attachment__link">`
    inside the message block. The link itself stays untouched — the
    user's "ни один байт не пострадал" promise.

    Assets land at `<chat>/assets/<year>/photos/<sha[:2]>/<basename>` so
    a single year's photo bucket stays browsable without blowing past
    filesystem dirent limits on giant chats.
    """

    name = "inline_photos"

    def apply(self, msg: ParsedMessage, ctx: TransformContext) -> ParsedMessage:
        if not msg.attachments:
            return msg
        new_html = msg.raw_html
        year = str(msg.sent_at.year) if msg.sent_at else "unknown"
        for att in msg.attachments:
            if att.kind != KIND_PHOTO or not att.url:
                continue
            # Match enrich's queue-time exclusion: on-site vk.com URLs
            # need auth/redirects and aren't downloaded, so they don't
            # count as candidates here either. Anything else with a
            # URL is fair game and bumps the denominator — done in the
            # render loop so the end-of-run summary is free instead of
            # paying a multi-second SELECT COUNT(*) at the finish.
            if att.url.startswith("https://vk.com/"):
                continue
            ctx.candidates_by_kind[KIND_PHOTO] = (
                ctx.candidates_by_kind.get(KIND_PHOTO, 0) + 1
            )
            meta = ctx.url_to_meta.get(att.url)
            if meta is None:
                continue
            src = ctx.static_root / meta.local_path
            if not src.is_file():
                # DB lied — row is 'ok' but the file isn't on disk.
                # Surface this in the summary so the user sees how
                # much of the gap is "never downloaded" vs "vanished".
                ctx.missing_on_disk_by_kind[KIND_PHOTO] = (
                    ctx.missing_on_disk_by_kind.get(KIND_PHOTO, 0) + 1
                )
                continue
            asset_rel = self._materialise_asset(src, year, ctx)
            before = new_html
            new_html = self._inject(new_html, att, asset_rel, meta)
            if new_html is not before:
                ctx.injected_by_kind[KIND_PHOTO] = (
                    ctx.injected_by_kind.get(KIND_PHOTO, 0) + 1
                )
        if new_html is not msg.raw_html:
            msg.raw_html = new_html
        return msg

    @staticmethod
    def _materialise_asset(src: Path, year: str, ctx: TransformContext) -> str:
        """Place `src` under `assets/` and return a POSIX href relative
        to `pages_dir` (the `messages/` subfolder), so injected tags
        keep the same `../assets/<year>/…` shape regardless of the
        chat's slug.
        """
        dst = materialise_photo_asset(src, year, ctx)
        return os.path.relpath(dst, ctx.pages_dir).replace(os.sep, "/")

    @staticmethod
    def _desc_extent_before_link(html: str, link_start: int) -> int:
        """Position where the `attachment__description` sitting immediately
        above this link starts. Returns ``link_start`` unchanged when no
        adjacent description is found. Bounded backwards scan (≤256 chars)
        keeps this cheap on long pages."""
        window_start = max(0, link_start - 256)
        m = _DESC_ABOVE_LINK_RE.search(html[window_start:link_start])
        if m is None:
            return link_start
        return window_start + m.start()

    @staticmethod
    def _inject(html: str, att: ParsedAttachment, asset_href: str, meta: AttMeta) -> str:
        pat = _link_re(att.url or "")
        m = pat.search(html)
        if m is None:
            return html
        splice_start = InlinePhotosTransform._desc_extent_before_link(
            html, m.start()
        )
        # Try to absorb the entire original `<a>…</a>` so its visible
        # URL — which is just a long verbatim copy of `href` — gets
        # compressed into a hostname chip inside the caption strip.
        # If the regex fails (unexpected text shape), leave the original
        # anchor in place and skip the chip: keeps the page consistent
        # at "either both transformations or neither".
        full_re = _full_link_re(att.url or "")
        m_full = full_re.match(html, m.start())

        label = _build_alt(att.description, meta)
        alt = html_escape(label, quote=True)
        caption = html_escape(label, quote=False)
        href = html_escape(asset_href, quote=True)
        # Block-level wrapper with inline styles so we don't depend on
        # VK's own CSS: VK's description/link siblings are inline, so
        # without `display:block` the injected image flows mid-sentence.
        # Margins separate it from surrounding text; max-width contains
        # wide images within the column.
        caption_inner = caption
        end = m.start()
        if m_full is not None:
            host = urlparse(att.url or "").netloc or (att.url or "")
            url_attr = html_escape(att.url or "", quote=True)
            host_text = html_escape(host, quote=False)
            caption_inner = (
                f'{caption} · '
                f'<a href="{url_attr}"'
                ' style="color:#888;text-decoration:underline">'
                f'{host_text}</a>'
            )
            end = m_full.end()
        tag = (
            '<div class="vkdump-inline-photo"'
            ' style="display:block;margin:6px 0">'
            f'<a href="{href}" title="{alt}" style="display:inline-block">'
            f'<img loading="lazy" src="{href}" alt="{alt}" title="{alt}"'
            ' style="display:block;max-width:100%;height:auto">'
            '</a>'
            f'<div class="vkdump-caption"'
            ' style="font-size:11px;color:#888;margin-top:2px">'
            f'{caption_inner}</div>'
            '</div>'
        )
        return html[:splice_start] + tag + html[end:]


class SenderNameFromDBTransform:
    """Swap the sender display-name text inside ``<div class="message__header">``
    with the current value from ``users.display_name``.

    The original ``<a href="https://vk.com/idN">…</a>`` link is
    preserved verbatim — only the visible link text changes. This is
    what surfaces the bracketed ``"DELETED (Real Name)"`` form for
    re-identified deleted users (without it the rendered HTML would
    still show whatever the VK exporter wrote at dump time — typically
    just "DELETED").

    Community senders (``public``/``club``/``event``) use a negative
    peer id in our DB convention, so the lookup keys flip sign.
    """

    name = "sender_name_from_db"

    def apply(self, msg: ParsedMessage, ctx: TransformContext) -> ParsedMessage:
        if not ctx.user_names:
            return msg

        def _sub(m: re.Match[str]) -> str:
            num = int(m.group("num"))
            vk_id = num if m.group("prefix") == "id" else -num
            name = ctx.user_names.get(vk_id)
            if not name or name == m.group("name"):
                return m.group(0)
            return (
                m.group(1) + m.group("prefix") + m.group("num")
                + m.group("after") + html_escape(name, quote=False)
                + m.group(6)
            )

        new_html, n = _SENDER_LINK_RE.subn(_sub, msg.raw_html, count=1)
        if n:
            msg.raw_html = new_html
        return msg


class MessageAnchorTransform:
    """Add ``id="m<vk_message_id>"`` to each ``<div class="message">``
    block so deep links of the form ``messagesN.html#m361522`` jump
    straight to that message. ``data-id`` stays untouched — browsers
    target the HTML ``id`` attribute, not ``data-id``.
    """

    name = "message_anchor"

    def apply(self, msg: ParsedMessage, ctx: TransformContext) -> ParsedMessage:
        anchor = f' id="m{msg.vk_message_id}"'
        new_html, n = _MESSAGE_OPEN_RE.subn(
            lambda m: m.group(0)[:-1] + anchor + ">",
            msg.raw_html, count=1,
        )
        if n:
            msg.raw_html = new_html
        return msg


# Order matters: anchor first so InlinePhotos sees the augmented opening
# tag (it doesn't depend on it, but keeps the chain easier to reason about).
DEFAULT_TRANSFORMS: list[Transform] = [
    SenderNameFromDBTransform(),
    MessageAnchorTransform(),
    InlinePhotosTransform(),
]
