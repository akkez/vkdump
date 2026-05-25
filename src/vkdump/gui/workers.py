from contextlib import contextmanager
from typing import Iterator

from PySide6.QtCore import QObject, QRunnable, Signal, Slot

from ..core.orchestrator import execute
from ..core.progress import CancellationToken, Cancelled
from ..tasks.spec import TaskSpec


class WorkerSignals(QObject):
    progress = Signal(int, int, str)
    log = Signal(str)
    finished = Signal(int, object)
    failed = Signal(int, str)
    cancelled = Signal(int)
    # Sub-task progress: a unique id distinguishes nested / concurrent
    # sub-bars (e.g. the per-chat and the global bars that parse_dump
    # owns). The panel maintains one QProgressBar per id.
    sub_started = Signal(int, str, int)   # sub_id, label, total
    sub_progress = Signal(int, int, int, str)  # sub_id, current, total, message
    sub_ended = Signal(int)               # sub_id
    main_hidden = Signal()


class GuiProgress:
    def __init__(self, signals: WorkerSignals, token: CancellationToken) -> None:
        self._signals = signals
        self._token = token
        self._next_sub_id = 0

    def report(self, current: int, total: int, message: str = "") -> None:
        self._signals.progress.emit(current, total, message)

    def check_cancelled(self) -> None:
        if self._token.is_cancelled():
            raise Cancelled()

    def log(self, message: str) -> None:
        self._signals.log.emit(message)

    @contextmanager
    def sub(self, label: str, total: int) -> Iterator["_SubGuiProgress"]:
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        self._signals.sub_started.emit(sub_id, label, total)
        sub = _SubGuiProgress(self._signals, self._token, sub_id, parent=self)
        try:
            yield sub
        finally:
            self._signals.sub_ended.emit(sub_id)

    def hide_main(self) -> None:
        self._signals.main_hidden.emit()


class _SubGuiProgress(GuiProgress):
    def __init__(self, signals, token, sub_id, parent: GuiProgress) -> None:
        super().__init__(signals, token)
        self._sub_id = sub_id
        self._parent = parent

    def report(self, current: int, total: int, message: str = "") -> None:
        self._signals.sub_progress.emit(self._sub_id, current, total, message)

    def log(self, message: str) -> None:
        self._parent.log(message)

    def check_cancelled(self) -> None:
        self._parent.check_cancelled()


class TaskWorker(QRunnable):
    def __init__(self, task: TaskSpec, params: dict) -> None:
        super().__init__()
        self.task = task
        self.params = params
        self.signals = WorkerSignals()
        self.token = CancellationToken()

    def cancel(self) -> None:
        self.token.cancel()

    @Slot()
    def run(self) -> None:
        reporter = GuiProgress(self.signals, self.token)
        outcome = execute(self.task, self.params, reporter)
        if outcome.status == "success":
            self.signals.finished.emit(outcome.run_id, outcome.result)
        elif outcome.status == "cancelled":
            self.signals.cancelled.emit(outcome.run_id)
        else:
            self.signals.failed.emit(outcome.run_id, outcome.error or "unknown error")
