"""Tests for the single-row sender-nav pagination in `photos_page.py`.

Layout:

    [everyone (N)] [<<< prev (X)] <30 chips> [next (Y) >>>]

Navigation rules:
- Pager arrows jump to the FIRST sender of the adjacent window
  (so each click loads at most one sender's photos, never the full
  everyone view in the middle of pagination).
- 'prev' from the leftmost sender window goes back to `index.html`
  (the only everyone view).
- 'everyone' link always points to `index.html`.

No DB contact — `_render_sender_nav` consumes a pre-built `_Sender`
list. Synthetic 2-letter placeholder names.
"""
from __future__ import annotations

from vkdump.modules.save_chat.photos_page import (
    _NAV_PAGE_SIZE,
    _Sender,
    _nav_page_count,
    _nav_page_for_sender,
    _render_sender_nav,
)


def _mk_senders(n: int) -> list[_Sender]:
    return [
        _Sender(vk_id=i, display_name=f"Aa B{i:02d}",
                count=1, slug=f"aa-{i}")
        for i in range(1, n + 1)
    ]


# ---------- pure helpers ----------


def test_nav_page_count_at_boundaries() -> None:
    assert _nav_page_count(0) == 1
    assert _nav_page_count(1) == 1
    assert _nav_page_count(_NAV_PAGE_SIZE) == 1
    assert _nav_page_count(_NAV_PAGE_SIZE + 1) == 2
    assert _nav_page_count(_NAV_PAGE_SIZE * 3) == 3
    assert _nav_page_count(_NAV_PAGE_SIZE * 3 + 1) == 4


def test_nav_page_for_sender_picks_correct_window() -> None:
    senders = _mk_senders(75)
    assert _nav_page_for_sender(senders, senders[0]) == 0
    assert _nav_page_for_sender(senders, senders[29]) == 0
    assert _nav_page_for_sender(senders, senders[30]) == 1
    assert _nav_page_for_sender(senders, senders[74]) == 2


# ---------- _render_sender_nav HTML output ----------


def test_single_window_no_pager_arrows() -> None:
    """Chats that fit in one window emit only everyone + chips."""
    html = _render_sender_nav(_mk_senders(5), current=None,
                              total_photos=10, nav_page_idx=0)
    assert "everyone-chip" in html
    assert "pager-arrow" not in html
    # All chips visible (no pagination needed).
    assert 'href="aa-1.html"' in html
    assert 'href="aa-5.html"' in html


def test_everyone_view_prev_disabled_next_to_first_sender_of_window_1() -> None:
    """On `index.html` (current=None, window 0) prev is disabled
    because nothing sits left of the first window. Next jumps to the
    first sender of window 1 (sender at index 30 — slug `aa-31`)."""
    html = _render_sender_nav(_mk_senders(75), current=None,
                              total_photos=100, nav_page_idx=0)
    # Prev disabled, with a 0 counter.
    assert 'class="pager-arrow disabled"' in html
    assert "prev (0)" in html
    # Next link points at the first sender of window 1.
    assert 'class="pager-arrow" href="aa-31.html"' in html
    # 45 senders sit on windows past 0 (75 - 30 = 45).
    assert "next (45)" in html


def test_middle_window_prev_jumps_to_first_sender_of_previous_window() -> None:
    """On a sender in window 2, prev goes to first sender of window 1
    (slug `aa-31`), NOT back to `index.html`."""
    senders = _mk_senders(75)
    target = senders[65]  # index 65 → window 2
    html = _render_sender_nav(senders, current=target,
                              total_photos=1, nav_page_idx=2)
    assert 'class="pager-arrow" href="aa-31.html"' in html
    assert "prev (60)" in html
    # Window 2 is the last — next disabled.
    assert "next (0)" in html
    assert 'class="pager-arrow disabled">next' in html


def test_window_1_prev_goes_back_to_index_html() -> None:
    """Specifically: from window 1 (the one right after everyone),
    prev returns to `index.html` — the everyone view — rather than
    to some `everyone-K.html`. Each sender file is the only artefact
    holding window-K context."""
    senders = _mk_senders(75)
    target = senders[40]  # window 1
    html = _render_sender_nav(senders, current=target,
                              total_photos=1, nav_page_idx=1)
    assert 'class="pager-arrow" href="index.html"' in html
    assert "prev (30)" in html
    # 15 senders past window 1 (75 - 60).
    assert "next (15)" in html
    assert 'class="pager-arrow" href="aa-61.html"' in html


def test_everyone_chip_links_to_index_html_on_every_page() -> None:
    """No matter which window is active, the everyone chip points at
    `index.html` so a single click always returns to the unfiltered
    view."""
    senders = _mk_senders(75)
    html = _render_sender_nav(senders, current=senders[65],
                              total_photos=1, nav_page_idx=2)
    assert 'class="everyone-chip" href="index.html"' in html
    # On a sender page the everyone chip is NOT marked active.
    assert ' class="active" class="everyone-chip"' not in html


def test_everyone_chip_active_on_index() -> None:
    html = _render_sender_nav(_mk_senders(75), current=None,
                              total_photos=100, nav_page_idx=0)
    assert 'class="active" class="everyone-chip"' in html


def test_only_window_senders_render_as_chips() -> None:
    """Senders outside the current window don't appear as chips —
    only as pager targets (which carry `class="pager-arrow"`)."""
    senders = _mk_senders(75)
    html = _render_sender_nav(senders, current=senders[40],
                              total_photos=1, nav_page_idx=1)
    # Window 1 = senders index 30..59 → slugs aa-31..aa-60.
    assert 'href="aa-31.html"' in html
    assert 'href="aa-60.html"' in html
    # Window 0's senders are not chips; aa-1 doesn't appear at all
    # (window 0's pager target is `index.html`, not a sender slug).
    assert 'href="aa-1.html"' not in html
    # aa-61 is the next-pager target only — not a chip. Distinguish
    # by the pager-arrow class on its anchor.
    assert 'class="pager-arrow" href="aa-61.html"' in html
    # Anchor without pager-arrow class on aa-61 would be a chip; not here.
    assert html.count('href="aa-61.html"') == 1
