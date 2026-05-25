"""Parse `messages/index-messages.html` — the dump-wide chat list.

Each row is a peer reference:

    <div class="message-peer">
      <div class="message-peer--id">
        <a href="<peer_folder>/messages0.html">Chat Title</a>
      </div>
    </div>

`peer_folder` is the chat's folder name relative to the messages root —
numeric (positive for DMs, negative for communities) or a slug ("<conf-slug>").
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

from .messages import _decode_jd_meta, decode_dump_bytes
from .sources import Source


@dataclass
class ChatIndexEntry:
    peer_folder: str        # the directory name relative to the messages root
    title: str | None       # display title from the index
    peer_id: int | None     # numeric peer id parsed from peer_folder, if any


_PEER_LINK_RE = re.compile(
    r'<div class="message-peer">\s*'
    r'<div class="message-peer--id">\s*'
    r'<a href="(?P<href>[^"]+)">(?P<title>.*?)</a>',
    re.DOTALL,
)


def parse_messages_index(html: str) -> list[ChatIndexEntry]:
    out: list[ChatIndexEntry] = []
    for m in _PEER_LINK_RE.finditer(html):
        href = m.group("href").strip()
        # href looks like '<peer_folder>/messages0.html'.
        peer_folder = href.split("/", 1)[0]
        title = unescape(m.group("title").strip()) or None
        peer_id: int | None = None
        if _numeric_signed(peer_folder):
            peer_id = int(peer_folder)
        out.append(
            ChatIndexEntry(peer_folder=peer_folder, title=title, peer_id=peer_id)
        )
    return out


def extract_account_id(html: str) -> str | None:
    """Pull the dump owner's vk_id (numeric, stringified) from the
    `<meta name="jd">` payload that VK embeds on every dump page. Same
    value across every HTML file in one archive.
    """
    jd = _decode_jd_meta(html)
    if jd is None:
        return None
    uid = jd.get("user_id")
    return str(uid) if uid is not None else None


def parse_messages_index_file(source: Source, rel: str) -> list[ChatIndexEntry]:
    return parse_messages_index(decode_dump_bytes(source.read_bytes(rel)))


CHAT_TYPE_DM = "dm"                  # 1:1 conversation with a VK user
CHAT_TYPE_COMMUNITY = "community"    # 1:1 conversation with a VK community / page (bots run on community accounts too — indistinguishable from HTML alone)
CHAT_TYPE_GROUP_CHAT = "group_chat"  # multi-user conversation (a.k.a. conf)


def chat_type_for_peer(peer_folder: str, peer_id: int | None) -> str:
    """Derive a chat type slug from the folder name / peer id.

    VK encodes the peer kind in the id range:
    - negative numeric → conversation with a community / group / page;
    - positive numeric < 2_000_000_000 → 1:1 DM with a VK user;
    - numeric ≥ 2_000_000_000 → multi-user chat (`peer = 2e9 + chat_id`);
    - non-numeric folder slug (e.g. `<some-conf-slug>`) → multi-user chat exported
      under a human alias instead of its numeric peer id.
    """
    if peer_id is None:
        return CHAT_TYPE_GROUP_CHAT
    if peer_id < 0:
        return CHAT_TYPE_COMMUNITY
    if peer_id >= 2_000_000_000:
        return CHAT_TYPE_GROUP_CHAT
    return CHAT_TYPE_DM


def _numeric_signed(s: str) -> bool:
    if not s:
        return False
    if s[0] in "-+":
        return s[1:].isdigit()
    return s.isdigit()
