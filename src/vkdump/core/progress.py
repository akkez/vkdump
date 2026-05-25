from contextlib import contextmanager
from dataclasses import dataclass
from typing import ContextManager, Iterator, Protocol


class Cancelled(Exception):
    """Raised by ProgressReporter.check_cancelled() when the user requests cancellation."""


@dataclass
class CancellationToken:
    _cancelled: bool = False

    def cancel(self) -> None:
        self._cancelled = True

    def is_cancelled(self) -> bool:
        return self._cancelled


class ProgressReporter(Protocol):
    def report(self, current: int, total: int, message: str = "") -> None: ...
    def check_cancelled(self) -> None: ...
    def log(self, message: str) -> None: ...

    def sub(self, label: str, total: int) -> ContextManager["ProgressReporter"]:
        """Open a nested progress scope (e.g. for one of many sub-tasks).

        Implementations that don't render sub-progress separately may yield
        `self` so that the scope is still iterable and the parent's bar
        absorbs the reports.
        """
        ...


@contextmanager
def _self_sub(reporter: "ProgressReporter", label: str, total: int) -> Iterator["ProgressReporter"]:
    """Default sub() implementation: yields the parent reporter unchanged."""
    reporter.log(f"› {label} (×{total})")
    yield reporter
