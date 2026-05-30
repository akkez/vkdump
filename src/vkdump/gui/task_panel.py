from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from rich.text import Text

from ..core.db import CancelHandle, connection, set_active_cancel
from ..tasks.spec import ParamSpec, TaskSpec
from .widgets.aero_progress import AeroProgressGroup
from .workers import TaskWorker


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


def _attach_searchable_combo(cb: "QComboBox") -> None:
    """Turn a populated `QComboBox` into a type-to-filter picker.

    The wiring is finicky because three handlers all fire on Enter and
    used to race each other, leaving the line edit showing one thing
    while `currentIndex` pointed at another. This version is explicit:

    * The popup is forced open whenever the user types — Firefox-like
      filter behaviour instead of relying on Qt's "show on Nth keystroke"
      heuristics that depended on focus order.
    * On Enter, we look at the completer's *currently highlighted*
      completion (the thing the popup is showing as selected after the
      user arrowed through it), find the matching combobox row by
      EXACT text, and set currentIndex. If nothing is highlighted (user
      typed but didn't arrow), we accept the first popup match.
    * On focus-out with text that doesn't match any item exactly, we
      revert the visible text to the current selection — but only then,
      so a successful pick from the popup never gets snapped back.
    """
    cb.setEditable(True)
    cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
    completer = QCompleter(cb.model(), cb)
    completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
    completer.setFilterMode(Qt.MatchFlag.MatchContains)
    completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
    cb.setCompleter(completer)
    line = cb.lineEdit()
    if line is None:
        return
    line.setPlaceholderText("Type to filter…")

    def _commit_exact(text: str) -> bool:
        if not text:
            return False
        idx = cb.findText(text, Qt.MatchFlag.MatchExactly)
        if idx < 0:
            return False
        # Block signals while we sync the line edit so we don't
        # re-trigger editingFinished / textEdited / etc., which used
        # to cascade into a snap-back that wiped the pick.
        was = cb.blockSignals(True)
        line.blockSignals(True)
        try:
            cb.setCurrentIndex(idx)
            line.setText(text)
        finally:
            line.blockSignals(False)
            cb.blockSignals(was)
        return True

    def _on_text_edited(_text: str) -> None:
        # Keep the popup open as the user types — without this, the
        # popup can vanish after a backspace and `currentCompletion()`
        # returns nothing, which is what caused the wrong-text-on-Enter
        # symptom (we fell through to the snap-back instead).
        completer.complete()

    def _on_activated(text: str) -> None:
        _commit_exact(text)

    def _on_return() -> None:
        # Prefer the highlighted popup row (what the user just arrowed
        # to). Falls back to the first popup match if nothing was
        # arrowed but text was typed.
        text = completer.currentCompletion()
        if not text and completer.completionCount() > 0:
            completer.setCurrentRow(0)
            text = completer.currentCompletion()
        if text and _commit_exact(text):
            return
        # No popup match at all — try the line edit text as a literal,
        # else keep the previous selection visually.
        if not _commit_exact(line.text().strip()):
            _commit_exact(cb.itemText(cb.currentIndex()))

    def _on_editing_finished() -> None:
        # Only snap back if the visible text genuinely doesn't match
        # any item — picks via popup/Enter already updated currentIndex
        # so this is a no-op for them.
        text = line.text().strip()
        if not text:
            return
        idx = cb.findText(text, Qt.MatchFlag.MatchExactly)
        if idx < 0:
            _commit_exact(cb.itemText(cb.currentIndex()))

    line.textEdited.connect(_on_text_edited)
    completer.activated.connect(_on_activated)
    line.returnPressed.connect(_on_return)
    line.editingFinished.connect(_on_editing_finished)


def _resolve_default(p: ParamSpec) -> Any:
    """Pick the value to seed an editor with at form-build time. The
    `default_provider` callback wins when present (so a task can
    prefill from `app_config` etc.); static `default` is the fallback.
    A provider that raises or returns None falls back gracefully.
    """
    if p.default_provider is not None:
        try:
            v = p.default_provider()
        except Exception:  # noqa: BLE001
            v = None
        if v is not None:
            return v
    return p.default


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


class _ErrorPollSignals(QObject):
    done = Signal(int, int)  # parse_n, download_n


class _ErrorPoll(QRunnable):
    """Counts parse / download errors since `started_at` off the GUI
    thread. The download-failed count is a full-scan on big DBs
    (~600ms on a 2.4 GB store) — running it inline on the GUI thread
    is what was eating frames during enrich runs.
    """

    def __init__(self, started_at: str) -> None:
        super().__init__()
        self.signals = _ErrorPollSignals()
        self.cancel_handle = CancelHandle()
        self._started_at = started_at

    def cancel(self) -> None:
        self.cancel_handle.cancel()

    @Slot()
    def run(self) -> None:
        if self.cancel_handle.is_cancelled():
            return
        set_active_cancel(self.cancel_handle)
        try:
            with connection() as conn:
                parse_n = conn.execute(
                    "SELECT COUNT(*) FROM parse_errors WHERE created_at >= ?",
                    (self._started_at,),
                ).fetchone()[0]
                download_n = conn.execute(
                    "SELECT COUNT(*) FROM attachments"
                    " WHERE download_status = 'failed'"
                    "   AND download_attempted_at >= ?",
                    (self._started_at,),
                ).fetchone()[0]
        except Exception:
            return
        finally:
            set_active_cancel(None)
        try:
            self.signals.done.emit(int(parse_n or 0), int(download_n or 0))
        except RuntimeError:
            # Receiver torn down (window closed mid-flight).
            pass


class TaskPanel(QWidget):
    """Auto-generated UI for one `TaskSpec`. The main window creates one
    panel per task and parks each in its own tab; there's no in-panel
    task switcher."""

    def __init__(self, task: TaskSpec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._task = task
        self._pool = QThreadPool.globalInstance()
        self._active_worker: TaskWorker | None = None
        self._editors: dict[str, QWidget] = {}
        # Original text of path inputs while a task is running; the
        # visible field gets collapsed to ".../<basename>" until the
        # run ends, then we restore from here.
        self._collapsed_paths: dict[str, str] = {}

        self._description = QLabel(task.description)
        self._description.setWordWrap(True)
        self._description.setStyleSheet("color: gray;")
        self._description.setVisible(bool(task.description))

        form_widget = QWidget()
        form = QFormLayout(form_widget)
        for p in task.params:
            editor = self._make_editor(p)
            self._editors[p.name] = editor
            form.addRow(p.label + ":", editor)

        self._run_btn = QPushButton("Run")
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._on_run)
        self._cancel_btn.clicked.connect(self._on_cancel)
        # Optional "Open output" button — only shown for tasks that
        # declare a `result_open_path` callable. Stays disabled until
        # a successful run produces a usable path on disk.
        self._open_btn: QPushButton | None = None
        self._open_path: str | None = None
        if self._task.result_open_path is not None:
            self._open_btn = QPushButton("Open output")
            self._open_btn.setEnabled(False)
            self._open_btn.clicked.connect(self._on_open_output)

        self._progress = AeroProgressGroup()
        # Start with a degenerate range — the group falls back to a flat
        # "0%" until a real total arrives via `_on_progress`.
        self._progress.setRange(0, 0)
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
        # Single-flight handle for the off-thread error poll: skip new
        # polls while one is already in the QThreadPool, otherwise on
        # huge DBs (where one poll can take 500–700 ms) successive timer
        # ticks pile up faster than the pool can drain them.
        self._errors_inflight: _ErrorPoll | None = None

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
        # Hard cap on the document so a chatty module (or SQL_DEBUG)
        # can't grow it to the point where each insert restyles tens of
        # thousands of blocks. Excess blocks fall off the top in FIFO.
        self._log.document().setMaximumBlockCount(2000)

        # Worker → GUI batching. On Windows in particular every
        # QTextEdit.append() and every QProgressBar.setValue() triggers
        # a native theme paint pass — at 100s/s the GUI thread spends
        # most of its time in repaint. We coalesce: log lines pile up in
        # a buffer flushed at ~20 Hz; the latest progress (main + per
        # sub_id) is stored and applied at ~30 Hz. Worker emits stay
        # cheap; the user-visible frame rate stays steady.
        self._log_buffer: list[str] = []
        self._log_flush_timer = QTimer(self)
        self._log_flush_timer.setInterval(50)  # 20 Hz
        self._log_flush_timer.timeout.connect(self._flush_log_buffer)
        self._log_flush_timer.start()

        self._pending_progress: tuple[int, int, str] | None = None
        self._pending_sub_progress: dict[int, tuple[int, int, str]] = {}
        self._progress_flush_timer = QTimer(self)
        self._progress_flush_timer.setInterval(33)  # ~30 Hz
        self._progress_flush_timer.timeout.connect(self._flush_progress_buffer)
        self._progress_flush_timer.start()

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._cancel_btn)
        if self._open_btn is not None:
            btn_row.addWidget(self._open_btn)
        btn_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(self._description)
        layout.addWidget(form_widget)
        layout.addLayout(btn_row)
        layout.addWidget(self._progress)
        layout.addWidget(self._sub_host)
        layout.addWidget(self._errors_label)
        layout.addWidget(self._status)
        layout.addWidget(self._log, 1)

    def _make_editor(self, p: ParamSpec) -> QWidget:
        initial = _resolve_default(p)
        if p.type in ("dir", "path", "path_any"):
            row = QWidget()
            line = _ClickyLineEdit() if p.type == "path_any" else QLineEdit()
            line.setObjectName(f"{p.name}__edit")
            if initial is not None:
                line.setText(str(initial))
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
            if initial is not None:
                sb.setValue(int(initial))
            return sb
        if p.type == "bool":
            cb = QCheckBox()
            cb.setChecked(bool(initial))
            return cb
        if p.type == "choice":
            cb = QComboBox()
            opts = list(p.choices)
            if p.choices_provider is not None:
                try:
                    opts.extend(p.choices_provider())
                except Exception as exc:  # noqa: BLE001
                    # A failing provider shouldn't break the entire form;
                    # the static `choices` (if any) still render.
                    cb.addItem(f"<choices unavailable: {exc}>", "")
            for value, label in opts:
                cb.addItem(label, value)
            if initial is not None:
                idx = cb.findData(str(initial))
                if idx >= 0:
                    cb.setCurrentIndex(idx)
            if len(opts) > 10:
                _attach_searchable_combo(cb)
            return cb
        line = QLineEdit()
        if initial is not None:
            line.setText(str(initial))
        line.setPlaceholderText(p.help or "")
        return line

    def _collect_params(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p in self._task.params:
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
            elif p.type == "choice":
                # Empty-string sentinel == "no selection" (e.g. the
                # "All chats" placeholder); treat it as missing rather
                # than passing the literal "" downstream.
                val = editor.currentData()
                out[p.name] = val if val not in (None, "") else None
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
        for p in self._task.params:
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

    def has_active_run(self) -> bool:
        """True while a TaskWorker is in flight. The main window uses
        this to decide whether to ask the user before quitting."""
        return self._active_worker is not None

    def _on_run(self) -> None:
        try:
            params = self._collect_params()
        except ValueError as e:
            self._status.setText(f"error: {e}")
            return
        self._log_buffer.clear()
        self._log.clear()
        self._pending_progress = None
        self._pending_sub_progress.clear()
        self._status.setText("running…")
        self._progress.reset()
        # See __init__: degenerate range == "0%" placeholder until the
        # module reports a real total.
        self._progress.setRange(0, 0)
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

        worker = TaskWorker(self._task, params)
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
        self._freeze_path_inputs()
        self._pool.start(worker)

    def _on_cancel(self) -> None:
        if self._active_worker is not None:
            self._active_worker.cancel()
            self._status.setText("cancelling…")
            self._cancel_btn.setEnabled(False)

    def _on_progress(self, current: int, total: int, message: str) -> None:
        # Stash the latest tick; `_flush_progress_buffer` applies it on
        # the next 30 Hz tick. Coalesces bursts so the bar repaints at
        # most ~30 times/sec no matter how often the worker emits.
        self._pending_progress = (current, total, message)

    def _on_log(self, message: str) -> None:
        # Buffer; `_flush_log_buffer` drains at 20 Hz with one cursor
        # insert per batch (vs N append() calls, which on Windows each
        # trigger a full QTextEdit layout pass).
        self._log_buffer.append(_strip_markup(message))

    def _flush_progress_buffer(self) -> None:
        if self._pending_progress is not None:
            current, total, message = self._pending_progress
            self._pending_progress = None
            # `total <= 0` means the module doesn't know the size yet —
            # keep the bar in its "0%" placeholder state instead of
            # inventing a bogus total.
            self._progress.setRange(0, max(total, 0))
            self._progress.setValue(current)
            if message:
                self._progress.setStatus(_strip_markup(message))

        if self._pending_sub_progress:
            pending = self._pending_sub_progress
            self._pending_sub_progress = {}
            for sub_id, (current, total, message) in pending.items():
                group = self._sub_bars.get(sub_id)
                if group is None:
                    continue
                group.setRange(0, max(total, 0))
                group.setValue(current)
                if message:
                    group.setStatus(_strip_markup(message))

    def _flush_log_buffer(self) -> None:
        if not self._log_buffer:
            return
        text = "\n".join(self._log_buffer)
        self._log_buffer.clear()
        cursor = self._log.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        # Prepend a newline only if the doc already has content, so we
        # don't waste the first line on an empty separator.
        if self._log.document().characterCount() > 1:
            cursor.insertText("\n" + text)
        else:
            cursor.insertText(text)
        sb = self._log.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _on_main_hidden(self) -> None:
        self._progress.setVisible(False)

    def _on_sub_started(self, sub_id: int, label: str, total: int) -> None:
        group = AeroProgressGroup()
        group.setRange(0, max(total, 0))
        group.setValue(0)
        group.setFormat("%v / %m (%p%)")
        group.setStatus(_strip_markup(label))
        self._sub_layout.addWidget(group)
        self._sub_bars[sub_id] = group

    def _on_sub_progress(self, sub_id: int, current: int, total: int, message: str) -> None:
        # Coalesced — `_flush_progress_buffer` applies the latest tick
        # per sub_id at ~30 Hz.
        self._pending_sub_progress[sub_id] = (current, total, message)

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
        self._thaw_path_inputs()
        self._active_worker = None
        self._progress.setVisible(False)
        # One last poll so the final counts land before we stop.
        self._poll_errors()
        self._errors_timer.stop()

    def _poll_errors(self) -> None:
        """Kick a background error-count refresh. The actual SQL happens
        in a `_ErrorPoll` runnable so a 500–700 ms full-scan on a huge
        DB doesn't freeze the GUI thread; the result lands via
        `_on_errors_polled`. Single-flight: ignore the tick if the
        previous poll is still running.
        """
        if self._run_started_at is None:
            return
        if self._errors_inflight is not None:
            return
        poll = _ErrorPoll(self._run_started_at)
        poll.signals.done.connect(self._on_errors_polled)
        self._errors_inflight = poll
        self._pool.start(poll)

    def _on_errors_polled(self, parse_n: int, download_n: int) -> None:
        self._errors_inflight = None
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

    def cancel_background_queries(self) -> None:
        """Interrupt any in-flight error-poll so a closing window
        doesn't keep churning a multi-hundred-ms SELECT after the
        receiver is gone.
        """
        if self._errors_inflight is not None:
            self._errors_inflight.cancel()

    def _on_finished(self, run_id: int, result: object) -> None:
        self._status.setText(f"ok — run #{run_id}")
        self._log_buffer.append(f"run #{run_id} completed")
        self._flush_log_buffer()
        self._flush_progress_buffer()
        self._update_open_button(result)
        self._reset_buttons()

    def _update_open_button(self, result: object) -> None:
        if self._open_btn is None or self._task.result_open_path is None:
            return
        try:
            path = self._task.result_open_path(result)
        except Exception:  # noqa: BLE001
            path = None
        self._open_path = str(path) if path else None
        self._open_btn.setEnabled(bool(self._open_path))

    def _on_open_output(self) -> None:
        if not self._open_path:
            return
        import webbrowser
        from pathlib import Path as _Path
        webbrowser.open(_Path(self._open_path).resolve().as_uri())

    def _on_failed(self, run_id: int, error: str) -> None:
        self._status.setText(f"failed — run #{run_id}")
        self._log_buffer.append(error)
        self._flush_log_buffer()
        self._flush_progress_buffer()
        self._reset_buttons()

    def _on_cancelled(self, run_id: int) -> None:
        self._status.setText(f"cancelled — run #{run_id}")
        self._flush_log_buffer()
        self._flush_progress_buffer()
        self._reset_buttons()
