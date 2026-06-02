"""Async client for the VK API.

One client owns one aiohttp.ClientSession — open with
`async with VKApi(token) as vk: ...`. VK's "logical" errors
(invalid token, missing permission, ...) come back as
`{"error": {...}}` with HTTP 200; the client raises
:class:`VKApiError` for those just like for transport failures.
"""
from __future__ import annotations

import asyncio
import json
import ssl
from typing import Any

import aiohttp
import certifi
from loguru import logger


API_BASE = "https://api.vk.com/method"
API_VERSION = "5.199"


class VKApiError(RuntimeError):
    """Raised for any VK API failure (transport or logical)."""

    def __init__(self, code: int | None, message: str) -> None:
        self.code = code
        self.message = message
        if code is None:
            super().__init__(f"VK API error: {message}")
        else:
            super().__init__(f"VK API error {code}: {message}")


class VKApiTimeout(VKApiError):
    """Raised when a request didn't complete within the client timeout.

    Caller-side retry loops single out this subclass so a slow VK
    endpoint doesn't kill a multi-million-message scrape — every other
    VKApiError still bubbles up untouched.
    """


class VKApi:
    def __init__(self, access_token: str, *, timeout: float = 15.0) -> None:
        self._token = access_token
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "VKApi":
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        self._session = aiohttp.ClientSession(
            timeout=self._timeout, connector=connector
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("VKApi must be used as an async context manager")
        return self._session

    async def call_raw(self, method: str, **params: Any) -> dict:
        """POST to ``method`` and return VK's full JSON payload as-is
        (including ``error`` blocks). Transport failures still raise
        :class:`VKApiError`.

        Responses are read as raw bytes and decoded manually so a
        non-UTF-8 byte (occasionally seen in VK execute payloads when
        a malformed message slips into the response) produces a
        diagnosable error: the failing request body, the response
        encoding header, and the surrounding bytes are dumped to the
        log instead of the bare ``UnicodeDecodeError`` aiohttp would
        raise from inside ``resp.json``.
        """
        body: dict[str, Any] = {
            "access_token": self._token,
            "v": API_VERSION,
        }
        for k, v in params.items():
            if v is None:
                continue
            if isinstance(v, (list, tuple)):
                body[k] = ",".join(str(x) for x in v)
            elif isinstance(v, bool):
                body[k] = "1" if v else "0"
            else:
                body[k] = v
        try:
            async with self.session.post(f"{API_BASE}/{method}", data=body) as resp:
                raw = await resp.read()
                content_type = resp.headers.get("Content-Type")
        except asyncio.TimeoutError as e:
            raise VKApiTimeout(None, "request timed out") from e
        except aiohttp.ClientError as e:
            raise VKApiError(None, f"network error: {e}") from e
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            # VK occasionally emits a 4-byte sequence whose codepoint
            # is past U+10FFFF (Python's strict UTF-8 decoder rejects
            # it as "invalid continuation byte"); observed in
            # reply_message.text fields where the upstream chat
            # contained binary garbage. Losing one user's bad byte
            # shouldn't kill a multi-million-message scrape — dump
            # the surrounding context so the maintainer can repro,
            # then re-decode with U+FFFD replacement and keep going.
            _dump_decode_failure(method, body, raw, content_type, e)
            text = raw.decode("utf-8", errors="replace")
            logger.warning(
                "VK API: salvaged {} bytes for method={} with U+FFFD replacement"
                " (first bad byte at offset {}, reason={!r})",
                len(raw), method, e.start, e.reason,
            )
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            _dump_json_failure(method, body, text, e)
            raise VKApiError(
                None,
                f"response JSON parse failed: {e.msg} at line {e.lineno} col {e.colno}"
                f" (method={method}, {len(text)} chars)",
            ) from e
        if not isinstance(payload, dict):
            return {"response": payload}
        return payload

    async def call(self, method: str, **params: Any) -> Any:
        payload = await self.call_raw(method, **params)
        if "error" in payload:
            err = payload["error"] or {}
            raise VKApiError(err.get("error_code"), err.get("error_msg") or "unknown")
        if "response" in payload:
            return payload["response"]
        return payload

    async def execute(self, code: str) -> tuple[Any, list[dict]]:
        """Run a VKScript snippet via the ``execute`` method.

        ``code`` is JavaScript-flavoured VKScript that can issue up to
        25 sub-calls via ``API.method.name({...})`` and must end in a
        ``return <value>;`` statement. Returns ``(response, errors)``:

        * ``response`` — VK's deserialized ``response`` array (with
          ``false`` in slots whose sub-call failed).
        * ``errors`` — the ``execute_errors`` list (one entry per failed
          sub-call, with ``error_code`` / ``error_msg`` / ``method``).

        Top-level errors (bad token, malformed code) still raise
        :class:`VKApiError`.
        """
        payload = await self.call_raw("execute", code=code)
        if "error" in payload:
            err = payload["error"] or {}
            raise VKApiError(err.get("error_code"), err.get("error_msg") or "unknown")
        errors = payload.get("execute_errors") or []
        if not isinstance(errors, list):
            errors = []
        return payload.get("response"), [e for e in errors if isinstance(e, dict)]

    async def users_get(
        self,
        user_ids: list[int | str] | None = None,
        fields: list[str] | None = None,
    ) -> list[dict]:
        return await self.call("users.get", user_ids=user_ids, fields=fields)

    async def account_get_app_permissions(self, user_id: int | None = None) -> int:
        """Return the permission bitmask. Raises VKApiError on a dead token."""
        return int(await self.call("account.getAppPermissions", user_id=user_id))

    async def fetch_bytes(self, url: str) -> tuple[bytes, str | None]:
        """Best-effort GET of an arbitrary URL using the same session.
        Returns ``(body, content_type)``."""
        async with self.session.get(url) as resp:
            resp.raise_for_status()
            ct = resp.headers.get("Content-Type")
            data = await resp.read()
        return data, ct


def _redact_body(body: dict[str, Any]) -> dict[str, Any]:
    """Drop the access_token so log dumps are safe to share."""
    return {k: v for k, v in body.items() if k != "access_token"} | {
        "access_token": "<redacted>" if "access_token" in body else None
    }


def _dump_decode_failure(
    method: str, body: dict[str, Any], raw: bytes,
    content_type: str | None, err: UnicodeDecodeError,
) -> None:
    """Log enough context to reproduce a non-UTF-8 VK response.

    Includes the request method + params, the response content-type
    header, the byte offset of the failure, a small hex window around
    that offset, and a UTF-8-with-replacement view of the surrounding
    text. The full body is also written so the maintainer can replay
    the exact request — VK occasionally returns garbled payloads for
    specific message ids and the only way to repro is to send the
    same params again.
    """
    start, end = err.start, err.end
    window = 80
    around_lo = max(0, start - window)
    around_hi = min(len(raw), end + window)
    hex_window = raw[around_lo:around_hi].hex(" ")
    text_window = raw[around_lo:around_hi].decode("utf-8", errors="replace")
    safe_body = _redact_body(body)
    body_preview = json.dumps(safe_body, ensure_ascii=False)
    if len(body_preview) > 2000:
        body_preview = body_preview[:2000] + f"…[+{len(body_preview)-2000}ch]"
    logger.error(
        "VK API response decode failed | method={} | content-type={!r}"
        " | total_bytes={} | bad_byte_offset={} | reason={}\n"
        "request body (token redacted): {}\n"
        "bytes around offset [{}:{}] (hex): {}\n"
        "bytes around offset (utf-8 w/ replacement): {!r}",
        method, content_type, len(raw), start, err.reason,
        body_preview, around_lo, around_hi, hex_window, text_window,
    )


def _dump_json_failure(
    method: str, body: dict[str, Any], text: str, err: json.JSONDecodeError,
) -> None:
    safe_body = _redact_body(body)
    body_preview = json.dumps(safe_body, ensure_ascii=False)
    if len(body_preview) > 2000:
        body_preview = body_preview[:2000] + f"…[+{len(body_preview)-2000}ch]"
    window = 200
    lo = max(0, err.pos - window)
    hi = min(len(text), err.pos + window)
    logger.error(
        "VK API JSON parse failed | method={} | total_chars={}"
        " | offset={} (line {} col {}) | reason={}\n"
        "request body (token redacted): {}\n"
        "text around offset [{}:{}]: {!r}",
        method, len(text), err.pos, err.lineno, err.colno, err.msg,
        body_preview, lo, hi, text[lo:hi],
    )


