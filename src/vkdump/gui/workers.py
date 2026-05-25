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


class GuiProgress:
    def __init__(self, signals: WorkerSignals, token: CancellationToken) -> None:
        self._signals = signals
        self._token = token

    def report(self, current: int, total: int, message: str = "") -> None:
        self._signals.progress.emit(current, total, message)

    def check_cancelled(self) -> None:
        if self._token.is_cancelled():
            raise Cancelled()

    def log(self, message: str) -> None:
        self._signals.log.emit(message)

    @contextmanager
    def sub(self, label: str, total: int) -> Iterator["GuiProgress"]:
        # No dedicated sub-progress widget yet: surface the sub-task as a
        # log line and reuse the same reporter for inner reports.
        self.log(f"› {label} (×{total})")
        yield self

    def hide_main(self) -> None:
        # GUI doesn't render an orchestrator-level bar separately, so
        # there's nothing to hide.
        return None


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
