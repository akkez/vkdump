"""Settings tab: active-account picker.

The picker enumerates every row in the ``accounts`` table and persists
the user's choice to ``app_config['active_account_id']``. The chat-scope
``choices_provider`` reads that key and filters chat lists by it, so
picking an account here narrows every chat dropdown in the GUI.

When the DB just gained its first account (typical post-initial-import
state), the picker auto-selects it instead of leaving the user on the
"— none —" sentinel.

The view is refreshed via ``refresh()`` whenever the main window
broadcasts ``data_changed`` (e.g. after a successful parse-dump).
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import app_config
from ..core.db import connection
from .auth_dialog import AuthDialog
from .task_panel import _attach_searchable_combo


_ACTIVE_KEY = "active_account_id"


def _account_choices() -> list[tuple[str, str]]:
    """Return ``(vk_id, label)`` for every row in ``accounts``, busiest
    first by total message count of that account's chats.

    The ``("", "— none —")`` sentinel is only included when there are
    no real account rows yet — once even one account exists, dropping
    it forces the user to a concrete pick (and prevents picking the
    empty-string value that disables the chat-scope filter).
    """
    sentinel: list[tuple[str, str]] = [("", "— none —")]
    try:
        with connection() as conn:
            rows = conn.execute(
                """
                SELECT a.vk_id AS vk_id,
                       a.display_name AS name,
                       COALESCE(SUM(c.message_count), 0) AS total_msgs
                  FROM accounts a
                  LEFT JOIN chats c
                    ON c.provider = a.provider
                   AND c.account_id = CAST(a.vk_id AS TEXT)
                 WHERE a.provider = 'vk'
                 GROUP BY a.id
                 ORDER BY total_msgs DESC, a.vk_id
                """
            ).fetchall()
    except Exception:
        return sentinel
    if not rows:
        return sentinel
    out: list[tuple[str, str]] = []
    for r in rows:
        vk_id = str(r["vk_id"])
        name = (r["name"] or "").strip()
        label = (
            f"{name} (id{vk_id})" if name
            else f"(id{vk_id}) — no name in DB"
        )
        out.append((vk_id, label))
    return out


class SettingsView(QWidget):
    """Settings tab body. The combo is repopulated on demand via
    ``refresh()`` so newly imported accounts surface without a restart.
    """

    # Emitted when the user changes the active-account pick; the main
    # window re-broadcasts this as `data_changed` so chat dropdowns in
    # every panel re-filter accordingly.
    active_account_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._account_cb = QComboBox()
        self._account_cb.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._account_cb.setMinimumWidth(420)
        self._searchable_attached = False
        self._account_cb.currentIndexChanged.connect(self._on_changed)

        self._auth_btn = QPushButton("Authorize")
        self._auth_btn.clicked.connect(self._on_auth_clicked)

        account_row = QHBoxLayout()
        account_row.setContentsMargins(0, 0, 0, 0)
        account_row.addWidget(self._account_cb, 1)
        account_row.addWidget(self._auth_btn, 0)
        account_row_w = QWidget()
        account_row_w.setLayout(account_row)

        self._hint = QLabel(
            "Picking an account here filters chat dropdowns in every"
            " other tab to that account's chats."
        )
        self._hint.setStyleSheet("color:#888; padding:4px 0")
        self._hint.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Active account:", account_row_w)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addLayout(form)
        layout.addWidget(self._hint)
        layout.addStretch(1)

        # Populate now and on every later data-changed nudge from the
        # main window.
        self._populate()

    def refresh(self) -> None:
        """Re-read the accounts list and repopulate the combo. Called by
        the main window after a task that mutates DB rows."""
        self._populate()

    def _populate(self) -> None:
        opts = _account_choices()
        saved = app_config.get(_ACTIVE_KEY) or ""
        # Auto-pick: if no active account is stored yet but accounts
        # exist, default to the first real one (skip the sentinel).
        real_options = [(v, l) for v, l in opts if v]
        if not saved and real_options:
            saved = real_options[0][0]
            try:
                app_config.set(_ACTIVE_KEY, saved)
            except Exception:  # noqa: BLE001
                pass

        # Suppress the signal so re-population doesn't fire _on_changed
        # with an intermediate value while we're rebuilding.
        self._account_cb.blockSignals(True)
        try:
            self._account_cb.clear()
            for value, label in opts:
                self._account_cb.addItem(label, value)
            idx = self._account_cb.findData(saved) if saved else 0
            self._account_cb.setCurrentIndex(idx if idx >= 0 else 0)
        finally:
            self._account_cb.blockSignals(False)

        if len(opts) > 10 and not self._searchable_attached:
            _attach_searchable_combo(self._account_cb)
            self._searchable_attached = True

        self._refresh_auth_button()

    def _refresh_auth_button(self) -> None:
        value = self._account_cb.currentData() or ""
        has_token = False
        if value:
            try:
                with connection() as conn:
                    row = conn.execute(
                        "SELECT vk_access_token FROM accounts"
                        " WHERE provider='vk' AND vk_id=?",
                        (value,),
                    ).fetchone()
                has_token = bool(row and row["vk_access_token"])
            except Exception:
                has_token = False
        self._auth_btn.setText("Re-authorize" if has_token else "Authorize")

    def _on_auth_clicked(self) -> None:
        dlg = AuthDialog(self)
        if dlg.exec() == AuthDialog.DialogCode.Accepted:
            self._populate()
            self.active_account_changed.emit()

    def _on_changed(self, _idx: int) -> None:
        value = self._account_cb.currentData() or ""
        self._refresh_auth_button()
        try:
            app_config.set(_ACTIVE_KEY, value or None)
        except Exception:  # noqa: BLE001
            # Don't blow up the GUI if the DB is wedged — the choice
            # just won't survive the next launch.
            pass
        self.active_account_changed.emit()
