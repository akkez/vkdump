"""End-to-end tests for `discover()` over a synthetic directory tree
that mimics the canonical VK archive layout.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vkdump.parsers.vk import discover


_HEADER = (
    '<!DOCTYPE html><html><head><meta charset="windows-1251">'
    '<meta name="jd" content="eyJ1c2VyX2lkIjo0MiwidGltZV9jdXJyZW50IjoxN30=">'
    '</head><body>'
)
_FOOTER = '</body></html>'


def _w1251(s: str) -> bytes:
    return s.encode("windows-1251")


def _make_chat_folder(root: Path, peer_folder: str, *, pages: int = 1) -> None:
    folder = root / peer_folder
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(pages):
        body = (
            f'{_HEADER}'
            f'<div class="item"><div class="item__main">'
            f'<div class="message" data-id="{i}">'
            f'<div class="message__header">You, at 1:00:00 pm on 1 Jan 2024</div>'
            f'<div>hi<div class="kludges"></div></div>'
            f'</div></div></div>'
            f'{_FOOTER}'
        )
        (folder / f"messages{i * 50}.html").write_bytes(_w1251(body))


def _index_messages(*peer_folders: str) -> bytes:
    rows = "".join(
        f'<div class="item"><div class="item__main"><div class="message-peer">'
        f'<div class="message-peer--id">'
        f'<a href="{pf}/messages0.html">title-{pf}</a>'
        f'</div></div></div></div>'
        for pf in peer_folders
    )
    return _w1251(_HEADER + rows + _FOOTER)


def _profile_page() -> bytes:
    body = (
        _HEADER
        + '<div class="item"><div class="item__tertiary">Полное имя</div>'
          '<div>John Doe</div></div>'
        + _FOOTER
    )
    return _w1251(body)


# ---------- archive root (canonical layout) ----------


def test_discover_archive_root(tmp_path: Path) -> None:
    """`<root>/index.html` + `<root>/messages/index-messages.html` +
    `<root>/profile/page-info.html` + per-peer chat folders."""
    (tmp_path / "index.html").write_bytes(_w1251(_HEADER + _FOOTER))
    msgs_dir = tmp_path / "messages"
    msgs_dir.mkdir()
    (msgs_dir / "index-messages.html").write_bytes(_index_messages("100", "-200"))
    _make_chat_folder(msgs_dir, "100", pages=2)
    _make_chat_folder(msgs_dir, "-200", pages=1)
    (tmp_path / "profile").mkdir()
    (tmp_path / "profile" / "page-info.html").write_bytes(_profile_page())

    d = discover(tmp_path)
    try:
        assert d.profile_file == "profile/page-info.html"
        assert d.messages_index_file == "messages/index-messages.html"
        assert sorted(d.chat_folders) == ["messages/-200", "messages/100"]
        assert d.single_html_files == []
    finally:
        d.source.close()


# ---------- messages root ----------


def test_discover_messages_root(tmp_path: Path) -> None:
    (tmp_path / "index-messages.html").write_bytes(_index_messages("100"))
    _make_chat_folder(tmp_path, "100")

    d = discover(tmp_path)
    try:
        assert d.messages_index_file == "index-messages.html"
        assert d.chat_folders == ["100"]
    finally:
        d.source.close()


# ---------- single chat folder ----------


def test_discover_single_chat_folder(tmp_path: Path) -> None:
    _make_chat_folder(tmp_path, "", pages=3)  # files directly in tmp_path
    d = discover(tmp_path)
    try:
        # No index / profile, but the folder itself is a chat folder.
        assert d.messages_index_file is None
        assert d.profile_file is None
        assert d.chat_folders == [""]
    finally:
        d.source.close()


# ---------- single HTML files ----------


def test_discover_single_messages_html(tmp_path: Path) -> None:
    chat = tmp_path / "100"
    _make_chat_folder(tmp_path, "100", pages=1)
    target = chat / "messages0.html"

    d = discover(target)
    try:
        assert d.single_html_files == ["messages0.html"]
        assert d.chat_folders == []
    finally:
        d.source.close()


def test_discover_single_index_messages(tmp_path: Path) -> None:
    (tmp_path / "index-messages.html").write_bytes(_index_messages("100"))
    _make_chat_folder(tmp_path, "100")

    d = discover(tmp_path / "index-messages.html")
    try:
        assert d.messages_index_file == "index-messages.html"
        assert d.chat_folders == ["100"]
    finally:
        d.source.close()


def test_discover_single_page_info(tmp_path: Path) -> None:
    (tmp_path / "page-info.html").write_bytes(_profile_page())
    d = discover(tmp_path / "page-info.html")
    try:
        assert d.profile_file == "page-info.html"
        assert d.chat_folders == []
        assert d.single_html_files == []
    finally:
        d.source.close()


def test_discover_unrecognised_file_raises(tmp_path: Path) -> None:
    (tmp_path / "random.html").write_bytes(_w1251(_HEADER + _FOOTER))
    with pytest.raises(ValueError):
        discover(tmp_path / "random.html")
