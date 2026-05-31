"""Tests for parsers.vk.footer.parse_archive_footer."""
from datetime import datetime

from vkdump.parsers.vk.footer import parse_archive_footer


def test_parse_ru_footer() -> None:
    html = """
    <html><body>
      <div class="footer">
        <div class="generated">Архив был создан 25 мая 2026 в 18:41:09 за 1,333.23 секунд</div>
      </div>
    </body></html>
    """
    f = parse_archive_footer(html)
    assert f is not None
    assert f.lang == "ru"
    assert f.generated_at == datetime(2026, 5, 25, 18, 41, 9)
    assert f.duration_seconds == 1333.23
    assert f.raw_text == "Архив был создан 25 мая 2026 в 18:41:09 за 1,333.23 секунд"


def test_parse_en_footer() -> None:
    html = (
        '<div class="generated">'
        'Archive was created 25 May 2026 at 18:41:09 in 1,333.23 seconds'
        '</div>'
    )
    f = parse_archive_footer(html)
    assert f is not None
    assert f.lang == "en"
    assert f.generated_at == datetime(2026, 5, 25, 18, 41, 9)
    assert f.duration_seconds == 1333.23


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
