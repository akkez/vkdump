from PySide6.QtWidgets import QMainWindow, QTabWidget

from ..tasks.registry import TASKS
from .results_dialog import StatsView
from .task_panel import TaskPanel


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("vkdump")
        self.resize(900, 640)

        # One tab per registered task, with `stats` rendered as the
        # embedded `StatsView` (instead of a form-driven task panel) so
        # clicking the tab is the "view results" experience. The Runs
        # history panel is hidden for now — wire it back when the user
        # asks.
        self._tabs = QTabWidget()
        for task in TASKS:
            if task.name == "stats":
                self._tabs.addTab(StatsView(), "Stats")
            else:
                self._tabs.addTab(TaskPanel(task), task.title)

        self.setCentralWidget(self._tabs)
