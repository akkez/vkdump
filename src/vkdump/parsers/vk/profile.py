"""Parse `profile/page-info.html` — the dump owner's profile page.

Fields we extract:
- vk_id (numeric) from the jd meta
- display_name from the "Полное имя" / "Full name" row
- avatar_url from the <img class="fans_fan_img"> tag

Anything we can't find is left as None and the caller continues without it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

from .messages import _decode_jd_meta, decode_dump_bytes
from .sources import Source


@dataclass
class ProfileInfo:
    vk_id: int | None
    display_name: str | None
    avatar_url: str | None


# Label localised by VK's exporter: Russian "Полное имя", English "Full name".
_FULL_NAME_RE = re.compile(
    r'<div class="item__tertiary">(?:Полное имя|Full name)</div>\s*<div>(?P<name>[^<]*)</div>',
    re.DOTALL,
)
_AVATAR_RE = re.compile(
    r'<img\s+class="fans_fan_img"\s+src="(?P<url>[^"]+)"', re.IGNORECASE
)


def parse_profile_page(html: str) -> ProfileInfo:
    jd = _decode_jd_meta(html)
    vk_id: int | None = None
    if jd is not None:
        uid = jd.get("user_id")
        if isinstance(uid, int):
            vk_id = uid

    name_m = _FULL_NAME_RE.search(html)
    display_name = unescape(name_m.group("name").strip()) if name_m else None
    # Collapse the double-space VK sometimes emits between first/last name.
    if display_name:
        display_name = re.sub(r"\s+", " ", display_name)

    avatar_m = _AVATAR_RE.search(html)
    avatar_url = avatar_m.group("url") if avatar_m else None

    return ProfileInfo(vk_id=vk_id, display_name=display_name, avatar_url=avatar_url)


def parse_profile_file(source: Source, rel: str) -> ProfileInfo:
    return parse_profile_page(decode_dump_bytes(source.read_bytes(rel)))
