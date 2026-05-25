"""Date parsing — every shape VK rendered in the wild, both locales.

All sample timestamps below are synthetic; they exist only to exercise
the regex / 12-to-24-hour conversion logic.
"""
from datetime import datetime

import pytest

from vkdump.parsers.vk.dates import DateParseError, parse_vk_datetime


# ---------- Russian ----------

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1 янв 2020 в 0:00:00",  datetime(2020, 1, 1, 0, 0, 0)),
        ("2 фев 2020 в 22:46:22", datetime(2020, 2, 2, 22, 46, 22)),
        ("3 мар 2021 в 9:05:01",  datetime(2021, 3, 3, 9, 5, 1)),
        ("4 апр 2022 в 23:59:59", datetime(2022, 4, 4, 23, 59, 59)),
        # "May" lives as both "май" and "мая" in different headers.
        ("5 май 2023 в 12:30:00", datetime(2023, 5, 5, 12, 30, 0)),
        ("5 мая 2023 в 12:30:00", datetime(2023, 5, 5, 12, 30, 0)),
        ("6 июн 2024 в 1:23:45",  datetime(2024, 6, 6, 1, 23, 45)),
        ("7 июл 2024 в 18:00:00", datetime(2024, 7, 7, 18, 0, 0)),
        ("8 авг 2024 в 20:00:00", datetime(2024, 8, 8, 20, 0, 0)),
        ("9 сен 2024 в 21:00:00", datetime(2024, 9, 9, 21, 0, 0)),
        ("10 окт 2024 в 22:00:00", datetime(2024, 10, 10, 22, 0, 0)),
        ("11 ноя 2024 в 23:00:00", datetime(2024, 11, 11, 23, 0, 0)),
        ("12 дек 2024 в 00:00:00", datetime(2024, 12, 12, 0, 0, 0)),
        # Single-digit day at month boundary.
        ("1 янв 1970 в 0:00:00", datetime(1970, 1, 1, 0, 0, 0)),
    ],
)
def test_parse_russian(raw: str, expected: datetime) -> None:
    assert parse_vk_datetime(raw) == expected


# ---------- English ----------

@pytest.mark.parametrize(
    "raw, expected",
    [
        # Standard pm / am / 12 wrap-around — the bugs we actually saw.
        ("at 7:15:40 pm on 3 Nov 2014",   datetime(2014, 11, 3, 19, 15, 40)),
        ("at 1:00:32 am on 10 Mar 2015",  datetime(2015, 3, 10, 1, 0, 32)),
        ("at 5:54:20 pm on 10 Jul 2018",  datetime(2018, 7, 10, 17, 54, 20)),
        ("at 11:05:00 am on 3 Apr 2025",  datetime(2025, 4, 3, 11, 5, 0)),
        # 12-hour edge cases: 12 am → 00:xx, 12 pm → 12:xx.
        ("at 12:30:00 am on 1 Jan 2020",  datetime(2020, 1, 1, 0, 30, 0)),
        ("at 12:30:00 pm on 1 Jan 2020",  datetime(2020, 1, 1, 12, 30, 0)),
        # Two-digit day, single-digit hour.
        ("at 9:09:09 pm on 29 Feb 2024",  datetime(2024, 2, 29, 21, 9, 9)),
        # All months exist.
        ("at 1:00:00 pm on 15 Jan 2020",  datetime(2020, 1, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 Feb 2020",  datetime(2020, 2, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 Mar 2020",  datetime(2020, 3, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 Apr 2020",  datetime(2020, 4, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 May 2020",  datetime(2020, 5, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 Jun 2020",  datetime(2020, 6, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 Jul 2020",  datetime(2020, 7, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 Aug 2020",  datetime(2020, 8, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 Sep 2020",  datetime(2020, 9, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 Oct 2020",  datetime(2020, 10, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 Nov 2020",  datetime(2020, 11, 15, 13, 0, 0)),
        ("at 1:00:00 pm on 15 Dec 2020",  datetime(2020, 12, 15, 13, 0, 0)),
    ],
)
def test_parse_english(raw: str, expected: datetime) -> None:
    assert parse_vk_datetime(raw) == expected


# ---------- failure modes ----------

@pytest.mark.parametrize(
    "raw",
    [
        "",
        "garbage",
        "32 фев 2020 в 0:00:00",          # day out of range
        "1 zzz 2020 в 0:00:00",            # unknown month token
        "at 25:00:00 am on 1 Jan 2020",    # hour out of range
        "at 1:00:00 zz on 1 Jan 2020",     # am/pm token missing
    ],
)
def test_parse_raises(raw: str) -> None:
    with pytest.raises(DateParseError):
        parse_vk_datetime(raw)
