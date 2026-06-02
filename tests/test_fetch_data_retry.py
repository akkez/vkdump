"""Regression tests for fetch-data's backoff loops.

* VK timeouts retry indefinitely with a short delay — a slow upstream
  shouldn't crash the whole scrape.
* Rate-limit errors keep the original fixed schedule (`_RATE_LIMIT_BACKOFF_S`)
  and propagate on exhaustion.

Both loops are exercised against a fake VK client; we monkey-patch
``_sleep_until_stop`` to a no-op so a 2-second retry doesn't slow the
test suite.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from vkdump.core.progress import ProgressReporter
from vkdump.core.rate_limiter import RateLimiter
from vkdump.core.vk_api import VKApiError, VKApiTimeout
from vkdump.modules import fetch_data


class _LogReporter:
    """Records progress.log lines; no-ops the rest of the protocol."""
    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, message: str) -> None:
        self.lines.append(message)

    def report(self, current: int, total: int, message: str = "") -> None: ...
    def check_cancelled(self) -> None: ...
    def sub(self, label: str, total: int):
        raise NotImplementedError

    def hide_main(self) -> None: ...


class _FakeVK:
    """Returns a scripted sequence of outcomes for ``call`` / ``execute``.

    Each entry in ``script`` is either an exception instance (raised on
    that call) or a response dict (returned). The fake counts calls so
    the test can assert on how many retries the loop issued.
    """
    def __init__(self, script: list[Any]) -> None:
        self._script = list(script)
        self.calls: list[tuple[str, dict]] = []

    async def call(self, method: str, **params: Any) -> Any:
        self.calls.append((method, params))
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def execute(self, code: str) -> tuple[Any, list[dict]]:
        self.calls.append(("execute", {"code": code}))
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _instant_sleep(monkeypatch):
    """Skip real sleeps in the backoff path so the suite stays fast."""
    async def _noop(delay: float, stop: asyncio.Event) -> None:
        return None
    monkeypatch.setattr(fetch_data, "_sleep_until_stop", _noop)


def test_get_by_id_retries_timeouts_indefinitely_then_succeeds() -> None:
    """Three timeouts in a row, then a clean response — the loop must
    keep retrying and ultimately return the response payload."""
    good = {"items": [{"id": 1}, {"id": 2}, {"id": 3}]}
    vk = _FakeVK([
        VKApiTimeout(None, "request timed out"),
        VKApiTimeout(None, "request timed out"),
        VKApiTimeout(None, "request timed out"),
        good,
    ])
    limiter = RateLimiter(100.0)  # effectively unthrottled
    stop = asyncio.Event()
    progress = _LogReporter()

    items = _run(fetch_data._get_by_id_with_backoff(
        vk, limiter, [1, 2, 3], stop, progress,
    ))

    assert [m["id"] for m in items] == [1, 2, 3]
    assert len(vk.calls) == 4
    timeout_lines = [ln for ln in progress.lines if "timeout" in ln.lower()]
    assert len(timeout_lines) == 3
    # Each retry must announce a fresh attempt number so the user can
    # tell whether the loop is making progress or wedged.
    assert "attempt 1" in timeout_lines[0]
    assert "attempt 3" in timeout_lines[2]


def test_get_by_id_rate_limit_schedule_unchanged() -> None:
    """Six rate-limit errors should exhaust the fixed schedule and the
    seventh propagates — timeouts must not have widened the policy."""
    schedule = fetch_data._RATE_LIMIT_BACKOFF_S
    rate_limited = VKApiError(6, "Too many requests per second")
    vk = _FakeVK([rate_limited] * (len(schedule) + 1))
    limiter = RateLimiter(100.0)
    stop = asyncio.Event()
    progress = _LogReporter()

    with pytest.raises(VKApiError) as exc:
        _run(fetch_data._get_by_id_with_backoff(
            vk, limiter, [1], stop, progress,
        ))
    assert exc.value.code == 6
    # One call per schedule entry, plus the one that raised after
    # the schedule ran out — so len(schedule)+1 total.
    assert len(vk.calls) == len(schedule) + 1


def test_execute_retries_timeouts_indefinitely_then_succeeds() -> None:
    """Same contract for the execute strategy: timeouts retry forever,
    a later good response wins."""
    good_response = [
        {"count": 2, "items": [{"id": 10}, {"id": 11}]},
        {"count": 1, "items": [{"id": 20}]},
    ]
    vk = _FakeVK([
        VKApiTimeout(None, "request timed out"),
        VKApiTimeout(None, "request timed out"),
        (good_response, []),
    ])
    limiter = RateLimiter(100.0)
    stop = asyncio.Event()
    progress = _LogReporter()

    items, missing = _run(fetch_data._execute_get_by_id_with_backoff(
        vk, limiter, [[10, 11], [20]], stop, progress,
    ))

    assert [m["id"] for m in items] == [10, 11, 20]
    assert missing == 0
    assert len(vk.calls) == 3


def test_stop_during_timeout_retry_short_circuits() -> None:
    """If the user clicks Stop while we're waiting on a timeout retry,
    the next iteration must bail without issuing another API call."""
    vk = _FakeVK([VKApiTimeout(None, "request timed out")])
    limiter = RateLimiter(100.0)
    stop = asyncio.Event()
    progress = _LogReporter()

    # Flip stop the moment the first timeout's retry sleep is awaited,
    # so the loop sees stop.is_set() and exits before re-dispatching.
    async def _stop_then_noop(delay: float, _stop: asyncio.Event) -> None:
        _stop.set()
    import pytest as _pytest  # noqa: F401  (keeps import order stable)
    fetch_data._sleep_until_stop = _stop_then_noop  # type: ignore[assignment]

    items = _run(fetch_data._get_by_id_with_backoff(
        vk, limiter, [1], stop, progress,
    ))
    assert items == []
    assert len(vk.calls) == 1
