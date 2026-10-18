from __future__ import annotations

import time
from collections import deque


def format_bytes(size: int) -> str:
    value = max(0, size)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if value < 1000 or unit == 'TB':
            return f'{value:.0f} B' if unit == 'B' else f'{value:.2f} {unit}'
        value /= 1000
    raise AssertionError('unreachable')


def format_eta(seconds: float) -> str:
    """Format seconds as 0:SS, M:SS or H:MM:SS."""
    if seconds <= 0 or seconds != seconds or seconds == float('inf'):
        return '—'
    s = int(round(seconds))
    if s < 60:
        return f'0:{s:02d}'
    if s < 3600:
        return f'{s // 60}:{s % 60:02d}'
    return f'{s // 3600}:{(s % 3600) // 60:02d}:{s % 60:02d}'


class ProgressEta:
    """Estimate time remaining from progress in an eight-second sliding window."""

    def __init__(self):
        self._samples: deque[tuple[float, int]] = deque()

    def reset(self):
        self._samples.clear()

    def update(self, current: int, total: int, now: float | None = None) -> str:
        now = time.monotonic() if now is None else now
        if total <= 0 or (self._samples and current < self._samples[-1][1]):
            self.reset()
        if total <= 0:
            return ''
        self._samples.append((now, current))
        cutoff = now - 8.0
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()
        if len(self._samples) < 2:
            return ''
        t0, v0 = self._samples[0]
        dt = now - t0
        dv = current - v0
        if dt <= 0 or dv <= 0:
            return ''
        if current >= total:
            return 'ETA: done'
        return f'ETA: {format_eta((total - current) * dt / dv)}'
