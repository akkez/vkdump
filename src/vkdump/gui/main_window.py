from PySide6.QtWidgets import QMainWindow, QTabWidget

from .runs_panel import RunsPanel
from .task_panel import TaskPanel


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("vkdump")
        self.resize(900, 640)

        self._tabs = QTabWidget()
        self._task_panel = TaskPanel()
        self._runs_panel = RunsPanel()
        self._tabs.addTab(self._task_panel, "Tasks")
        self._tabs.addTab(self._runs_panel, "Runs")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self.setCentralWidget(self._tabs)

    def _on_tab_changed(self, index: int) -> None:
        if self._tabs.widget(index) is self._runs_panel:
            self._runs_panel.refresh()
