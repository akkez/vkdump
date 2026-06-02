"""Sliding-window throughput tracker (1m / 5m / 15m, htop load-average style).

Callers feed in unitless "units" (messages, requests, …) via :meth:`add`.
:meth:`averages` returns the exponentially weighted unit-per-second rate
across each window — same shape the kernel uses for ``loadavg`` so the
numbers feel familiar to anyone who reads ``htop``.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass


_WINDOWS_S: tuple[float, float, float] = (60.0, 300.0, 900.0)


@dataclass
class ThroughputAverages:
    one_min: float
    five_min: float
    fifteen_min: float

    def format(self) -> str:
        return f"{self.one_min:.1f} / {self.five_min:.1f} / {self.fifteen_min:.1f}"


class Throughput:
    """Tracks an EWMA of ``units / second`` across 1m / 5m / 15m windows.

    Internally the moving average is recomputed on every :meth:`add` /
    :meth:`averages` call using the elapsed time since the last update
    so the rate decays cleanly even when no events arrive."""

    def __init__(self, now: float | None = None) -> None:
        t = time.monotonic() if now is None else now
        self._last_t = t
        self._pending = 0.0
        self._avg = [0.0, 0.0, 0.0]

    def add(self, units: int) -> None:
        if units <= 0:
            return
        self._pending += float(units)

    def averages(self, now: float | None = None) -> ThroughputAverages:
        t = time.monotonic() if now is None else now
        dt = t - self._last_t
        if dt <= 0:
            return ThroughputAverages(*self._avg)
        rate = self._pending / dt
        for i, window in enumerate(_WINDOWS_S):
            alpha = 1.0 - math.exp(-dt / window)
            self._avg[i] += alpha * (rate - self._avg[i])
        self._pending = 0.0
        self._last_t = t
        return ThroughputAverages(*self._avg)
