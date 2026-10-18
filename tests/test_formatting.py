import pytest

from vkdump.core.formatting import ProgressEta, format_bytes, format_eta


@pytest.mark.parametrize('size,expected', [
    (0, '0 B'), (999, '999 B'), (1000, '1.00 KB'),
    (1_500_000, '1.50 MB'), (2_400_000_000, '2.40 GB'),
])
def test_decimal_size_units(size, expected):
    assert format_bytes(size) == expected


@pytest.mark.parametrize('seconds,expected', [(9, '0:09'), (70, '1:10'), (3670, '1:01:10')])
def test_eta_clock_format(seconds, expected):
    assert format_eta(seconds) == expected


def test_eta_handles_raw_byte_counts_beyond_qt_integer_range():
    estimate = ProgressEta()
    assert estimate.update(1_000_000_000, 5_000_000_000, now=0) == ''
    assert estimate.update(2_000_000_000, 5_000_000_000, now=2) == 'ETA: 0:06'
    assert estimate.update(5_000_000_000, 5_000_000_000, now=8) == 'ETA: done'


def test_eta_does_not_include_a_pause_or_progress_rollback():
    estimate = ProgressEta()
    estimate.update(100, 1000, now=0)
    assert estimate.update(200, 1000, now=1) == 'ETA: 0:08'
    estimate.reset()
    assert estimate.update(200, 1000, now=100) == ''
    assert estimate.update(300, 1000, now=101) == 'ETA: 0:07'
    assert estimate.update(100, 1000, now=102) == ''
    assert estimate.update(200, 1000, now=103) == 'ETA: 0:08'


def test_eta_is_unknown_without_progress_or_a_known_total():
    estimate = ProgressEta()
    assert estimate.update(0, 0, now=0) == ''
    assert estimate.update(0, 100, now=1) == ''
    assert estimate.update(0, 100, now=2) == ''
