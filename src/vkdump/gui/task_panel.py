from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..tasks.registry import TASKS
from ..tasks.spec import ParamSpec, TaskSpec
from .workers import TaskWorker


class TaskPanel(QWidget):
    """Auto-generated UI for any TaskSpec from the registry."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool.globalInstance()
        self._active_worker: TaskWorker | None = None
        self._editors: dict[str, QWidget] = {}

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
        self._cancel_btn.setEnabled(False)
        self._run_btn.clicked.connect(self._on_run)
        self._cancel_btn.clicked.connect(self._on_cancel)

        self._progress = QProgressBar()
        self._progress.setRange(0, 1)
        self._progress.setValue(0)
        self._progress.setFormat("%v / %m  %p%")

        self._status = QLabel("")
        self._log = QTextEdit()
        self._log.setReadOnly(True)

        btn_row = QHBoxLayout()
        btn_row.addWidget(self._run_btn)
        btn_row.addWidget(self._cancel_btn)
        btn_row.addStretch(1)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Task"))
        layout.addWidget(self._task_combo)
        layout.addWidget(self._description)
        layout.addWidget(self._form_host)
        layout.addLayout(btn_row)
        layout.addWidget(self._progress)
        layout.addWidget(self._status)
        layout.addWidget(self._log, 1)

        self._rebuild_form()

    def _current_task(self) -> TaskSpec:
        name = self._task_combo.currentData()
        return next(t for t in TASKS if t.name == name)

    def _rebuild_form(self) -> None:
        task = self._current_task()
        self._description.setText(task.description)
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
        if p.type in ("dir", "path"):
            row = QWidget()
            line = QLineEdit()
            line.setObjectName(f"{p.name}__edit")
            if p.default is not None:
                line.setText(str(p.default))
            line.setPlaceholderText(p.help or "")
            browse = QPushButton("Browse…")

            def on_browse() -> None:
                if p.type == "dir":
                    path = QFileDialog.getExistingDirectory(self, f"Select {p.label}")
                else:
                    path, _ = QFileDialog.getOpenFileName(self, f"Select {p.label}")
                if path:
                    line.setText(path)

            browse.clicked.connect(on_browse)
            layout = QHBoxLayout(row)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(line, 1)
            layout.addWidget(browse)
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
        out: dict[str, Any] = {}
        for p in task.params:
            editor = self._editors[p.name]
            if p.type in ("dir", "path"):
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

    def _on_run(self) -> None:
        try:
            params = self._collect_params()
        except ValueError as e:
            self._status.setText(f"error: {e}")
            return
        task = self._current_task()
        self._log.clear()
        self._status.setText("running…")
        self._progress.setRange(0, 1)
        self._progress.setValue(0)

        worker = TaskWorker(task, params)
        worker.signals.progress.connect(self._on_progress)
        worker.signals.log.connect(self._log.append)
        worker.signals.finished.connect(self._on_finished)
        worker.signals.failed.connect(self._on_failed)
        worker.signals.cancelled.connect(self._on_cancelled)
        self._active_worker = worker

        self._run_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._task_combo.setEnabled(False)
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
            self._status.setText(message)

    def _reset_buttons(self) -> None:
        self._run_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._task_combo.setEnabled(True)
        self._active_worker = None

    def _on_finished(self, run_id: int, result: object) -> None:
        self._status.setText(f"ok — run #{run_id}")
        self._log.append(f"result: {result}")
        self._reset_buttons()

    def _on_failed(self, run_id: int, error: str) -> None:
        self._status.setText(f"failed — run #{run_id}")
        self._log.append(error)
        self._reset_buttons()

    def _on_cancelled(self, run_id: int) -> None:
        self._status.setText(f"cancelled — run #{run_id}")
        self._reset_buttons()
