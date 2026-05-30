from PySide6.QtWidgets import QMainWindow, QMessageBox, QTabWidget

from ..tasks.registry import TASKS
from .results_dialog import StatsView
from .task_panel import TaskPanel


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VKdump")
        self.resize(900, 640)

        # One tab per registered task, with `stats` rendered as the
        # embedded `StatsView` (instead of a form-driven task panel) so
        # clicking the tab is the "view results" experience. The Runs
        # history panel is hidden for now — wire it back when the user
        # asks.
        self._tabs = QTabWidget()
        self._task_panels: list[TaskPanel] = []
        self._stats_view: StatsView | None = None
        for task in TASKS:
            if task.name == "stats":
                self._stats_view = StatsView()
                self._tabs.addTab(self._stats_view, "Stats")
            else:
                panel = TaskPanel(task)
                self._task_panels.append(panel)
                self._tabs.addTab(panel, task.title)

        # Lazy-start the rollups: don't touch the DB until the user
        # actually opens the Stats tab. Free side-effect — if they
        # never open it, app shutdown is instant (no QThreadPool drain).
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self.setCentralWidget(self._tabs)

    def _on_tab_changed(self, index: int) -> None:
        if self._stats_view is not None and self._tabs.widget(index) is self._stats_view:
            self._stats_view.ensure_started()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        active = [p for p in self._task_panels if p.has_active_run()]
        if not active:
            self._cancel_background_queries()
            event.accept()
            return
        names = ", ".join(p._task.title for p in active)
        reply = QMessageBox.question(
            self,
            "Task running",
            (
                f"A task is still running ({names}). "
                f"Quit anyway? Any in-flight work will be abandoned."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self._cancel_background_queries()
            event.accept()
        else:
            event.ignore()

    def _cancel_background_queries(self) -> None:
        """Interrupt the Stats tab's SQLite rollups so multi-second
        aggregations don't keep churning in the worker pool after the
        window is gone.
        """
        if self._stats_view is not None:
            self._stats_view.cancel_all()
