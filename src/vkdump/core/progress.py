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

    def hide_main(self) -> None:
        """Suppress the orchestrator-level row when a module owns its own
        bars via `sub()`. Implementations that don't render a main row
        may make this a no-op.
        """
        ...


@contextmanager
def _self_sub(reporter: "ProgressReporter", label: str, total: int) -> Iterator["ProgressReporter"]:
    """Default sub() implementation: yields the parent reporter unchanged."""
    reporter.log(f"› {label} (×{total})")
    yield reporter


class Throttle:
    """Cooperative rate-limiter for progress-bar updates.

    Calling code does `if throttle(i, total): progress.report(...)`.

    Returns True (= "emit now") iff any of:
      - this is the very first tick (`i == 1`),
      - or the final tick (`i == total`) so the bar lands at 100%,
      - or at least `every_n` ticks have passed since the last emit,
      - or `every_s` seconds have elapsed.

    Defaults are tuned for hundreds-of-thousands-of-items workloads
    where per-tick GUI signal emission used to dominate; the user
    barely sees a difference between a 25 Hz and a 250 Hz bar.
    """

    def __init__(self, every_n: int = 10, every_s: float = 0.25) -> None:
        self._every_n = every_n
        self._every_s = every_s
        self._last_idx = 0
        self._last_t = 0.0

    def __call__(self, i: int, total: int) -> bool:
        import time as _time

        now = _time.monotonic()
        if (
            i == 1
            or i == total
            or i - self._last_idx >= self._every_n
            or now - self._last_t >= self._every_s
        ):
            self._last_idx = i
            self._last_t = now
            return True
        return False
