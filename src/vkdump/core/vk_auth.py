from __future__ import annotations

import re
import secrets
from typing import Any
from urllib.parse import parse_qs

from . import app_config
from .db import connection
from .vk_api import VKApi, VKApiError


_ACTIVE_KEY = "active_account_id"
_AUTHORIZE_URL = (
    "https://oauth.vk.com/authorize?client_id=6463690&scope=215989462"
    "&redirect_uri=https://oauth.vk.com/blank.html&display=page&response_type=token&revoke=1"
)
_TOKEN_RE = re.compile(r"access_token=([A-Za-z0-9._\-]+)")
_BARE_RE = re.compile(r"^[A-Za-z0-9._\-]{20,}$")
# VK's error_msg for an unknown method points the developer at the
# editapp URL with the calling app's id baked in — we exploit that to
# recover vk_token_app_id without having to hard-code per-client values.
_EDITAPP_ID_RE = re.compile(r"editapp\?[^\s\"']*id=(\d+)")


def _parse_token_input(text: str) -> str | None:
    text = (text or "").strip()
    if not text:
        return None
    if "access_token=" in text:
        if "#" in text:
            frag = text.split("#", 1)[1]
            tok = parse_qs(frag).get("access_token", [None])[0]
            if tok:
                return tok
        m = _TOKEN_RE.search(text)
        if m:
            return m.group(1)
        return None
    if _BARE_RE.match(text):
        return text
    return None


async def _validate(token: str) -> dict[str, Any]:
    async with VKApi(token) as vk:
        scope = await vk.account_get_app_permissions()
        users = await vk.users_get(
            fields=["first_name", "last_name", "screen_name", "photo_100"]
        )
        if not users:
            raise VKApiError(None, "users.get returned empty list")
        me = users[0]
        avatar_url = (me.get("photo_100") or "").strip() or None
        avatar_bytes: bytes | None = None
        avatar_ct: str | None = None
        if avatar_url:
            try:
                avatar_bytes, avatar_ct = await vk.fetch_bytes(avatar_url)
            except Exception:
                avatar_bytes = None
                avatar_ct = None
        app_id = await _probe_app_id(vk)
    return {
        "vk_id": int(me["id"]),
        "display_name": _label(me),
        "scope": int(scope),
        "app_id": app_id,
        "avatar_url": avatar_url,
        "avatar_binary": avatar_bytes,
        "avatar_content_type": avatar_ct,
    }


async def _probe_app_id(vk: VKApi) -> int | None:
    """Call a guaranteed-missing ``execute.*`` method and parse the
    calling app's id out of VK's "see editapp?id=..." error pointer.
    Returns None on any deviation from the expected error shape so the
    upsert never blocks on this opportunistic lookup."""
    try:
        payload = await vk.call_raw(f"execute.ololo{secrets.randbelow(10**9)}")
    except VKApiError:
        return None
    msg = ((payload.get("error") or {}).get("error_msg")) or ""
    m = _EDITAPP_ID_RE.search(msg)
    return int(m.group(1)) if m else None


def _label(u: dict) -> str:
    fn = (u.get("first_name") or "").strip()
    ln = (u.get("last_name") or "").strip()
    name = f"{fn} {ln}".strip()
    return name or (u.get("screen_name") or "").strip()


def persist(token: str, info: dict[str, Any]) -> None:
    vk_id = int(info["vk_id"])
    name = info["display_name"]
    scope = int(info["scope"])
    app_id = info.get("app_id")
    avatar_url = info.get("avatar_url")
    avatar_binary = info.get("avatar_binary")
    avatar_ct = info.get("avatar_content_type")

    with connection() as conn:
        existing = conn.execute(
            "SELECT id FROM accounts WHERE provider='vk' AND vk_id=?", (vk_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO accounts (provider, vk_id, display_name, avatar_url,"
                " avatar_binary, avatar_content_type, vk_access_token,"
                " vk_token_app_id, vk_token_app_scope)"
                " VALUES ('vk', ?, ?, ?, ?, ?, ?, ?, ?)",
                (vk_id, name, avatar_url, avatar_binary, avatar_ct,
                 token, app_id, scope),
            )
        else:
            if avatar_binary is not None:
                conn.execute(
                    "UPDATE accounts SET"
                    " display_name=COALESCE(NULLIF(?,''), display_name),"
                    " avatar_url=COALESCE(?, avatar_url),"
                    " avatar_binary=?, avatar_content_type=?,"
                    " vk_access_token=?, vk_token_app_id=COALESCE(?, vk_token_app_id),"
                    " vk_token_app_scope=?"
                    " WHERE id=?",
                    (name, avatar_url, avatar_binary, avatar_ct,
                     token, app_id, scope, existing["id"]),
                )
            else:
                conn.execute(
                    "UPDATE accounts SET"
                    " display_name=COALESCE(NULLIF(?,''), display_name),"
                    " avatar_url=COALESCE(?, avatar_url),"
                    " vk_access_token=?, vk_token_app_id=COALESCE(?, vk_token_app_id),"
                    " vk_token_app_scope=?"
                    " WHERE id=?",
                    (name, avatar_url, token, app_id, scope, existing["id"]),
                )
    app_config.set(_ACTIVE_KEY, str(vk_id))

