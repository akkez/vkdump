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
        for task in TASKS:
            if task.name == "stats":
                self._tabs.addTab(StatsView(), "Stats")
            else:
                panel = TaskPanel(task)
                self._task_panels.append(panel)
                self._tabs.addTab(panel, task.title)

        self.setCentralWidget(self._tabs)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        active = [p for p in self._task_panels if p.has_active_run()]
        if not active:
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
            event.accept()
        else:
            event.ignore()
