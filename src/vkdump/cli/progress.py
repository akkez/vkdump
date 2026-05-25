from contextlib import contextmanager
from typing import Iterator

from rich.progress import Progress, TaskID

from ..core.progress import ProgressReporter


class CliProgress:
    """ProgressReporter backed by rich.progress.Progress.

    Cancellation in the CLI happens via Ctrl-C → KeyboardInterrupt, which the
    orchestrator catches and converts; check_cancelled() stays a no-op.

    `sub()` opens a nested rich Progress task (rendered as an extra line
    below the main bar) and removes it when the scope exits.
    """

    def __init__(self, rp: Progress, task_id: TaskID) -> None:
        self._rp = rp
        self._tid = task_id

    def report(self, current: int, total: int, message: str = "") -> None:
        self._rp.update(
            self._tid,
            completed=current,
            total=total if total > 0 else None,
            description=message or self._rp.tasks[self._tid].description,
        )

    def check_cancelled(self) -> None:  # noqa: D401
        return

    def log(self, message: str) -> None:
        self._rp.console.log(message)

    def hide_main(self) -> None:
        self._rp.update(self._tid, visible=False)

    @contextmanager
    def sub(self, label: str, total: int) -> Iterator["CliProgress"]:
        sub_tid = self._rp.add_task(label, total=total if total > 0 else None)
        try:
            yield _SubCliProgress(self._rp, sub_tid, parent=self)
        finally:
            # Remove the per-sub bar once its scope is done; the parent's
            # global bar still shows the cumulative progress.
            self._rp.remove_task(sub_tid)


class _SubCliProgress(CliProgress):
    """Inner progress reporter sharing a Progress instance with its parent.

    log/check_cancelled delegate to the parent so messages flow to the same
    console and cancellation works at any nesting level.
    """

    def __init__(self, rp: Progress, task_id: TaskID, parent: CliProgress) -> None:
        super().__init__(rp, task_id)
        self._parent = parent

    def check_cancelled(self) -> None:
        self._parent.check_cancelled()

    def log(self, message: str) -> None:
        self._parent.log(message)
