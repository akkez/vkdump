"""Async client for the VK API.

One client owns one aiohttp.ClientSession — open with
`async with VKApi(token) as vk: ...`. VK's "logical" errors
(invalid token, missing permission, ...) come back as
`{"error": {...}}` with HTTP 200; the client raises
:class:`VKApiError` for those just like for transport failures.
"""
from __future__ import annotations

import asyncio
import ssl
from typing import Any

import aiohttp
import certifi


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
        :class:`VKApiError`."""
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
                payload = await resp.json(content_type=None)
        except asyncio.TimeoutError as e:
            raise VKApiError(None, "request timed out") from e
        except aiohttp.ClientError as e:
            raise VKApiError(None, f"network error: {e}") from e
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
