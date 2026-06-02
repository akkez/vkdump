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


def test_get_by_id_rate_limit_retries_past_schedule_exhaustion() -> None:
    """Rate-limit retries are indefinite — the fixed schedule
    (`_RATE_LIMIT_BACKOFF_S`) only controls the delay between attempts,
    never the upper bound on retry count. After the schedule is
    exhausted the loop must keep going with the final (15 s) delay."""
    schedule = fetch_data._RATE_LIMIT_BACKOFF_S
    n_rl = len(schedule) + 4  # exhaust the schedule, then four more rl hits
    rate_limited = VKApiError(6, "Too many requests per second")
    good = {"items": [{"id": 1}]}
    vk = _FakeVK([rate_limited] * n_rl + [good])
    limiter = RateLimiter(100.0)
    stop = asyncio.Event()
    progress = _LogReporter()

    items = _run(fetch_data._get_by_id_with_backoff(
        vk, limiter, [1], stop, progress,
    ))
    assert [m["id"] for m in items] == [1]
    assert len(vk.calls) == n_rl + 1
    rl_lines = [ln for ln in progress.lines if "rate limited" in ln]
    assert len(rl_lines) == n_rl
    # First few attempts walk the schedule, later ones clamp to the last entry.
    assert f"backing off {schedule[0]}s" in rl_lines[0]
    assert f"backing off {schedule[-1]}s" in rl_lines[-1]


def test_get_by_id_non_rate_limit_error_still_propagates() -> None:
    """Bad-token / permission / malformed-request errors aren't retryable
    at this layer — they must bubble out so the orchestrator can mark
    the run failed instead of looping forever on a doomed call."""
    vk = _FakeVK([VKApiError(5, "User authorization failed")])
    limiter = RateLimiter(100.0)
    stop = asyncio.Event()
    progress = _LogReporter()
    with pytest.raises(VKApiError) as exc:
        _run(fetch_data._get_by_id_with_backoff(
            vk, limiter, [1], stop, progress,
        ))
    assert exc.value.code == 5


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


def test_execute_oversized_response_downgrades_to_per_batch_get_by_id() -> None:
    """VK code 13 ("response size is too big") means the bundled execute
    payload is unusable. The loop must downgrade to running each
    sub-batch as a standalone messages.getById and stitch the per-batch
    results back together — one ugly batch shouldn't kill the worker."""
    oversized = VKApiError(13, "Runtime error occurred during code invocation:"
                               " response size is too big")
    # After the execute hard error, three per-batch getById calls
    # follow; each returns its own slice of items.
    batches = [[1, 2], [3, 4], [5]]
    per_batch_responses = [
        {"items": [{"id": 1}, {"id": 2}]},
        {"items": [{"id": 3}, {"id": 4}]},
        {"items": [{"id": 5}]},
    ]
    vk = _FakeVK([oversized, *per_batch_responses])
    limiter = RateLimiter(100.0)
    stop = asyncio.Event()
    progress = _LogReporter()

    items, missing = _run(fetch_data._execute_get_by_id_with_backoff(
        vk, limiter, batches, stop, progress,
    ))
    assert [m["id"] for m in items] == [1, 2, 3, 4, 5]
    assert missing == 0
    # 1 execute + 3 messages.getById = 4 vk calls total.
    methods = [c[0] for c in vk.calls]
    assert methods == ["execute", "messages.getById", "messages.getById", "messages.getById"]
    assert any("downgrading" in ln and "code=13" in ln for ln in progress.lines), \
        progress.lines


def test_execute_rate_limit_does_not_downgrade() -> None:
    """Rate-limit on execute is just a pacing problem — keep retrying
    the same execute. It must NOT trigger the per-batch downgrade
    (which would 25x the request count and make the throttle worse)."""
    rate_limited = VKApiError(6, "Too many requests per second")
    good_response = [{"count": 1, "items": [{"id": 7}]}]
    vk = _FakeVK([rate_limited, rate_limited, (good_response, [])])
    limiter = RateLimiter(100.0)
    stop = asyncio.Event()
    progress = _LogReporter()

    items, missing = _run(fetch_data._execute_get_by_id_with_backoff(
        vk, limiter, [[7]], stop, progress,
    ))
    assert [m["id"] for m in items] == [7]
    assert missing == 0
    methods = [c[0] for c in vk.calls]
    assert methods == ["execute", "execute", "execute"]
    assert not any("downgrading" in ln for ln in progress.lines)


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
