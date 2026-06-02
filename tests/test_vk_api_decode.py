"""Regression tests for VKApi.call_raw's response decoder.

VK occasionally emits 4-byte UTF-8 sequences whose codepoint sits past
U+10FFFF — the bytes form a syntactically valid 4-byte sequence but
Python's strict UTF-8 decoder rejects it with the misleading message
"invalid continuation byte". The fetch-data run used to die there;
call_raw now falls back to U+FFFD replacement so a bad reply text in
one of thousands of messages doesn't take out the whole scrape.

The fixture bytes are synthetic — built from the *shape* of the real
incident (a reply_message.text field containing four corrupted bytes)
with invented ids / names so no real archive content leaks into the
repo.
"""
from __future__ import annotations

import asyncio
import json

import pytest
from loguru import logger

from vkdump.core.vk_api import VKApi


# 4 bytes that form a syntactically-valid UTF-8 sequence whose
# codepoint (U+113AFD) is past U+10FFFF — exactly the shape of the
# real-world VK garbage that triggered the bug.
_OUT_OF_RANGE_4BYTE = b"\xf4\x93\xab\xbd"


class _FakeResponse:
    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        self._body = body
        self.headers = {"Content-Type": content_type}

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeSession:
    """Minimal aiohttp.ClientSession stand-in that returns canned bytes.

    Only the surface call_raw actually uses — ``post(url, data=…)`` —
    is implemented. The returned context manager yields a _FakeResponse.
    """
    def __init__(self, body: bytes, content_type: str = "application/json") -> None:
        self._body = body
        self._content_type = content_type
        self.last_url: str | None = None
        self.last_data: dict | None = None

    def post(self, url: str, data: dict) -> _FakeResponse:
        self.last_url = url
        self.last_data = data
        return _FakeResponse(self._body, self._content_type)


def _make_vk_with_session(session: _FakeSession) -> VKApi:
    vk = VKApi("synthetic-token-do-not-use")
    vk._session = session  # type: ignore[assignment]
    return vk


def _payload_with_bad_text(bad_bytes: bytes) -> bytes:
    """Build a synthetic VK execute response with one reply.text field
    holding ``bad_bytes`` literally — i.e. the bytes are spliced into
    the JSON octet stream verbatim, not through json.dumps. Mirrors
    the way the bug arrived from VK's wire format.
    """
    prefix = (
        b'{"response":[{"count":1,"items":[{"id":1,"peer_id":2,"text":"ok",'
        b'"reply_message":{"from_id":3,"text":"'
    )
    suffix = (
        b'","attachments":[]}}]}],"execute_errors":[]}'
    )
    return prefix + bad_bytes + suffix


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_strict_utf8_decode_round_trip() -> None:
    """Sanity: a clean UTF-8 response parses normally."""
    body = json.dumps({"response": {"items": [{"id": 1, "text": "привет"}]}}).encode("utf-8")
    vk = _make_vk_with_session(_FakeSession(body))
    payload = _run(vk.call_raw("messages.getById"))
    assert payload["response"]["items"][0]["text"] == "привет"


def test_out_of_range_4byte_codepoint_is_salvaged_with_replacement(caplog) -> None:
    """A 4-byte UTF-8 sequence with codepoint > U+10FFFF must not abort
    the run. call_raw should log the decode failure, replace the bad
    bytes with U+FFFD, and still return parsed JSON."""
    raw = _payload_with_bad_text(_OUT_OF_RANGE_4BYTE)
    # Confirm the fixture really does trip strict UTF-8 — otherwise
    # the test would silently degrade to the clean path.
    with pytest.raises(UnicodeDecodeError):
        raw.decode("utf-8")

    # Loguru doesn't propagate to logging by default; wire it in for
    # the duration of this test so caplog catches both the ERROR dump
    # and the WARNING salvage line.
    import logging
    handler_id = logger.add(
        lambda msg: logging.getLogger("vkdump.core.vk_api").log(
            msg.record["level"].no, msg.record["message"]
        ),
        level="DEBUG",
    )
    try:
        with caplog.at_level("DEBUG", logger="vkdump.core.vk_api"):
            vk = _make_vk_with_session(_FakeSession(raw))
            payload = _run(vk.call_raw("execute"))
    finally:
        logger.remove(handler_id)

    items = payload["response"][0]["items"]
    assert items[0]["text"] == "ok"
    # Bad bytes replaced — the surviving text is just U+FFFD chars
    # (one per replaced byte under Python's 'replace' policy).
    assert items[0]["reply_message"]["text"] == "����"
    # Surrounding structure intact, ids etc. untouched.
    assert items[0]["reply_message"]["from_id"] == 3

    messages = [r.message for r in caplog.records]
    assert any("response decode failed" in m for m in messages), messages
    assert any("salvaged" in m and "U+FFFD" in m for m in messages), messages


def test_request_body_in_dump_has_token_redacted(caplog) -> None:
    """The error dump must never leak the access token — the dump line
    is the one a user is most likely to paste into a bug report."""
    raw = _payload_with_bad_text(_OUT_OF_RANGE_4BYTE)
    import logging
    handler_id = logger.add(
        lambda msg: logging.getLogger("vkdump.core.vk_api").log(
            msg.record["level"].no, msg.record["message"]
        ),
        level="DEBUG",
    )
    try:
        with caplog.at_level("DEBUG", logger="vkdump.core.vk_api"):
            vk = _make_vk_with_session(_FakeSession(raw))
            _run(vk.call_raw("execute", code="API.messages.getById({...})"))
    finally:
        logger.remove(handler_id)

    dump = next(r.message for r in caplog.records if "response decode failed" in r.message)
    assert "<redacted>" in dump
    assert "synthetic-token-do-not-use" not in dump
