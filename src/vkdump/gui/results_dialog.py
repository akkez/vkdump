"""Results dialog shown after a task completes.

Opens immediately with three empty tabs (column headers visible); each
tab loads its rollup in a background `QThreadPool` runnable so the GUI
thread never blocks. Tab titles show `(loading…)` until data arrives,
then switch to the row count. Loaders for all three tabs are kicked off
in parallel on construction, so switching tabs never waits.
"""
from __future__ import annotations

from typing import Any, Callable, Sequence

from PySide6.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    Signal,
    Slot,
)
from PySide6.QtWidgets import (
    QDialog,
    QHeaderView,
    QLabel,
    QProgressBar,
    QStackedWidget,
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


def _make_empty_table(columns: Sequence[tuple[str, str]]) -> QTableView:
    """Table with just the column headers visible — no rows yet."""
    table = QTableView()
    table.setModel(_DictListModel([], columns))
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
    table.setSortingEnabled(True)
    header = table.horizontalHeader()
    header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
    header.setStretchLastSection(True)
    return table


def _loading_overlay() -> QWidget:
    """A centred "Loading…" label over an indeterminate progress bar."""
    holder = QWidget()
    layout = QVBoxLayout(holder)
    layout.addStretch(1)

    label = QLabel("Loading…")
    label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    label.setStyleSheet("color: gray; font-size: 14px;")
    layout.addWidget(label)

    bar = QProgressBar()
    bar.setRange(0, 0)  # indeterminate / busy marquee
    bar.setTextVisible(False)
    bar.setFixedWidth(220)
    layout.addWidget(bar, 0, Qt.AlignmentFlag.AlignHCenter)

    layout.addStretch(1)
    return holder


class _RollupSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Rollup(QRunnable):
    """Run a single rollup callable off the GUI thread."""

    def __init__(self, fn: Callable[[], Any]) -> None:
        super().__init__()
        self._fn = fn
        self.signals = _RollupSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self._fn()
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.signals.done.emit(result)


class _Tab(QWidget):
    """A tab that swaps between a loading overlay, the populated table
    (+ optional grey footer), or an error message."""

    def __init__(self, columns: Sequence[tuple[str, str]]) -> None:
        super().__init__()
        self._columns = columns

        self._stack = QStackedWidget()
        self._loading = _loading_overlay()
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._table = _make_empty_table(columns)
        self._content_layout.addWidget(self._table, 1)
        self._footer: QLabel | None = None
        self._error = QLabel("")
        self._error.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._error.setStyleSheet("color: #d63a3a; padding: 24px;")
        self._error.setWordWrap(True)

        self._stack.addWidget(self._loading)
        self._stack.addWidget(self._content)
        self._stack.addWidget(self._error)
        self._stack.setCurrentWidget(self._loading)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._stack)

    def populate(self, rows: Sequence[dict[str, Any]], footer: str | None) -> None:
        self._table.setModel(_DictListModel(rows, self._columns))
        self._table.resizeColumnsToContents()
        if footer:
            if self._footer is None:
                self._footer = QLabel(footer)
                self._footer.setStyleSheet("color: gray; padding: 4px 2px;")
                self._content_layout.addWidget(self._footer)
            else:
                self._footer.setText(footer)
        elif self._footer is not None:
            self._content_layout.removeWidget(self._footer)
            self._footer.deleteLater()
            self._footer = None
        self._stack.setCurrentWidget(self._content)

    def show_error(self, message: str) -> None:
        self._error.setText(message)
        self._stack.setCurrentWidget(self._error)


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
    """Tabbed read-only summary. Opens instantly with empty tabs, then
    fills them as background rollups complete."""

    def __init__(self, parent, min_messages: int = 10) -> None:
        super().__init__(parent)
        self.setWindowTitle("vkdump — results")
        self.resize(1200, 720)

        self._min_messages = min_messages
        self._pool = QThreadPool.globalInstance()
        # Hold references to in-flight runnables: the pool owns them on
        # the C++ side, but Python may GC the wrappers (and their
        # `_RollupSignals` QObject) before `run()` reaches `emit`,
        # producing "Signal source has been deleted".
        self._runnables: list[_Rollup] = []

        self._tabs = QTabWidget()
        self._chats_tab = _Tab(_CHAT_COLS)
        self._users_tab = _Tab(_USER_COLS)
        self._att_tab = _Tab(_ATT_COLS)
        self._chats_idx = self._tabs.addTab(self._chats_tab, "Chats (loading…)")
        self._users_idx = self._tabs.addTab(self._users_tab, "Users (loading…)")
        self._att_idx = self._tabs.addTab(self._att_tab, "Attachments (loading…)")

        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs)

        self._start_loaders()

    def _start_loaders(self) -> None:
        # Imported lazily so test-time import of this module doesn't drag
        # in sqlite/connection plumbing.
        from ..core.stats_views import (
            attachments_rollup, chats_rollup, users_rollup,
        )
        mm = self._min_messages

        chats = _Rollup(lambda: chats_rollup(min_messages=mm))
        chats.signals.done.connect(self._on_chats_done)
        chats.signals.failed.connect(self._on_chats_failed)

        users = _Rollup(lambda: users_rollup(min_messages=mm))
        users.signals.done.connect(self._on_users_done)
        users.signals.failed.connect(self._on_users_failed)

        att = _Rollup(attachments_rollup)
        att.signals.done.connect(self._on_att_done)
        att.signals.failed.connect(self._on_att_failed)

        # SQLite in WAL mode tolerates concurrent readers across separate
        # connections, and `connection()` opens a fresh one per call —
        # safe to run all three in parallel.
        self._runnables.extend([chats, users, att])
        self._pool.start(chats)
        self._pool.start(users)
        self._pool.start(att)

    def _on_chats_done(self, payload: object) -> None:
        rows, hidden = payload  # type: ignore[misc]
        self._chats_tab.populate(rows, _hidden_footer(hidden, "chat", self._min_messages))
        self._tabs.setTabText(self._chats_idx, f"Chats ({len(rows)})")

    def _on_users_done(self, payload: object) -> None:
        rows, hidden = payload  # type: ignore[misc]
        self._users_tab.populate(rows, _hidden_footer(hidden, "user", self._min_messages))
        self._tabs.setTabText(self._users_idx, f"Users ({len(rows)})")

    def _on_att_done(self, payload: object) -> None:
        rows = payload  # type: ignore[assignment]
        self._att_tab.populate(rows, None)
        self._tabs.setTabText(self._att_idx, f"Attachments ({len(rows)})")

    def _on_chats_failed(self, error: str) -> None:
        self._chats_tab.show_error(error)
        self._tabs.setTabText(self._chats_idx, "Chats (error)")

    def _on_users_failed(self, error: str) -> None:
        self._users_tab.show_error(error)
        self._tabs.setTabText(self._users_idx, "Users (error)")

    def _on_att_failed(self, error: str) -> None:
        self._att_tab.show_error(error)
        self._tabs.setTabText(self._att_idx, "Attachments (error)")


def _hidden_footer(hidden: int, noun: str, threshold: int) -> str | None:
    if not hidden:
        return None
    return (
        f"{hidden} {noun}{'s' if hidden != 1 else ''} hidden — "
        f"fewer than {threshold} messages."
    )
