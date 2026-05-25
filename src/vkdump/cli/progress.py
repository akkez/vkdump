from rich.progress import Progress, TaskID
from ..core.progress import Cancelled


class CliProgress:
    """ProgressReporter implementation backed by rich.progress.Progress.

    Cancellation in the CLI happens via Ctrl-C → KeyboardInterrupt, which the
    orchestrator catches and converts. check_cancelled() stays a no-op here.
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
