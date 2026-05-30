"""Regression tests for the searchable combobox picker
(`_attach_searchable_combo`).

Original bug: typing text + arrowing through the popup + Enter
committed the *first filtered* item instead of the highlighted one,
because `QCompleter.currentCompletion()` does not track arrow-key
navigation in the popup — it stays pinned at the first match. The
handler used to read it on Enter; now it tracks the latest text
emitted by `completer.highlighted` and prefers that.

These tests bypass real keystrokes / popup widgets — they're
notoriously brittle in headless Qt — and instead drive the picker by
emitting the same signals the real popup would emit. The handlers
under test are signal slots, so emitting reproduces the production
path exactly.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QComboBox  # noqa: E402

from vkdump.gui.task_panel import _attach_searchable_combo  # noqa: E402


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return QApplication.instance() or QApplication([])


def _make_combo(n: int = 50) -> QComboBox:
    cb = QComboBox()
    for i in range(n):
        cb.addItem(f"Item {i}", f"id-{i}")
    _attach_searchable_combo(cb)
    return cb


def test_arrow_then_enter_picks_highlighted(qapp: QApplication) -> None:
    """The bug: arrow+Enter used to commit the first popup match
    regardless of which item was actually highlighted."""
    cb = _make_combo()
    line = cb.lineEdit()
    completer = cb.completer()
    assert line is not None and completer is not None

    # Simulate: user types "1" → popup auto-highlights first match.
    line.setText("1")
    line.textEdited.emit("1")
    # Simulate arrow-key navigation: completer emits highlighted as
    # the user moves through the popup.
    completer.highlighted.emit("Item 12")
    completer.highlighted.emit("Item 13")
    # User hits Enter.
    line.returnPressed.emit()

    assert cb.currentText() == "Item 13", (
        f"Expected the latest popup-highlighted item, got {cb.currentText()!r}"
    )
    assert cb.currentData() == "id-13"


def test_mouse_click_in_popup_picks_clicked(qapp: QApplication) -> None:
    """Mouse-click on a popup row fires `completer.activated` —
    nothing to do with our highlight tracking, just verify it still
    works end-to-end."""
    cb = _make_combo()
    completer = cb.completer()
    assert completer is not None

    completer.activated.emit("Item 7")

    assert cb.currentText() == "Item 7"
    assert cb.currentData() == "id-7"


def test_enter_with_typed_literal_no_highlight(qapp: QApplication) -> None:
    """User pastes a full item label and hits Enter without the popup
    ever showing — the literal-text fallback resolves it."""
    cb = _make_combo()
    line = cb.lineEdit()
    assert line is not None

    line.setText("Item 42")
    # No textEdited/highlighted — they didn't actually type interactively.
    line.returnPressed.emit()

    assert cb.currentText() == "Item 42"
    assert cb.currentData() == "id-42"


def test_focus_out_with_garbage_snaps_back(qapp: QApplication) -> None:
    """`editingFinished` reverts the visible text only when it doesn't
    match any item exactly. Successful picks must not trigger snap-back."""
    cb = _make_combo()
    line = cb.lineEdit()
    assert line is not None

    # Land on a valid item first.
    cb.setCurrentIndex(5)
    line.setText("Item 5")

    # Now type garbage and lose focus.
    line.setText("not a real item")
    line.editingFinished.emit()

    # Index stays put; visible text snaps back to the current item.
    assert cb.currentIndex() == 5
    assert line.text() == "Item 5"


def test_focus_out_with_valid_text_does_not_snap(qapp: QApplication) -> None:
    """Counter-test for the snap-back: if the visible text *does* match
    an item exactly (because the user just picked it), editingFinished
    must leave everything alone."""
    cb = _make_combo()
    line = cb.lineEdit()
    completer = cb.completer()
    assert line is not None and completer is not None

    completer.highlighted.emit("Item 22")
    line.returnPressed.emit()

    line.editingFinished.emit()

    assert cb.currentText() == "Item 22"
    assert cb.currentData() == "id-22"


def test_text_edited_clears_stale_highlight(qapp: QApplication) -> None:
    """If the user highlights one item, then starts typing again, the
    stale highlight must not leak into the next Enter — fresh popup
    sessions start clean."""
    cb = _make_combo(100)
    line = cb.lineEdit()
    completer = cb.completer()
    assert line is not None and completer is not None

    # First popup session: highlight a thing but don't commit.
    completer.highlighted.emit("Item 33")

    # User starts a new filter — that has to wipe last_highlighted.
    line.setText("99")
    line.textEdited.emit("99")
    line.setText("Item 99")
    line.returnPressed.emit()

    # Stale "Item 33" must NOT have been picked.
    assert cb.currentText() == "Item 99"
    assert cb.currentData() == "id-99"
