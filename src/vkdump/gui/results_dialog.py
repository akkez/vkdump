"""Results dialog shown after a task completes — tabbed read-only
view over the rollups in `core.stats_views`.

Three tabs: chats, users, attachments. Each is a QTableView driven by
a tiny dict-list model so the SQL layer can stay schema-agnostic.
"""
from __future__ import annotations

from typing import Any, Sequence

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QTabWidget,
    QTableView,
    QVBoxLayout,
)

from ..core.stats_views import attachments_rollup, chats_rollup, users_rollup


class _DictListModel(QAbstractTableModel):
    """Read-only model over `list[dict]` rows.

    `columns` is a list of `(key, header)` pairs deciding which fields
    show up and in what order.
    """

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


class ResultsDialog(QDialog):
    """Read-only summary window: three tabs over the SQLite store."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("vkdump — results")
        self.resize(1100, 700)

        tabs = QTabWidget()

        # Chats tab.
        chat_cols = [
            ("title", "Title"),
            ("peer_id", "peer_id"),
            ("source_folder", "Folder"),
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
        tabs.addTab(_make_table(chats_rollup(), chat_cols), "Chats")

        # Users tab.
        user_cols = [
            ("display_name", "Name"),
            ("vk_id", "vk_id"),
            ("is_deleted", "Deleted"),
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
        tabs.addTab(_make_table(users_rollup(), user_cols), "Users")

        # Attachments tab.
        att_cols = [
            ("kind", "Kind"),
            ("total", "Count"),
            ("first_seen_at", "First seen"),
            ("last_seen_at", "Last seen"),
        ]
        tabs.addTab(_make_table(attachments_rollup(), att_cols), "Attachments")

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
