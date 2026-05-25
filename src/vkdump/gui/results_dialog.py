"""Results dialog shown after a task completes.

Reads its three tables from a pre-computed payload (the heavy SQL
rollups run in a worker, see `task_panel._ResultsLoader`) so the UI
thread never blocks on the dialog opening.
"""
from __future__ import annotations

from typing import Any, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QLabel,
    QTabWidget,
    QTableView,
    QVBoxLayout,
    QWidget,
)


class _DictListModel(QAbstractTableModel):
    """Read-only model over `list[dict]` rows. `columns` is a list of
    `(key, header)` pairs."""

    def __init__(self, rows: Sequence[dict[str, Any]], columns: Sequence[tuple[str, str]]) -> None:
        super().__init__()
        self._rows = list(rows)
        self._columns = list(columns)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._columns)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or role not in (
            Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole,
        ):
            return None
        key, _ = self._columns[index.column()]
        value = self._rows[index.row()].get(key)
        if value is None:
            return ""
        return str(value)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            if 0 <= section < len(self._columns):
                return self._columns[section][1]
        else:
            return section + 1
        return None


def _make_table(rows: Sequence[dict[str, Any]], columns: Sequence[tuple[str, str]]) -> QTableView:
    table = QTableView()
    model = _DictListModel(rows, columns)
    table.setModel(model)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    table.setSortingEnabled(True)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    table.resizeColumnsToContents()
    return table


def _wrap_with_footer(table: QTableView, footer: str | None) -> QWidget:
    """Stack a table over an optional grey footer label (used to show
    the count of rows hidden by the message-count threshold).
    """
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.addWidget(table, 1)
    if footer:
        lbl = QLabel(footer)
        lbl.setStyleSheet("color: gray; padding: 4px 2px;")
        layout.addWidget(lbl)
    return holder


# Column layouts kept here so the dialog can stay dumb / mechanical.
_CHAT_COLS: list[tuple[str, str]] = [
    ("title", "Title"),
    ("peer_id", "peer_id"),
    ("type", "Type"),
    ("message_count", "Messages"),
    ("attachment_count", "Attachments"),
    ("photo_count", "Photos"),
    ("video_count", "Videos"),
    ("audio_count", "Audios"),
    ("file_count", "Files"),
    ("forward_count", "Forwards"),
    ("link_count", "Links"),
    ("sticker_count", "Stickers"),
    ("other_count", "Other"),
    ("last_message_at", "Last message"),
]

_USER_COLS: list[tuple[str, str]] = [
    ("display_name", "Name"),
    ("vk_id", "vk_id"),
    ("message_count", "Messages"),
    ("attachment_count", "Attachments"),
    ("photo_count", "Photos"),
    ("video_count", "Videos"),
    ("audio_count", "Audios"),
    ("file_count", "Files"),
    ("forward_count", "Forwards"),
    ("link_count", "Links"),
    ("sticker_count", "Stickers"),
    ("other_count", "Other"),
    ("profile_url", "Profile URL"),
]

_ATT_COLS: list[tuple[str, str]] = [
    ("kind", "Kind"),
    ("total", "Count"),
    ("first_seen_at", "First seen"),
    ("last_seen_at", "Last seen"),
    ("last_chat_title", "Last seen in chat"),
    ("last_chat_peer", "peer_id"),
]


class ResultsDialog(QDialog):
    """Tabbed read-only summary. Expects `data` from `_ResultsLoader`."""

    def __init__(self, parent, data: dict[str, Any]) -> None:
        super().__init__(parent)
        self.setWindowTitle("vkdump — results")
        self.resize(1200, 720)

        chats, chats_hidden = data["chats"]
        users, users_hidden = data["users"]
        attachments = data["attachments"]
        min_messages = data.get("min_messages", 10)

        tabs = QTabWidget()
        tabs.addTab(
            _wrap_with_footer(
                _make_table(chats, _CHAT_COLS),
                _hidden_footer(chats_hidden, "chat", min_messages),
            ),
            f"Chats ({len(chats)})",
        )
        tabs.addTab(
            _wrap_with_footer(
                _make_table(users, _USER_COLS),
                _hidden_footer(users_hidden, "user", min_messages),
            ),
            f"Users ({len(users)})",
        )
        tabs.addTab(
            _wrap_with_footer(_make_table(attachments, _ATT_COLS), None),
            f"Attachments ({len(attachments)})",
        )

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)


def _hidden_footer(hidden: int, noun: str, threshold: int) -> str | None:
    if not hidden:
        return None
    return (
        f"{hidden} {noun}{'s' if hidden != 1 else ''} hidden — "
        f"fewer than {threshold} messages."
    )
