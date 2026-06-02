"""Modal dialog for pasting a VK API access token (or the full vkhost
redirect URL) and validating it before persisting to the accounts table.
"""
from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any
from urllib.parse import parse_qs

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ..core import app_config
from ..core.db import connection
from ..core.vk_api import VKApi, VKApiError


_ACTIVE_KEY = "active_account_id"
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


class _CheckSignals(QObject):
    finished = Signal(object)


class _CheckRunnable(QRunnable):
    def __init__(self, token: str, signals: _CheckSignals) -> None:
        super().__init__()
        self._token = token
        self._signals = signals

    def run(self) -> None:
        try:
            data = asyncio.run(_validate(self._token))
            self._signals.finished.emit((True, data))
        except VKApiError as e:
            self._signals.finished.emit((False, str(e)))
        except Exception as e:  # noqa: BLE001
            self._signals.finished.emit((False, f"{type(e).__name__}: {e}"))


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


class AuthDialog(QDialog):
    """Modal: paste vkhost URL or bare token, validate, upsert account."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Authorize VK account")
        self.setModal(True)
        self.setMinimumWidth(560)
        self._pending_token: str | None = None

        instructions = QLabel(
            "<ol>"
            "<li>Open <a href='https://vkhost.github.io/'>https://vkhost.github.io/</a></li>"
            "<li>Click <b>Kate Mobile</b></li>"
            "<li>Grant access</li>"
            "<li>Paste the full resulting URL (or just the <code>access_token</code> value) below and press OK</li>"
            "</ol>"
        )
        instructions.setOpenExternalLinks(True)
        instructions.setWordWrap(True)
        instructions.setTextInteractionFlags(
            instructions.textInteractionFlags()
            | instructions.textInteractionFlags().__class__.TextSelectableByMouse
        )

        self._input = QLineEdit()
        self._input.setPlaceholderText(
            "https://oauth.vk.com/blank.html#access_token=...   or   vk1.a...."
        )

        self._status = QLabel("")
        self._status.setStyleSheet("color:#888")
        self._status.setWordWrap(True)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self._buttons.accepted.connect(self._on_ok)
        self._buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(instructions)
        lay.addWidget(self._input)
        lay.addWidget(self._status)
        lay.addWidget(self._buttons)

        self._signals = _CheckSignals()
        self._signals.finished.connect(self._on_check_done)

    def _on_ok(self) -> None:
        token = _parse_token_input(self._input.text())
        if not token:
            QMessageBox.warning(
                self,
                "Invalid input",
                "Couldn't find an access_token. Paste either the full redirect URL or the bare token.",
            )
            return
        self._pending_token = token
        self._set_busy(True)
        self._status.setText("Checking token with api.vk.com…")
        QThreadPool.globalInstance().start(_CheckRunnable(token, self._signals))

    def _on_check_done(self, payload: tuple) -> None:
        ok, data = payload
        self._set_busy(False)
        if not ok:
            self._status.setText(f"Failed: {data}")
            QMessageBox.critical(self, "Token rejected", str(data))
            return
        try:
            self._persist(self._pending_token or "", data)
        except Exception as e:  # noqa: BLE001
            self._status.setText(f"DB write failed: {e}")
            QMessageBox.critical(self, "DB error", str(e))
            return
        QMessageBox.information(
            self,
            "Authorized",
            f"Token saved for {data['display_name']} (id{data['vk_id']}).",
        )
        self.accept()

    def _persist(self, token: str, info: dict[str, Any]) -> None:
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

    def _set_busy(self, busy: bool) -> None:
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setEnabled(not busy)
        self._input.setReadOnly(busy)
