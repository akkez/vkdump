from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtWidgets import QMainWindow, QMessageBox, QStackedWidget, QTabWidget, QWidget

from ..tasks.registry import TASKS
from .results_dialog import StatsView
from .settings_view import SettingsView
from .start_view import StartView, WIZARD_WIDTH, WIZARD_HEIGHT, _app_icon
from .task_panel import TaskPanel
from .xp_widgets import XPExitDialog, xp_window_region


class MainWindow(QMainWindow):
    # Broadcast by the main window when something happens that may have
    # changed the rows other panels' `choices_provider`s read — either a
    # task finished with `mutates_data=True`, or the user picked a new
    # active account in Settings. Subscribers repopulate their combos.
    data_changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("VK Dump")
        self.resize(900, 640)
        self._workspace_flags = self.windowFlags()
        self._workspace_geometry: QRect | None = None
        self._wizard_mode = False
        self._close_confirmed = False

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
                # Stats rollups re-run on data_changed so account
                # switches / new imports show fresh numbers.
                self.data_changed.connect(self._stats_view.refresh)
                self._tabs.addTab(self._stats_view, "Stats")
            else:
                panel = TaskPanel(task)
                # Fan in each panel's per-run mutation signal into the
                # window-level bus.
                panel.data_mutated.connect(self.data_changed)
                # Each panel listens to the bus so its dropdowns re-pull
                # whenever any task finishes mutating DB rows or the
                # user picks a new active account.
                self.data_changed.connect(panel.refresh_choice_inputs)
                self._task_panels.append(panel)
                self._tabs.addTab(panel, task.title)
        # GUI-only Settings tab: not backed by a TaskSpec, lives at
        # the very end. Hosts the active-account picker; switching it
        # narrows chat dropdowns elsewhere via the same bus.
        self._settings_view = SettingsView()
        self._settings_view.active_account_changed.connect(self.data_changed)
        self.data_changed.connect(self._settings_view.refresh)
        self._tabs.addTab(self._settings_view, "Settings")

        self._download_panel = next(p for p in self._task_panels if p._task.name == "download-messages")
        self._import_panel = next(p for p in self._task_panels if p._task.name == "parse-dump")
        self._start_view = StartView(self._download_panel)
        self._start_view.import_requested.connect(lambda: self._tabs.setCurrentWidget(self._import_panel))
        self._start_view.download_requested.connect(self._start_download)
        self._start_view.exit_requested.connect(self.close)
        self._start_view.minimize_requested.connect(self.showMinimized)
        self._start_view.account_changed.connect(self.data_changed)
        self.data_changed.connect(self._start_view.refresh)
        self._start_entry = QWidget()
        self._tabs.insertTab(0, self._start_entry, "Start")
        self._tabs.setCurrentIndex(0)

        # Lazy-start the rollups: don't touch the DB until the user
        # actually opens the Stats tab. Free side-effect — if they
        # never open it, app shutdown is instant (no QThreadPool drain).
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self._screens = QStackedWidget()
        self._screens.addWidget(self._start_view)
        self._screens.addWidget(self._tabs)
        self.setCentralWidget(self._screens)
        self._show_wizard()

    def _on_tab_changed(self, index: int) -> None:
        if self._tabs.widget(index) is self._start_entry:
            self._show_wizard()
        else:
            self._show_workspace()
        if self._stats_view is not None and self._tabs.widget(index) is self._stats_view:
            self._stats_view.ensure_started()

    def _show_wizard(self) -> None:
        if self._wizard_mode:
            return
        visible = self.isVisible()
        center = self.frameGeometry().center()
        if visible:
            self._workspace_geometry = self.geometry()
        self._wizard_mode = True
        self._screens.setCurrentWidget(self._start_view)
        self._start_view.reset()
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowMinimizeButtonHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowTitle('VK Dump Wizard')
        self.setFixedSize(WIZARD_WIDTH, WIZARD_HEIGHT)
        region = xp_window_region(self.rect())
        self.setMask(region)
        self._start_view.setMask(region)
        self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)
        if visible:
            self.show()

    def _show_workspace(self) -> None:
        if not self._wizard_mode:
            return
        visible = self.isVisible()
        center = self.frameGeometry().center()
        self._wizard_mode = False
        self._screens.setCurrentWidget(self._tabs)
        self.clearMask()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.setWindowFlags(self._workspace_flags)
        self.setWindowTitle('VK Dump')
        self.setMinimumSize(0, 0)
        self.setMaximumSize(16777215, 16777215)
        if self._workspace_geometry is not None:
            self.setGeometry(self._workspace_geometry)
        else:
            self.resize(900, 640)
            self.move(center.x() - self.width() // 2, center.y() - self.height() // 2)
        if visible:
            self.show()

    def _start_download(self) -> None:
        self._tabs.setCurrentWidget(self._download_panel)
        if not self._download_panel.has_active_run():
            self._download_panel._on_run()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._wizard_mode and not self._close_confirmed:
            if not XPExitDialog.confirm(self, _app_icon()):
                event.ignore()
                return
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
        """Interrupt the Stats tab's SQLite rollups and any in-flight
        per-panel error polls so multi-hundred-ms SELECTs don't keep
        churning in the worker pool after the window is gone.
        """
        if self._stats_view is not None:
            self._stats_view.cancel_all()
        for p in self._task_panels:
            p.cancel_background_queries()
