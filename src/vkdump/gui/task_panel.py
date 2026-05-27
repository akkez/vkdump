from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QObject, QRunnable, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from rich.text import Text


class _ResultsLoaderSignals(QObject):
    done = Signal(dict)
    failed = Signal(str)


class _ResultsLoader(QRunnable):
    """Run the three SQL rollups off the GUI thread. The dialog is
    cheap to build once the data is in hand; the heavy step is the
    aggregation."""

    def __init__(self) -> None:
        super().__init__()
        self.signals = _ResultsLoaderSignals()

    @Slot()
    def run(self) -> None:
        try:
            from ..core.stats_views import (
                attachments_rollup, chats_rollup, users_rollup,
            )
            min_messages = 10
            data = {
                "chats": chats_rollup(min_messages=min_messages),
                "users": users_rollup(min_messages=min_messages),
                "attachments": attachments_rollup(),
                "min_messages": min_messages,
            }
            self.signals.done.emit(data)
        except Exception as exc:  # noqa: BLE001
            self.signals.failed.emit(f"{type(exc).__name__}: {exc}")


class _ClickyLineEdit(QLineEdit):
    """QLineEdit that fires `clicked_when_empty` on a click when the
    field is empty. Lets the user tap-to-browse without hunting for a
    button.
    """

    clicked_when_empty = Signal()

    def mousePressEvent(self, e) -> None:  # type: ignore[override]
        super().mousePressEvent(e)
        if not self.text().strip():
            self.clicked_when_empty.emit()

from ..core.db import connection
from ..tasks.registry import TASKS
from ..tasks.spec import ParamSpec, TaskSpec
from .results_dialog import ResultsDialog
from .widgets.aero_progress import AeroProgressGroup
from .workers import TaskWorker


def _strip_markup(s: str) -> str:
    """Drop Rich-style `[green]…[/green]` markup so it doesn't show up
    raw in Qt widgets. Rich's parser is a more robust stripper than a
    handrolled regex (handles nested / unmatched tags / escapes)."""
    if not s or "[" not in s:
        return s
    try:
        return Text.from_markup(s).plain
    except Exception:
        return s


class TaskPanel(QWidget):
    """Auto-generated UI for any TaskSpec from the registry."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._active_worker: TaskWorker | None = None
        self._editors: dict[str, QWidget] = {}
        # Original text of path inputs while a task is running; the
        # visible field gets collapsed to ".../<basename>" until the
        # run ends, then we restore from here.
        self._collapsed_paths: dict[str, str] = {}

        self._task_combo = QComboBox()
        for t in TASKS:
            self._task_combo.addItem(t.title, t.name)
        self._task_combo.currentIndexChanged.connect(self._rebuild_form)

        self._description = QLabel()
        self._description.setWordWrap(True)
        self._description.setStyleSheet("color: gray;")

        self._form_host = QStackedWidget()

        self._run_btn = QPushButton("Run")
        self._cancel_btn = QPushButton("Cancel")
        self._results_btn = QPushButton("View results…")
        self._cancel_btn.setEnabled(False)
        self._results_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._on_run)
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._results_btn.clicked.connect(self._on_view_results)

        self._progress = AeroProgressGroup()
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setFormat("%v / %m (%p%)")
        self._progress.setVisible(False)

        # Live error counter shown next to the progress bars while a
        # task is running. Hidden when zero. Polled from a QTimer
        # (cheap — two COUNT(*) queries) so modules don't have to wire
        # the count through their progress signals.
        self._errors_label = QLabel("")
        self._errors_label.setStyleSheet(
            "color: #d63a3a; font-weight: 600; padding: 2px 0;"
        )
        self._errors_label.setVisible(False)
        self._errors_timer = QTimer(self)
        self._errors_timer.setInterval(1000)
        self._errors_timer.timeout.connect(self._poll_errors)
        # SQLite-formatted UTC timestamp captured at Run start so the
        # poller can ignore failures from previous runs.
        self._run_started_at: str | None = None

        # Stack of sub-progress bars (one per active `sub()` scope), kept
        # in a dedicated container so the layout can grow/shrink as the
        # module opens and closes nested scopes.
        self._sub_host = QFrame()
        self._sub_layout = QVBoxLayout(self._sub_host)
        self._sub_layout.setContentsMargins(0, 0, 0, 0)
        self._sub_bars: dict[int, AeroProgressGroup] = {}

        self._status = QLabel("")
        self._log = QTextEdit()
        self._log.setReadOnly(True)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addWidget(self._results_btn)
        btn_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Task"))
        layout.addWidget(self._task_combo)
        layout.addWidget(self._description)
        layout.addWidget(self._form_host)
        layout.addLayout(btn_row)
        layout.addWidget(self._progress)
        layout.addWidget(self._sub_host)
        layout.addWidget(self._errors_label)
        layout.addWidget(self._status)
        layout.addWidget(self._log, 1)

        self._rebuild_form()

    def _current_task(self) -> TaskSpec:
        name = self._task_combo.currentData()
        return next(t for t in TASKS if t.name == name)

    def _rebuild_form(self, _index: int = 0) -> None:
        # `_index` is supplied by `QComboBox.currentIndexChanged(int)` —
        # accepting it explicitly so PySide6 doesn't silently swallow a
        # TypeError when invoking the slot, which would leave
        # `self._editors` populated from the previous task and trigger
        # a KeyError in `_collect_params` on Run.
        task = self._current_task()
        self._description.setText(task.description)
        self._description.setVisible(bool(task.description))
        self._editors.clear()

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        for p in task.params:
            editor = self._make_editor(p)
            self._editors[p.name] = editor
            form.addRow(p.label + ":", editor)

        while self._form_host.count():
            w = self._form_host.widget(0)
            self._form_host.removeWidget(w)
            w.deleteLater()
        self._form_host.addWidget(form_widget)

    def _make_editor(self, p: ParamSpec) -> QWidget:
        if p.type in ("dir", "path", "path_any"):
            row = QWidget()
            line = _ClickyLineEdit() if p.type == "path_any" else QLineEdit()
            line.setObjectName(f"{p.name}__edit")
            if p.default is not None:
                line.setText(str(p.default))
            line.setPlaceholderText(p.help or "")
            line.setMinimumWidth(420)
            line.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(line, 1)

            def pick_file() -> None:
                # `path_any` is meant for VK dumps — bias the filter
                # to ZIP archives but keep "All files" reachable.
                filt = (
                    "ZIP archive (*.zip);;All files (*)"
                    if p.type == "path_any" else "All files (*)"
                )
                path, _ = QFileDialog.getOpenFileName(self, f"Select {p.label}", "", filt)
                if path:
                    line.setText(path)

            def pick_dir() -> None:
                path = QFileDialog.getExistingDirectory(self, f"Select {p.label}")
                if path:
                    line.setText(path)

            if p.type == "dir":
                btn = QPushButton("Browse…")
                btn.clicked.connect(pick_dir)
                layout.addWidget(btn)
            elif p.type == "path":
                btn = QPushButton("Browse…")
                btn.clicked.connect(pick_file)
                layout.addWidget(btn)
            else:  # path_any → one button with a popup menu
                btn = QPushButton("Browse…")
                menu = QMenu(btn)
                file_action = QAction("Pick a ZIP archive…", menu)
                dir_action = QAction("Pick a folder…", menu)
                file_action.triggered.connect(pick_file)
                dir_action.triggered.connect(pick_dir)
                menu.addAction(file_action)
                menu.addAction(dir_action)
                btn.setMenu(menu)
                layout.addWidget(btn)

                # Click on an empty line → pop the same menu under it so
                # the user can pick without aiming at the button.
                if isinstance(line, _ClickyLineEdit):
                    def _show_menu() -> None:
                        menu.exec(line.mapToGlobal(line.rect().bottomLeft()))
                    line.clicked_when_empty.connect(_show_menu)
            return row
        if p.type == "int":
            sb = QSpinBox()
            sb.setMaximum(2_000_000_000)
            if p.default is not None:
                sb.setValue(int(p.default))
            return sb
        if p.type == "bool":
            cb = QCheckBox()
            cb.setChecked(bool(p.default))
            return cb
        line = QLineEdit()
        if p.default is not None:
            line.setText(str(p.default))
        line.setPlaceholderText(p.help or "")
        return line

    def _collect_params(self) -> dict[str, Any]:
        task = self._current_task()
        # If signal wiring failed to keep the form in sync with the
        # combo (the historic root cause of the "KeyError: 'kinds'"
        # bug), rebuild here so Run still works without a restart.
        expected = {p.name for p in task.params}
        if expected - self._editors.keys():
            self._rebuild_form()
        out: dict[str, Any] = {}
        for p in task.params:
            editor = self._editors[p.name]
            if p.type in ("dir", "path", "path_any"):
                line = editor.findChild(QLineEdit)
                text = line.text().strip() if line else ""
                if not text and p.required:
                    raise ValueError(f"{p.label} is required")
                out[p.name] = Path(text) if text else None
            elif p.type == "int":
                out[p.name] = editor.value()
            elif p.type == "bool":
                out[p.name] = editor.isChecked()
            else:
                text = editor.text().strip()
                if not text and p.required:
                    raise ValueError(f"{p.label} is required")
                out[p.name] = text
        return out

    def _freeze_path_inputs(self) -> None:
        """Collapse path-typed inputs to '.../<basename>' and make them
        read-only for the duration of a run."""
        self._collapsed_paths.clear()
        task = self._current_task()
        for p in task.params:
            if p.type not in ("dir", "path", "path_any"):
                continue
            editor = self._editors.get(p.name)
            if editor is None:
                continue
            line = editor.findChild(QLineEdit)
            if line is None:
                continue
            full = line.text()
            self._collapsed_paths[p.name] = full
            if full:
                base = Path(full.rstrip("/").rstrip("\\")).name or full
                line.setText(f".../{base}")
            line.setReadOnly(True)
            line.setCursorPosition(0)
            for btn in editor.findChildren(QPushButton):
                btn.setEnabled(False)

    def _thaw_path_inputs(self) -> None:
        """Restore the original full path and re-enable editing."""
        for name, full in self._collapsed_paths.items():
            editor = self._editors.get(name)
            if editor is None:
                continue
            line = editor.findChild(QLineEdit)
            if line is None:
                continue
            line.setText(full)
            line.setReadOnly(False)
            for btn in editor.findChildren(QPushButton):
                btn.setEnabled(True)
        self._collapsed_paths.clear()

    def _on_run(self) -> None:
        try:
            params = self._collect_params()
        except ValueError as e:
            self._status.setText(f"error: {e}")
            return
        task = self._current_task()
        self._log.clear()
        self._status.setText("running…")
        self._progress.reset()
        self._progress.setRange(0, 1)
        self._progress.setValue(0)

        # Tear down any leftover sub-bars from a previous run.
        for sub_id in list(self._sub_bars.keys()):
            self._remove_sub_bar(sub_id)
        self._progress.setVisible(True)
        self._errors_label.setText("")
        self._errors_label.setVisible(False)
        # Anchor the per-run window to SQLite's clock (matches the
        # values stored in parse_errors.created_at /
        # attachments.download_attempted_at).
        with connection() as conn:
            self._run_started_at = conn.execute(
                "SELECT datetime('now')"
            ).fetchone()[0]
        self._errors_timer.start()

        worker = TaskWorker(task, params)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.log.connect(self._on_log)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.failed.connect(self._on_failed)
        worker.signals.cancelled.connect(self._on_cancelled)
        worker.signals.sub_started.connect(self._on_sub_started)
        worker.signals.sub_progress.connect(self._on_sub_progress)
        worker.signals.sub_ended.connect(self._on_sub_ended)
        worker.signals.main_hidden.connect(self._on_main_hidden)
        self._active_worker = worker

        self._run_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._task_combo.setEnabled(False)
        self._freeze_path_inputs()
        self._pool.start(worker)

    def _on_cancel(self) -> None:
        if self._active_worker is not None:
            self._active_worker.cancel()
            self._status.setText("cancelling…")
            self._cancel_btn.setEnabled(False)

    def _on_progress(self, current: int, total: int, message: str) -> None:
        if total <= 0:
            total = max(current, 1)
        self._progress.setRange(0, total)
        self._progress.setValue(current)
        if message:
            self._progress.setStatus(_strip_markup(message))

    def _on_log(self, message: str) -> None:
        self._log.append(_strip_markup(message))

    def _on_main_hidden(self) -> None:
        self._progress.setVisible(False)

    def _on_sub_started(self, sub_id: int, label: str, total: int) -> None:
        group = AeroProgressGroup()
        group.setRange(0, total if total > 0 else 1)
        group.setValue(0)
        group.setFormat("%v / %m (%p%)")
        group.setStatus(_strip_markup(label))
        self._sub_layout.addWidget(group)
        self._sub_bars[sub_id] = group

    def _on_sub_progress(self, sub_id: int, current: int, total: int, message: str) -> None:
        group = self._sub_bars.get(sub_id)
        if group is None:
            return
        if total <= 0:
            total = max(current, 1)
        group.setRange(0, total)
        group.setValue(current)
        if message:
            group.setStatus(_strip_markup(message))

    def _on_sub_ended(self, sub_id: int) -> None:
        self._remove_sub_bar(sub_id)

    def _remove_sub_bar(self, sub_id: int) -> None:
        group = self._sub_bars.pop(sub_id, None)
        if group is None:
            return
        self._sub_layout.removeWidget(group)
        group.deleteLater()

    def _reset_buttons(self) -> None:
        self._run_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._task_combo.setEnabled(True)
        self._thaw_path_inputs()
        self._active_worker = None
        self._progress.setVisible(False)
        # One last poll so the final counts land before we stop.
        self._poll_errors()
        self._errors_timer.stop()

    def _poll_errors(self) -> None:
        """Refresh the live error/failure counter shown by the progress
        bars. Filtered to events that happened during this run, so the
        label doesn't carry over yesterday's failures.
        """
        if self._run_started_at is None:
            return
        try:
            with connection() as conn:
                parse_n = conn.execute(
                    "SELECT COUNT(*) FROM parse_errors WHERE created_at >= ?",
                    (self._run_started_at,),
                ).fetchone()[0]
                download_n = conn.execute(
                    "SELECT COUNT(*) FROM attachments "
                    " WHERE download_status = 'failed'"
                    "   AND download_attempted_at >= ?",
                    (self._run_started_at,),
                ).fetchone()[0]
        except Exception:
            return
        chunks: list[str] = []
        if parse_n:
            chunks.append(f"{parse_n} parse errors")
        if download_n:
            chunks.append(f"{download_n} failed downloads")
        if chunks:
            self._errors_label.setText(" · ".join(chunks))
            self._errors_label.setVisible(True)
        else:
            self._errors_label.setVisible(False)
            self._errors_label.setText("")

    def _on_view_results(self) -> None:
        # Heavy SQL rollups run off the GUI thread; the button shows
        # `Loading…` and gets re-enabled when the loader finishes.
        self._results_btn.setEnabled(False)
        self._results_btn.setText("Loading…")
        loader = _ResultsLoader()
        loader.signals.done.connect(self._on_results_ready)
        loader.signals.failed.connect(self._on_results_failed)
        self._pool.start(loader)

    def _on_results_ready(self, data: dict) -> None:
        self._results_btn.setEnabled(True)
        self._results_btn.setText("View results…")
        ResultsDialog(self, data).exec()

    def _on_results_failed(self, error: str) -> None:
        self._results_btn.setEnabled(True)
        self._results_btn.setText("View results…")
        self._status.setText(f"results failed: {error}")

    def _on_finished(self, run_id: int, result: object) -> None:
        self._status.setText(f"ok — run #{run_id}")
        # Skip dumping the raw dict — the user has the Results dialog
        # for a structured view now. Just acknowledge completion.
        self._log.append(f"run #{run_id} completed — click View results…")
        self._results_btn.setEnabled(True)
        self._reset_buttons()

    def _on_failed(self, run_id: int, error: str) -> None:
        self._status.setText(f"failed — run #{run_id}")
        self._log.append(error)
        self._reset_buttons()

    def _on_cancelled(self, run_id: int) -> None:
        self._status.setText(f"cancelled — run #{run_id}")
        self._reset_buttons()
