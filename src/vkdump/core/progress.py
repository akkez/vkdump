from dataclasses import dataclass
from typing import Protocol


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
