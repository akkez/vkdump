"""Concrete transforms used by save-chat. New media types (video poster,
audio player, …) plug in here without touching the orchestrator.
"""
from __future__ import annotations

import os
import re
import shutil
from html import escape as html_escape
from pathlib import Path

from ...parsers.vk.models import KIND_PHOTO, ParsedAttachment, ParsedMessage
from .pipeline import Transform, TransformContext


def _link_re(url: str) -> re.Pattern[str]:
    # The VK dump always writes `<a class="attachment__link" href="...">`
    # with the URL verbatim. Match that exact link and capture an open
    # boundary before it so we can inject ahead of the original anchor.
    return re.compile(
        r'<a\s+class=[\'"]attachment__link[\'"]\s+href=[\'"]'
        + re.escape(url)
        + r'[\'"]'
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
            local_rel = ctx.url_to_local.get(att.url)
            if not local_rel:
                continue
            src = ctx.static_root / local_rel
            if not src.is_file():
                continue
            asset_rel = self._materialise_asset(src, year, ctx)
            new_html = self._inject(new_html, att, asset_rel)
        if new_html is not msg.raw_html:
            msg.raw_html = new_html
        return msg

    @staticmethod
    def _materialise_asset(src: Path, year: str, ctx: TransformContext) -> str:
        """Copy/link `src` into the chat's asset tree, return the href
        the rendered page should use — relative to `pages_dir` (where
        the messagesN.html files live), POSIX-style. Since pages sit
        at `<chat>/messages/`, that gives `../assets/<year>/...`.
        """
        already = ctx.copied_assets.get(src)
        if already is None:
            # enrich.py names files <sha[:16]>.<ext>; the bucket is the
            # first two hex chars, same convention as data/static/.
            bucket = src.stem[:2] if len(src.stem) >= 2 else "_"
            rel = Path("assets") / year / "photos" / bucket / src.name
            dst = ctx.output_chat_dir / rel
            _link_asset(src, dst)
            ctx.copied_assets[src] = dst
        else:
            dst = already
        return os.path.relpath(dst, ctx.pages_dir).replace(os.sep, "/")

    @staticmethod
    def _inject(html: str, att: ParsedAttachment, asset_href: str) -> str:
        pat = _link_re(att.url or "")
        m = pat.search(html)
        if m is None:
            return html
        alt = html_escape(att.description or "photo", quote=True)
        href = html_escape(asset_href, quote=True)
        # Block-level wrapper with inline styles so we don't depend on
        # VK's own CSS: description/link in VK markup is inline, so
        # without `display:block` the injected image flows mid-sentence
        # next to "Фотография" and the URL. Margins separate it from
        # the surrounding text; max-width keeps wide images contained.
        tag = (
            '<div class="vkdump-inline-photo"'
            ' style="display:block;margin:6px 0">'
            f'<a href="{href}" style="display:inline-block">'
            f'<img loading="lazy" src="{href}" alt="{alt}"'
            ' style="display:block;max-width:100%;height:auto">'
            '</a>'
            '</div>'
        )
        return html[:m.start()] + tag + html[m.start():]


DEFAULT_TRANSFORMS: list[Transform] = [InlinePhotosTransform()]
