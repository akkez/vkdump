"""Parse a VK dump messages*.html page into structured records.

The dump HTML is hand-written by VK's export tool; the layout is stable but
contains nested <div>s inside each message body (the "kludges" attachments
block). Pure-regex slicing fails on the nested case, so we mix regex with a
small balanced-<div> scanner.

Two goals shape the code:

1. Extract every field we can without inventing data.
2. When a single message item fails to parse, isolate the failure (return it
   as a ParseError) so the rest of the page still lands in the DB and the
   bad HTML can be inspected and re-parsed later.
"""
from __future__ import annotations

import base64
import json
import re
import traceback
from datetime import datetime, timezone
from html import unescape
from typing import Iterator

from .sources import Source

from .dates import parse_vk_datetime
from .models import (
    KIND_AUDIO,
    KIND_FILE,
    KIND_FORWARD,
    KIND_PHOTO,
    KIND_UNKNOWN,
    KIND_VIDEO,
    KIND_WALL_COMMENT,
    KIND_WALL_POST,
    ParsedAttachment,
    ParsedChatMeta,
    ParsedMessage,
    ParseError,
)


VK_DUMP_ENCODING = "windows-1251"

_MSG_OPEN_RE = re.compile(r'<div class="message" data-id="(?P<id>\d+)">')
_HEADER_RE = re.compile(
    r'<div class="message__header">(?P<header>.*?)</div>', re.DOTALL
)
_BODY_OPEN_RE = re.compile(r"<div>", re.IGNORECASE)
_ATT_OPEN_RE = re.compile(r'<div class="attachment">')

_HEADER_LINK_RE = re.compile(
    r'<a href="https://vk\.com/id(?P<vk_id>\d+)"[^>]*>(?P<name>.*?)</a>\s*,\s*(?P<date>.*)',
    re.DOTALL,
)
_HEADER_SELF_RE = re.compile(r"^\s*Вы\s*,\s*(?P<date>.*)", re.DOTALL)

_KLUDGES_OPEN_RE = re.compile(r'<div class="kludges">')
_ATTACHMENT_DESC_RE = re.compile(
    r'<div class="attachment__description">(?P<desc>[^<]*)</div>'
)
_ATTACHMENT_LINK_RE = re.compile(
    r'<a class=[\'"]attachment__link[\'"] href=[\'"](?P<url>[^\'"]+)[\'"]'
)

_FORWARD_DESC_RE = re.compile(r"^(\d+)\s+прикреплённ")

_KIND_BY_DESC: dict[str, str] = {
    "Фотография": KIND_PHOTO,
    "Видеозапись": KIND_VIDEO,
    "Аудиозапись": KIND_AUDIO,
    "Файл": KIND_FILE,
    "Запись на стене": KIND_WALL_POST,
    "Комментарий на стене": KIND_WALL_COMMENT,
}

_CRUMB_RE = re.compile(r'<div class="ui_crumb"[^>]*>([^<]+)</div>')
_META_JD_RE = re.compile(r'<meta name="jd" content="([^"]+)"')
_PAGINATION_LAST_RE = re.compile(r'href="messages(\d+)\.html"')


def decode_dump_bytes(data: bytes) -> str:
    """Decode raw HTML bytes using the encoding declared in the dump."""
    return data.decode(VK_DUMP_ENCODING)


def read_page(source: Source, rel: str) -> str:
    return decode_dump_bytes(source.read_bytes(rel))


def parse_chat_meta(html: str, source_folder: str) -> ParsedChatMeta:
    """Extract chat title, dump owner id, dump generation time, and a
    total-messages estimate.

    Should be called against the *first* page (messages0.html) — pagination
    on later pages does not necessarily expose the last-page offset.
    """
    title = _extract_chat_title(html)
    jd = _decode_jd_meta(html)
    account_id: str | None = None
    dump_generated_at: datetime | None = None
    if jd is not None:
        uid = jd.get("user_id")
        if uid is not None:
            account_id = str(uid)
        tc = jd.get("time_current")
        if isinstance(tc, (int, float)):
            dump_generated_at = datetime.fromtimestamp(int(tc), tz=timezone.utc)
    total = _estimate_total_from_pagination(html)
    return ParsedChatMeta(
        source_folder=source_folder,
        title=title,
        account_id=account_id,
        total_expected_count=total,
        dump_generated_at=dump_generated_at,
    )


def _extract_chat_title(html: str) -> str | None:
    crumbs = _CRUMB_RE.findall(html)
    if not crumbs:
        return None
    return unescape(crumbs[-1].strip())


def _decode_jd_meta(html: str) -> dict | None:
    """Decode the base64-JSON `<meta name="jd">` payload, or None."""
    m = _META_JD_RE.search(html)
    if not m:
        return None
    try:
        raw = m.group(1)
        padded = raw + "=" * (-len(raw) % 4)
        return json.loads(base64.b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return None


def _estimate_total_from_pagination(html: str) -> int | None:
    """Take the largest offset in the pagination block as the last page's
    starting offset — a lower-bound progress hint.
    """
    offsets = [int(m) for m in _PAGINATION_LAST_RE.findall(html)]
    return max(offsets) if offsets else None


def _find_balanced_div_end(html: str, after_open: int) -> int:
    """Given an index right after a `<div...>` opening tag, return the index
    of the matching `</div>` close (pointing at the '<'). Counts only `<div`
    opens and `</div>` closes; ignores other tags.
    """
    depth = 1
    i = after_open
    while depth > 0:
        next_open = html.find("<div", i)
        next_close = html.find("</div>", i)
        if next_close == -1:
            raise ValueError("unbalanced <div> while scanning")
        if next_open != -1 and next_open < next_close:
            depth += 1
            i = next_open + 4
        else:
            depth -= 1
            i = next_close + 6
            if depth == 0:
                return next_close
    raise ValueError("unbalanced <div> while scanning")


def _iter_message_blocks(html: str) -> Iterator[tuple[int, int, int, str]]:
    """Yield (vk_id, block_start, block_end_exclusive, inner_html) for every
    `<div class="message" data-id="...">...</div>` block on the page.
    """
    cursor = 0
    while True:
        m = _MSG_OPEN_RE.search(html, cursor)
        if not m:
            return
        block_start = m.start()
        after_open = m.end()
        close_at = _find_balanced_div_end(html, after_open)
        inner = html[after_open:close_at]
        yield int(m.group("id")), block_start, close_at + 6, inner
        cursor = close_at + 6


def parse_page(html: str, source_file: str) -> tuple[list[ParsedMessage], list[ParseError]]:
    """Parse every message item on a single page.

    Returns (messages, errors). A single broken item never blocks the rest.
    """
    messages: list[ParsedMessage] = []
    errors: list[ParseError] = []
    for vk_id, b_start, b_end, inner in _iter_message_blocks(html):
        raw_block = html[b_start:b_end]
        try:
            messages.append(_parse_one_message(vk_id, inner, raw_block, source_file))
        except Exception as exc:
            errors.append(
                ParseError(
                    source_file=source_file,
                    raw_html=raw_block,
                    error=f"{type(exc).__name__}: {exc}",
                    traceback=traceback.format_exc(),
                )
            )
    return messages, errors


def _parse_one_message(
    vk_id: int, inner_html: str, raw_block: str, source_file: str
) -> ParsedMessage:
    header_match = _HEADER_RE.search(inner_html)
    if not header_match:
        raise ValueError("missing message__header")
    header = header_match.group("header")

    # Body div: first <div> after the header.
    body_open = _BODY_OPEN_RE.search(inner_html, header_match.end())
    if not body_open:
        raise ValueError("missing message body div")
    body_close = _find_balanced_div_end(inner_html, body_open.end())
    body = inner_html[body_open.end():body_close]

    sender_vk_id, sender_name, sender_is_self, date_str = _parse_header(header)
    sent_at = parse_vk_datetime(date_str)

    text, kludges_html = _split_body(body)
    attachments = _parse_attachments(kludges_html)
    forwarded_count = sum(
        (a.forward_count or 0) for a in attachments if a.kind == KIND_FORWARD
    )

    return ParsedMessage(
        vk_message_id=vk_id,
        sent_at=sent_at,
        sender_vk_id=sender_vk_id,
        sender_display_name=sender_name,
        sender_is_self=sender_is_self,
        text=text,
        attachments=attachments,
        has_forwards=forwarded_count > 0,
        forwarded_count=forwarded_count,
        is_reply=False,
        reply_to_message_id=None,
        raw_html=raw_block,
        source_file=source_file,
    )


def _parse_header(header: str) -> tuple[int | None, str | None, bool, str]:
    """Return (vk_id, display_name, is_self, raw_date_string)."""
    m = _HEADER_LINK_RE.search(header)
    if m:
        return (
            int(m.group("vk_id")),
            unescape(m.group("name").strip()),
            False,
            m.group("date").strip(),
        )
    m = _HEADER_SELF_RE.match(header.strip())
    if m:
        return None, "Вы", True, m.group("date").strip()
    raise ValueError(f"unrecognised header layout: {header!r}")


def _split_body(body: str) -> tuple[str, str]:
    """Return (plain_text, kludges_html).

    Body looks like ``TEXT<br>MORE TEXT<div class="kludges">...</div>``.
    If kludges is present it terminates the text portion. <br> becomes
    newline; HTML entities are unescaped; any other stray inline tags have
    their text content preserved.
    """
    kludges_html = ""
    text_html = body
    m = _KLUDGES_OPEN_RE.search(body)
    if m:
        text_html = body[: m.start()]
        # Take the kludges balanced block inclusive of its inner content; the
        # closing </div> is balanced with respect to the *kludges* opener.
        kludges_close = _find_balanced_div_end(body, m.end())
        kludges_html = body[m.end():kludges_close]
    text = re.sub(r"<br\s*/?>", "\n", text_html, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return unescape(text).strip(), kludges_html


def _parse_attachments(kludges_html: str) -> list[ParsedAttachment]:
    out: list[ParsedAttachment] = []
    pos = 0
    for i, m in enumerate(_ATT_OPEN_RE.finditer(kludges_html)):
        att_close = _find_balanced_div_end(kludges_html, m.end())
        att_inner = kludges_html[m.end():att_close]
        desc_m = _ATTACHMENT_DESC_RE.search(att_inner)
        if not desc_m:
            continue
        desc_raw = unescape(desc_m.group("desc").strip())
        link_m = _ATTACHMENT_LINK_RE.search(att_inner)
        url = link_m.group("url") if link_m else None
        kind = _KIND_BY_DESC.get(desc_raw)
        forward_count: int | None = None
        if kind is None:
            fwd = _FORWARD_DESC_RE.match(desc_raw)
            if fwd:
                kind = KIND_FORWARD
                forward_count = int(fwd.group(1))
            else:
                kind = KIND_UNKNOWN
        out.append(
            ParsedAttachment(
                kind=kind,
                description=desc_raw,
                url=url,
                forward_count=forward_count,
                position=pos,
            )
        )
        pos += 1
    return out
