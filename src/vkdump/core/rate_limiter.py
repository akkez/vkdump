"""Async rate limiter shared across concurrent VK API callers.

Caps the *rate at which requests are dispatched to the network*: no more
than ``max_per_second`` calls to :meth:`acquire` return in any 1-second
sliding window. Multiple coroutines awaiting the same limiter are served
in FIFO order under a single asyncio.Lock — no thundering herd, no
oversubscription past the cap even with hundreds of in-flight workers.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_per_second: float) -> None:
        if max_per_second <= 0:
            raise ValueError("max_per_second must be positive")
        self._max = max_per_second
        self._window = 1.0
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                cutoff = now - self._window
                while self._stamps and self._stamps[0] <= cutoff:
                    self._stamps.popleft()
                if len(self._stamps) < self._max:
                    self._stamps.append(now)
                    return
                wait_for = self._stamps[0] + self._window - now
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
