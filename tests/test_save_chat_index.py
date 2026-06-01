"""Tests for the save-chat top-level export index (HTML + JSON sidecar).

Exercises the small surface area that's user-visible:
- the `exported_at` value loses the `T` / `+00:00` cruft on render,
- each row gets a stable `id="chat-<slug>"` so the GUI's Open Output
  button can deep-link with a `#chat-<slug>` fragment,
- legacy ISO-formatted sidecars still render cleanly without a
  forced re-migration of the JSON file.
"""
from __future__ import annotations

import json
from pathlib import Path

from vkdump.modules.save_chat.index import (
    _fmt_exported_at,
    _render_html,
    ExportEntry,
    ExportManifest,
    load,
    upsert,
)


def _entry(**overrides) -> ExportEntry:
    base = dict(
        chat_slug="1_TestChat",
        peer_id="2000000001",
        title="Test Chat",
        type="group_chat",
        message_count=42,
        exported_at="2026-05-31 00:26:38",
        first_page="messages/messages0.html",
        chat_id=1,
        photos_page="photos.html",
        photos_count=7,
    )
    base.update(overrides)
    return ExportEntry(**base)


def test_fmt_exported_at_strips_t_and_timezone() -> None:
    assert _fmt_exported_at("2026-05-31T00:26:38+00:00") == "2026-05-31 00:26:38"


def test_fmt_exported_at_strips_z_suffix() -> None:
    assert _fmt_exported_at("2026-05-31T00:26:38Z") == "2026-05-31 00:26:38"


def test_fmt_exported_at_passthrough_for_already_clean_values() -> None:
    assert _fmt_exported_at("2026-05-31 00:26:38") == "2026-05-31 00:26:38"


def test_fmt_exported_at_handles_empty() -> None:
    assert _fmt_exported_at("") == ""


def test_fmt_exported_at_returns_raw_when_unparseable() -> None:
    assert _fmt_exported_at("not a date") == "not a date"


def test_render_html_emits_chat_anchor_id() -> None:
    """Each row carries `id="chat-<slug>"` so the GUI can deep-link
    with `index.html#chat-<slug>`."""
    manifest = ExportManifest(chats=[_entry(chat_slug="42_Hello")])
    html = _render_html(manifest)
    assert 'id="chat-42_Hello"' in html


def test_render_html_normalises_legacy_iso_timestamp() -> None:
    """A sidecar that still carries the old ISO-with-tz format renders
    with the cleaned-up timestamp; no JSON migration required."""
    manifest = ExportManifest(chats=[_entry(exported_at="2026-05-31T00:26:38+00:00")])
    html = _render_html(manifest)
    assert "2026-05-31 00:26:38" in html
    assert "T00:26:38" not in html
    assert "+00:00" not in html


def test_render_html_includes_target_highlight_rule() -> None:
    """The `:target` CSS rule is what visually marks the row the user
    deep-linked to. Pin its presence so a cosmetic tweak doesn't
    accidentally drop the highlight."""
    manifest = ExportManifest(chats=[_entry()])
    html = _render_html(manifest)
    assert "li:target" in html


def test_upsert_writes_clean_timestamp_format(tmp_path: Path) -> None:
    """`_now()` no longer emits `T`/`+00:00`, so freshly-written sidecars
    carry the simple `YYYY-MM-DD HH:MM:SS` form."""
    upsert(
        output_dir=tmp_path,
        chat_slug="1_Chat",
        peer_id="2000000001",
        title="Chat",
        type_="group_chat",
        message_count=5,
        first_page="messages/messages0.html",
        chat_id=1,
    )
    sidecar = json.loads((tmp_path / "_export.json").read_text(encoding="utf-8"))
    [c] = sidecar["chats"]
    # YYYY-MM-DD HH:MM:SS with a single space between date and time.
    assert "T" not in c["exported_at"]
    assert "+" not in c["exported_at"]
    assert c["exported_at"][4] == "-"
    assert c["exported_at"][10] == " "
    assert c["exported_at"][13] == ":"


def test_open_path_appends_chat_slug_anchor(tmp_path: Path) -> None:
    """A specific-chat run yields an Open Output path with a
    `#chat-<slug>` fragment so the index opens scrolled to that row."""
    from vkdump.tasks.registry import _save_chat_open_path

    # Pre-populate the output dir with an index.html — the open_path
    # callable refuses to point at a missing file.
    upsert(
        output_dir=tmp_path,
        chat_slug="42_Some_Chat",
        peer_id="2000000001",
        title="Some Chat",
        type_="group_chat",
        message_count=1,
        chat_id=42,
    )
    result = {"output_dir": str(tmp_path), "chat_slug": "42_Some_Chat"}
    path = _save_chat_open_path(result)
    assert path is not None
    assert path.endswith("/index.html#chat-42_Some_Chat")


def test_open_path_drops_anchor_in_all_chats_mode(tmp_path: Path) -> None:
    """"All chats" mode returns a manifest-level result without a
    `chat_slug` key — Open Output should just open the plain index."""
    from vkdump.tasks.registry import _save_chat_open_path

    upsert(
        output_dir=tmp_path,
        chat_slug="42_Some_Chat",
        peer_id="2000000001",
        title="Some Chat",
        type_="group_chat",
        message_count=1,
        chat_id=42,
    )
    result = {"output_dir": str(tmp_path), "mode": "all-chats"}
    path = _save_chat_open_path(result)
    assert path is not None
    assert "#" not in path
    assert path.endswith("/index.html")


def test_upsert_roundtrip_through_load(tmp_path: Path) -> None:
    """Sanity: writing then re-reading the sidecar preserves the entry,
    and a re-render off the loaded manifest contains the same row id."""
    upsert(
        output_dir=tmp_path,
        chat_slug="1_Chat",
        peer_id="2000000001",
        title="Chat",
        type_="group_chat",
        message_count=5,
        chat_id=1,
    )
    manifest = load(tmp_path)
    assert len(manifest.chats) == 1
    html = _render_html(manifest)
    assert 'id="chat-1_Chat"' in html
