from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.runs import list_runs


COLUMNS = ("id", "task", "status", "started_at", "finished_at", "error")


# GUI-only renames for task ids stored under their legacy names in the
# DB / CLI. Keeps history rows readable without a migration.
_TASK_DISPLAY = {
    "enrich-media": "download-media",
}


class RunsPanel(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._table = QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)

        top = QHBoxLayout()
        top.addWidget(self._refresh_btn)
        top.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addLayout(top)
        layout.addWidget(self._table, 1)

    def refresh(self) -> None:
        rows = list_runs(limit=100)
        self._table.setRowCount(len(rows))
        for r, run in enumerate(rows):
            values = (
                str(run.id),
                _TASK_DISPLAY.get(run.task, run.task),
                run.status,
                run.started_at.isoformat(timespec="seconds"),
                run.finished_at.isoformat(timespec="seconds") if run.finished_at else "",
                (run.error or "").splitlines()[0][:120] if run.error else "",
            )
            for c, v in enumerate(values):
                self._table.setItem(r, c, QTableWidgetItem(v))
        self._table.resizeColumnsToContents()
