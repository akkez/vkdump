"""Tests for `parse_messages_index` + `extract_account_id` + the
peer → chat-type derivation. All values are synthetic."""
from __future__ import annotations

import pytest

from vkdump.parsers.vk.index import (
    CHAT_TYPE_COMMUNITY,
    CHAT_TYPE_DM,
    CHAT_TYPE_GROUP_CHAT,
    chat_type_for_peer,
    extract_account_id,
    parse_messages_index,
)


# An empty placeholder so the regex finds peer entries without us having
# to wire a full HTML document.
_INDEX_HTML = """
<html><body>
<div class="item">
  <div class='item__main'><div class="message-peer">
    <div class="message-peer--id">
      <a href="100/messages0.html">A User Name</a>
    </div>
  </div></div>
</div>
<div class="item">
  <div class='item__main'><div class="message-peer">
    <div class="message-peer--id">
      <a href="-200/messages0.html">A Community Name</a>
    </div>
  </div></div>
</div>
<div class="item">
  <div class='item__main'><div class="message-peer">
    <div class="message-peer--id">
      <a href="2000000001/messages0.html">Group Chat A</a>
    </div>
  </div></div>
</div>
<div class="item">
  <div class='item__main'><div class="message-peer">
    <div class="message-peer--id">
      <a href="example-slug/messages0.html">Conf with alias</a>
    </div>
  </div></div>
</div>
</body></html>
"""


def test_parse_messages_index_entries() -> None:
    entries = parse_messages_index(_INDEX_HTML)
    assert len(entries) == 4

    by_folder = {e.peer_folder: e for e in entries}
    assert by_folder["100"].title == "A User Name"
    assert by_folder["100"].peer_id == 100
    assert by_folder["-200"].peer_id == -200
    assert by_folder["2000000001"].peer_id == 2_000_000_001
    # Non-numeric slug → peer_id stays None.
    assert by_folder["example-slug"].peer_id is None


@pytest.mark.parametrize(
    "peer_folder, peer_id, expected",
    [
        ("100",             100,             CHAT_TYPE_DM),
        ("-200",           -200,             CHAT_TYPE_COMMUNITY),
        ("2000000001", 2_000_000_001,        CHAT_TYPE_GROUP_CHAT),
        ("alias",            None,           CHAT_TYPE_GROUP_CHAT),
    ],
)
def test_chat_type_derivation(peer_folder: str, peer_id: int | None, expected: str) -> None:
    assert chat_type_for_peer(peer_folder, peer_id) == expected


def test_extract_account_id_from_jd_meta() -> None:
    """jd meta = base64 of `{"user_id":N,"time_current":T}`."""
    # Synthetic owner id 42, generation epoch 1700000000.
    payload = (
        '<html><head>'
        '<meta name="jd" content="eyJ1c2VyX2lkIjo0MiwidGltZV9jdXJyZW50IjoxNzAwMDAwMDAwfQ==">'
        '</head></html>'
    )
    assert extract_account_id(payload) == "42"


def test_extract_account_id_missing_returns_none() -> None:
    assert extract_account_id("<html></html>") is None
