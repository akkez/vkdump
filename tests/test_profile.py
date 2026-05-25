"""Tests for `parse_profile_page` — the dump owner's profile page."""
from __future__ import annotations

from vkdump.parsers.vk.profile import parse_profile_page


def _page(name_block: str = "", img_src: str | None = None) -> str:
    img = (
        f'<img class="fans_fan_img" src="{img_src}">'
        if img_src is not None else ""
    )
    return (
        '<html><head>'
        '<meta name="jd" content="eyJ1c2VyX2lkIjo0MiwidGltZV9jdXJyZW50IjoxN30=">'
        '</head><body>' + img + name_block + '</body></html>'
    )


def test_parse_profile_full() -> None:
    """Russian-locale 'Полное имя' row + avatar img."""
    html = _page(
        name_block=(
            '<div class="item">'
            '<div class="item__tertiary">Полное имя</div>'
            '<div>Ivan  Ivanov</div>'   # double-space VK sometimes emits
            '</div>'
        ),
        img_src="https://example.com/avatar.jpg",
    )
    info = parse_profile_page(html)
    assert info.vk_id == 42
    assert info.display_name == "Ivan Ivanov"   # collapsed
    assert info.avatar_url == "https://example.com/avatar.jpg"


def test_parse_profile_minimal() -> None:
    """Missing name + avatar — owner id alone is enough."""
    info = parse_profile_page('<html><head><meta name="jd" content="eyJ1c2VyX2lkIjo0MiwidGltZV9jdXJyZW50IjoxN30="></head></html>')
    assert info.vk_id == 42
    assert info.display_name is None
    assert info.avatar_url is None


def test_parse_profile_no_jd_yields_none_vk_id() -> None:
    info = parse_profile_page("<html><body>nothing</body></html>")
    assert info.vk_id is None
