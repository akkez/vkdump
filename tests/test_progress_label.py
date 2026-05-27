"""AeroProgressGroup label behaviour across its lifecycle.

Verifies the bar text stays usable even when the module hasn't reported
a real total yet — Qt's QProgressBar collapses `text()` to "" when
min==max, so the group has to fake a (0,1) range and switch the format
to a flat "0%" until a real maximum lands.
"""
from __future__ import annotations

import os

import pytest

# Headless Qt so the test runs in CI without a display server.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from vkdump.gui.widgets.aero_progress import AeroProgressGroup  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


def _make() -> AeroProgressGroup:
    g = AeroProgressGroup()
    g.setFormat("%v / %m (%p%)")
    return g


def test_placeholder_when_no_total(qapp: QApplication) -> None:
    """Before any progress: range zero -> flat '0%' label, no '/ 0' noise."""
    g = _make()
    g.setRange(0, 0)
    g.setValue(0)
    assert g._bar.text() == "0%"


def test_verbose_when_real_total(qapp: QApplication) -> None:
    """Once a real total arrives, the verbose template kicks in."""
    g = _make()
    g.setRange(0, 100)
    g.setValue(0)
    assert g._bar.text() == "0 / 100 (0%)"
    g.setValue(42)
    assert g._bar.text() == "42 / 100 (42%)"


def test_returns_to_placeholder_after_reset(qapp: QApplication) -> None:
    """reset() + zero-range should flip back to the '0%' placeholder."""
    g = _make()
    g.setRange(0, 100)
    g.setValue(50)
    assert "%" in g._bar.text() and "/" in g._bar.text()
    g.reset()
    g.setRange(0, 0)
    g.setValue(0)
    assert g._bar.text() == "0%"


def test_negative_total_is_treated_as_no_total(qapp: QApplication) -> None:
    """A negative/garbage total should not produce '-1 / -1 (0%)' or similar."""
    g = _make()
    g.setRange(0, -5)
    g.setValue(0)
    assert g._bar.text() == "0%"
