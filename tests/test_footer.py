"""Tests for parsers.vk.footer.parse_archive_footer."""
from datetime import datetime

from vkdump.parsers.vk.footer import parse_archive_footer


def test_parse_ru_footer() -> None:
    text = "Архив был создан 1 янв 2020 в 12:34:56 за 1,234.56 секунд"
    html = f"""
    <html><body>
      <div class="footer">
        <div class="generated">{text}</div>
      </div>
    </body></html>
    """
    f = parse_archive_footer(html)
    assert f is not None
    assert f.lang == "ru"
    assert f.generated_at == datetime(2020, 1, 1, 12, 34, 56)
    assert f.duration_seconds == 1234.56
    assert f.raw_text == text


def test_parse_en_footer() -> None:
    """EN: 12-hour am/pm time, date after time with 'on'. Same shape as
    EN message-header dates in parsers/vk/dates.py."""
    html = (
        '<div class="generated">'
        'This data copy was created on at 1:34:56 pm on 1 Jan 2020'
        ' in 1,234.56 seconds'
        '</div>'
    )
    f = parse_archive_footer(html)
    assert f is not None
    assert f.lang == "en"
    assert f.generated_at == datetime(2020, 1, 1, 13, 34, 56)
    assert f.duration_seconds == 1234.56


def test_parse_en_footer_am_midnight() -> None:
    """12:00:00 am → 00:00:00; 12 pm → 12:00 (noon). Edge cases of the
    12-hour convention."""
    midnight = (
        '<div class="generated">'
        'This data copy was created on at 12:00:00 am on 1 Jan 2024'
        ' in 1.00 seconds'
        '</div>'
    )
    noon = (
        '<div class="generated">'
        'This data copy was created on at 12:30:00 pm on 1 Jan 2024'
        ' in 1.00 seconds'
        '</div>'
    )
    f1 = parse_archive_footer(midnight)
    f2 = parse_archive_footer(noon)
    assert f1 is not None and f1.generated_at == datetime(2024, 1, 1, 0, 0, 0)
    assert f2 is not None and f2.generated_at == datetime(2024, 1, 1, 12, 30, 0)


def test_unknown_when_div_present_but_text_unrecognised() -> None:
    html = '<div class="generated">something else entirely</div>'
    f = parse_archive_footer(html)
    assert f is not None
    assert f.lang == "unknown"
    assert f.generated_at is None
    assert f.duration_seconds is None
    assert f.raw_text == "something else entirely"


def test_returns_none_when_no_generated_div() -> None:
    assert parse_archive_footer("<html><body>nothing here</body></html>") is None


def test_handles_extra_attributes_on_div() -> None:
    html = (
        '<div class="generated" data-foo="bar">'
        'Архив был создан 1 янв 2020 в 00:00:00 за 0.50 секунд'
        '</div>'
    )
    f = parse_archive_footer(html)
    assert f is not None
    assert f.lang == "ru"
    assert f.generated_at == datetime(2020, 1, 1, 0, 0, 0)
    assert f.duration_seconds == 0.50
