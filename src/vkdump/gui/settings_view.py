"""Settings tab: just the active-account picker for now.

The picker enumerates every distinct `chats.account_id` in the DB
(joined to `users` for the display name) and persists the user's
choice to `app_config['active_account_id']`. Wiring this value into
stats / chat pickers / save-chat scoping is intentionally out of
scope for this pass — a future change will read the key from the
config and filter task choices_providers / stats rollups accordingly.

Account rows whose `users.display_name` is missing surface that fact
in the label so the user knows to backfill it (parse-dump owns user
records — this view doesn't write them).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QFormLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..core import app_config
from ..core.db import connection
from .task_panel import _attach_searchable_combo


_ACTIVE_KEY = "active_account_id"


def _account_choices() -> list[tuple[str, str]]:
    """Return (account_id_as_string, label) pairs, busiest account first.

    Label format: 'Имя Фамилия (id12345)' when the account's user row
    has a display_name; '(id12345) — no name in DB' otherwise so the
    user spots accounts they should backfill.
    """
    sentinel: list[tuple[str, str]] = [("", "— none —")]
    try:
        with connection() as conn:
            rows = conn.execute(
                """
                SELECT c.account_id AS acct,
                       u.display_name AS name,
                       SUM(c.message_count) AS total_msgs
                  FROM chats c
                  LEFT JOIN users u ON u.vk_id = c.account_id
                                   AND u.provider = c.provider
                 WHERE c.account_id IS NOT NULL AND c.account_id != ''
                 GROUP BY c.account_id
                 ORDER BY total_msgs DESC, c.account_id
                """
            ).fetchall()
    except Exception:
        return sentinel
    out: list[tuple[str, str]] = list(sentinel)
    for r in rows:
        acct = str(r["acct"])
        name = (r["name"] or "").strip()
        label = (
            f"{name} (id{acct})" if name
            else f"(id{acct}) — no name in DB"
        )
        out.append((acct, label))
    return out


class SettingsView(QWidget):
    """Settings tab body. Lazy DB read at construction time; the user
    presumably visits this tab rarely enough that we don't need a
    refresh button yet (will add when accounts start being added on
    the fly)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._account_cb = QComboBox()
        self._account_cb.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._account_cb.setMinimumWidth(420)

        opts = _account_choices()
        for value, label in opts:
            self._account_cb.addItem(label, value)
        # Pre-select whatever was active last time, if it's still
        # present in the list.
        saved = app_config.get(_ACTIVE_KEY)
        if saved:
            idx = self._account_cb.findData(saved)
            if idx >= 0:
                self._account_cb.setCurrentIndex(idx)
        if len(opts) > 10:
            _attach_searchable_combo(self._account_cb)
        self._account_cb.currentIndexChanged.connect(self._on_changed)

        self._hint = QLabel(
            "Selecting an account here just persists the choice. Filtering"
            " stats / pickers / save-chat by it is a separate step still"
            " to come."
        )
        self._hint.setStyleSheet("color:#888; padding:4px 0")
        self._hint.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Active account:", self._account_cb)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.addLayout(form)
        layout.addWidget(self._hint)
        layout.addStretch(1)

    def _on_changed(self, _idx: int) -> None:
        value = self._account_cb.currentData() or ""
        try:
            app_config.set(_ACTIVE_KEY, value or None)
        except Exception:  # noqa: BLE001
            # Don't blow up the GUI if the DB is wedged — the choice
            # just won't survive the next launch.
            pass
