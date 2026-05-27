"""Win7-Aero-styled progress bar with status + auto-ETA labels."""
from __future__ import annotations

import time
from collections import deque

from PySide6.QtCore import Property, QPropertyAnimation, QRectF, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import (
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


class AeroProgressBar(QProgressBar):
    """Custom-painted glowing green bar with an animated shimmer band."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shimmer = -0.3
        self.setMinimumHeight(24)
        self.setTextVisible(True)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._anim = QPropertyAnimation(self, b"shimmerPos", self)
        self._anim.setStartValue(-0.3)
        self._anim.setEndValue(1.3)
        self._anim.setDuration(1800)
        self._anim.setLoopCount(-1)
        self._anim.start()

        glow = QGraphicsDropShadowEffect(self)
        glow.setBlurRadius(22)
        glow.setColor(QColor(60, 255, 90, 200))
        glow.setOffset(0, 0)
        self.setGraphicsEffect(glow)

    def _get_shimmer(self) -> float:
        return self._shimmer

    def _set_shimmer(self, v: float) -> None:
        self._shimmer = v
        self.update()

    shimmerPos = Property(float, _get_shimmer, _set_shimmer)

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)

        bg = QLinearGradient(0, rect.top(), 0, rect.bottom())
        bg.setColorAt(0.0, QColor("#0d0d0d"))
        bg.setColorAt(1.0, QColor("#222222"))
        p.setBrush(bg)
        p.setPen(QColor("#000000"))
        p.drawRoundedRect(rect, 5, 5)

        span = max(1, self.maximum() - self.minimum())
        ratio = (self.value() - self.minimum()) / span
        fill_w = (rect.width() - 2) * ratio
        if fill_w > 0:
            fill = QRectF(rect.left() + 1, rect.top() + 1, fill_w, rect.height() - 2)
            p.setClipRect(fill)

            body = QLinearGradient(0, fill.top(), 0, fill.bottom())
            body.setColorAt(0.00, QColor("#5cff7e"))
            body.setColorAt(0.45, QColor("#1cd026"))
            body.setColorAt(0.55, QColor("#0e9216"))
            body.setColorAt(1.00, QColor("#2bff55"))
            p.setBrush(body)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(fill, 4, 4)

            sheen_rect = QRectF(fill.left(), fill.top(), fill.width(), fill.height() / 2)
            sheen = QLinearGradient(0, sheen_rect.top(), 0, sheen_rect.bottom())
            sheen.setColorAt(0.0, QColor(255, 255, 255, 140))
            sheen.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setBrush(sheen)
            p.drawRoundedRect(sheen_rect, 4, 4)

            band_w = fill.width() * 0.35
            band_x = fill.left() + fill.width() * self._shimmer - band_w / 2
            band = QLinearGradient(band_x, 0, band_x + band_w, 0)
            band.setColorAt(0.0, QColor(255, 255, 255, 0))
            band.setColorAt(0.5, QColor(255, 255, 255, 170))
            band.setColorAt(1.0, QColor(255, 255, 255, 0))
            p.setBrush(band)
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(fill, 4, 4)

            p.setClipping(False)

        if self.isTextVisible():
            p.setPen(QColor("#ffffff"))
            p.drawText(self.rect(), Qt.AlignCenter, self.text())


def _format_eta(seconds: float) -> str:
    """Format seconds as `0:SS`, `M:SS` or `H:MM:SS`."""
    if seconds <= 0 or seconds != seconds or seconds == float("inf"):
        return "—"
    s = int(round(seconds))
    if s < 60:
        return f"0:{s:02d}"
    if s < 3600:
        return f"{s // 60}:{s % 60:02d}"
    h = s // 3600
    m = (s % 3600) // 60
    sec = s % 60
    return f"{h}:{m:02d}:{sec:02d}"


class AeroProgressGroup(QWidget):
    """AeroProgressBar with a left status label and a right auto-ETA label."""

    _WINDOW_SECONDS = 8.0

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self._bar = AeroProgressBar(self)
        self._status = QLabel("")
        self._eta = QLabel("")
        self._eta.setVisible(False)
        self._status.setStyleSheet("color: #cfcfcf; font-size: 13px;")
        self._eta.setStyleSheet("color: #9ad19f; font-size: 13px; font-weight: 600;")
        self._eta.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(5)
        row = QHBoxLayout()
        row.setContentsMargins(2, 0, 2, 0)
        row.addWidget(self._status, 1)
        row.addWidget(self._eta, 0)
        root.addLayout(row)
        root.addWidget(self._bar)

        self._samples: deque[tuple[float, int]] = deque()

    def setRange(self, minimum: int, maximum: int) -> None:
        # `_on_progress` re-emits setRange every tick with the same args
        # for known-total tasks. Resetting unconditionally would wipe the
        # ETA sample window and keep `remaining/rate` at 1-sample forever.
        if (minimum, maximum) != (self._bar.minimum(), self._bar.maximum()):
            self._reset_eta_state()
        self._bar.setRange(minimum, maximum)

    def setValue(self, value: int) -> None:
        now = time.monotonic()
        prev = self._bar.value()
        self._bar.setValue(value)
        if value < prev:
            self._reset_eta_state()
            return
        self._samples.append((now, value))
        cutoff = now - self._WINDOW_SECONDS
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()
        self._recompute_eta()

    def setFormat(self, fmt: str) -> None:
        self._bar.setFormat(fmt)

    def setStatus(self, text: str) -> None:
        self._status.setText(text)

    def reset(self) -> None:
        """Clear status, ETA history and value — called between runs."""
        self._status.setText("")
        self._reset_eta_state()
        self._bar.setValue(self._bar.minimum())

    def _reset_eta_state(self) -> None:
        self._samples.clear()
        self._clear_eta()

    def _clear_eta(self) -> None:
        self._eta.setText("")
        self._eta.setVisible(False)

    def _show_eta(self, text: str) -> None:
        self._eta.setText(text)
        self._eta.setVisible(True)

    def _recompute_eta(self) -> None:
        if len(self._samples) < 2:
            self._clear_eta()
            return
        t0, v0 = self._samples[0]
        t1, v1 = self._samples[-1]
        dt = t1 - t0
        dv = v1 - v0
        if dt <= 0 or dv <= 0:
            self._clear_eta()
            return
        rate = dv / dt
        remaining = self._bar.maximum() - v1
        if remaining <= 0:
            self._show_eta("ETA: done")
            return
        self._show_eta(f"ETA: {_format_eta(remaining / rate)}")
