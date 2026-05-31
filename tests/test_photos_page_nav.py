"""Tests for the sender-nav pagination in `photos_page.py`.

Covers the pure helpers + the `_render_sender_nav` HTML output. No DB
contact — `_render_sender_nav` consumes a pre-built `_Sender` list,
so the tests fabricate the list with synthetic 2-letter placeholders.
"""
from __future__ import annotations

from vkdump.modules.save_chat.photos_page import (
    _NAV_PAGE_SIZE,
    _Sender,
    _everyone_filename,
    _nav_page_count,
    _nav_page_for_sender,
    _render_sender_nav,
)


def _mk_senders(n: int) -> list[_Sender]:
    """`n` synthetic senders with stable vk_ids 1..n and 2-letter
    placeholder names so the entropy scanner stays happy."""
    return [
        _Sender(vk_id=i, display_name=f"Aa B{i:02d}",
                count=1, slug=f"aa-{i}")
        for i in range(1, n + 1)
    ]


# ---------- pure helpers ----------


def test_everyone_filename_page0_is_index_html() -> None:
    assert _everyone_filename(0) == "index.html"


def test_everyone_filename_later_pages_numbered_from_2() -> None:
    assert _everyone_filename(1) == "everyone-2.html"
    assert _everyone_filename(4) == "everyone-5.html"


def test_nav_page_count_at_boundaries() -> None:
    assert _nav_page_count(0) == 1
    assert _nav_page_count(1) == 1
    assert _nav_page_count(_NAV_PAGE_SIZE) == 1
    assert _nav_page_count(_NAV_PAGE_SIZE + 1) == 2
    assert _nav_page_count(_NAV_PAGE_SIZE * 3) == 3
    assert _nav_page_count(_NAV_PAGE_SIZE * 3 + 1) == 4


def test_nav_page_for_sender_picks_correct_window() -> None:
    senders = _mk_senders(75)
    # Page size is 30, so:
    #   index 0  → page 0
    #   index 29 → page 0
    #   index 30 → page 1
    #   index 74 → page 2
    assert _nav_page_for_sender(senders, senders[0]) == 0
    assert _nav_page_for_sender(senders, senders[29]) == 0
    assert _nav_page_for_sender(senders, senders[30]) == 1
    assert _nav_page_for_sender(senders, senders[74]) == 2


# ---------- _render_sender_nav HTML output ----------


def test_render_sender_nav_single_page_omits_pager() -> None:
    """Chats with <= one window's worth of senders don't get pager
    controls — that block would be noise."""
    html = _render_sender_nav(_mk_senders(5), current=None,
                              total_photos=10, nav_page_idx=0)
    assert "sender-nav-everyone" in html
    assert ">everyone" in html
    assert "sender-pager" not in html


def test_render_sender_nav_multi_page_shows_pager_with_indicator() -> None:
    html = _render_sender_nav(_mk_senders(75), current=None,
                              total_photos=100, nav_page_idx=1)
    # Window 1 = senders 31..60. Chip presence is by href since the
    # display name is followed by a `<span class="count">` and not
    # immediately by `</a>`.
    assert 'href="aa-31.html"' in html
    assert 'href="aa-60.html"' in html
    # Senders outside the window aren't in this nav.
    assert 'href="aa-1.html"' not in html
    assert 'href="aa-61.html"' not in html
    # Pager indicator + both arrows present (neither edge of pagination).
    assert "page 2/3" in html
    assert 'href="index.html"' in html        # prev → page 0 = index.html
    assert 'href="everyone-3.html"' in html   # next → page 2


def test_render_sender_nav_first_page_disables_prev() -> None:
    html = _render_sender_nav(_mk_senders(75), current=None,
                              total_photos=100, nav_page_idx=0)
    assert 'class="pager-arrow disabled">← prev' in html
    # next is still a link.
    assert 'href="everyone-2.html"' in html


def test_render_sender_nav_last_page_disables_next() -> None:
    html = _render_sender_nav(_mk_senders(75), current=None,
                              total_photos=100, nav_page_idx=2)
    assert 'class="pager-arrow disabled">next →' in html
    # prev points back to page 1.
    assert 'href="everyone-2.html"' in html


def test_render_sender_nav_marks_current_sender_active() -> None:
    senders = _mk_senders(40)
    target = senders[35]  # in window 1
    html = _render_sender_nav(senders, current=target,
                              total_photos=1, nav_page_idx=1)
    # Active class on the current sender; everyone link is NOT active.
    assert 'class="active" href="aa-36.html"' in html
    # Everyone link present but unmarked when on a sender page.
    assert ' href="index.html">everyone' in html
    # Counts now reflect that we're on a sender page (sum across all
    # senders rather than total_photos).
    assert f">everyone <span class=\"count\">({sum(s.count for s in senders)})" in html


def test_render_sender_nav_window_indicator_reports_real_range() -> None:
    """For a tail window that isn't full (e.g. 5 senders on the last
    page), the indicator should say 'senders 31–35 of 35'."""
    html = _render_sender_nav(_mk_senders(35), current=None,
                              total_photos=10, nav_page_idx=1)
    assert "senders 31–35 of 35" in html
