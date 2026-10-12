"""Modal dialog for pasting a VK API access token (or the full OAuth
redirect URL) and validating it before persisting to the accounts table.
"""
from __future__ import annotations

import asyncio
from html import escape

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QMessageBox,
    QVBoxLayout,
    QWidget,
)

from ..core.vk_api import VKApiError
from ..core.vk_auth import _AUTHORIZE_URL, _parse_token_input, _validate, persist


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


class AuthDialog(QDialog):
    """Modal: paste OAuth URL or bare token, validate, upsert account."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Authorize VK account")
        self.setModal(True)
        self.setMinimumWidth(560)
        self.setStyleSheet('''
            QDialog { background: #2b2b2b; }
            QLabel { color: #ffffff; background: transparent; }
        ''')
        self._pending_token: str | None = None

        instructions = QLabel(
            "<ol>"
            f"<li>Open <a style='color:#8ab4f8' href='{_AUTHORIZE_URL}'>{escape(_AUTHORIZE_URL)}</a></li>"
            "<li>Authorize and grant access</li>"
            "<li>Paste the full resulting URL (or just the <code>access_token</code> value) below, then click OK</li>"
            "</ol>"
        )
        instructions.setOpenExternalLinks(True)
        instructions.setWordWrap(True)
        instructions.setTextInteractionFlags(
            Qt.TextInteractionFlag.LinksAccessibleByMouse
            | Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )

        self._input = QLineEdit()
        self._input.setPlaceholderText(
            "https://oauth.vk.com/blank.html#access_token=...   or   vk1.a...."
        )

        self._status = QLabel("")
        self._status.setStyleSheet("color:#dddddd")
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
            persist(self._pending_token or "", data)
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

    def _set_busy(self, busy: bool) -> None:
        ok_btn = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        if ok_btn is not None:
            ok_btn.setEnabled(not busy)
        self._input.setReadOnly(busy)
