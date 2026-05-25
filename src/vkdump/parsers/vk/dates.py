"""Parse the dates VK renders in its dump pages.

Two locale flavours show up in the wild:

- Russian: `2 фев 2020 в 22:46:22`           (day-first, 24h, "в" separator)
- English: `at 7:15:40 pm on 3 Nov 2014`     (12h with am/pm, "on" separator)

Both reduce to a naive datetime — the timezone they're in is recorded
separately on the chat (`chats.source_timezone`).
"""
from datetime import datetime
import re

# Russian: 3-letter abbreviations except May which appears in genitive
# form ("мая") in some headers.
_MONTHS_RU: dict[str, int] = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5,
    "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
}
_MONTHS_EN: dict[str, int] = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# `at 7:15:40 pm on 3 Nov 2014` — am/pm optional, but VK always emits it.
_EN_DATE_RE = re.compile(
    r"^\s*at\s+(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})"
    r"\s+(?P<ap>am|pm)\s+on\s+"
    r"(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]{3,4})\s+(?P<year>\d{4})\s*$",
    re.IGNORECASE,
)


class DateParseError(ValueError):
    pass


def parse_vk_datetime(s: str) -> datetime:
    """Parse a VK header date in either locale into a naive datetime.

    Raises DateParseError if the input does not match any known layout.
    """
    stripped = s.strip()
    if stripped.lower().startswith("at "):
        return _parse_en(stripped)
    return _parse_ru(stripped)


def _parse_ru(s: str) -> datetime:
    parts = s.split()
    if len(parts) < 5:
        raise DateParseError(f"too few tokens: {s!r}")
    try:
        day = int(parts[0])
        month_token = parts[1].lower()
        year = int(parts[2])
        # parts[3] is "в"
        h, m, sec = parts[4].split(":")
    except (ValueError, IndexError) as e:
        raise DateParseError(f"unparseable date {s!r}: {e}") from e
    month = _MONTHS_RU.get(month_token)
    if month is None:
        raise DateParseError(f"unknown month {month_token!r} in {s!r}")
    try:
        return datetime(year, month, day, int(h), int(m), int(sec))
    except ValueError as e:
        raise DateParseError(f"out-of-range date in {s!r}: {e}") from e


def _parse_en(s: str) -> datetime:
    m = _EN_DATE_RE.match(s)
    if not m:
        raise DateParseError(f"unparseable english date {s!r}")
    month = _MONTHS_EN.get(m.group("mon").lower())
    if month is None:
        raise DateParseError(f"unknown month {m.group('mon')!r} in {s!r}")
    hour = int(m.group("h"))
    if m.group("ap").lower() == "pm" and hour != 12:
        hour += 12
    elif m.group("ap").lower() == "am" and hour == 12:
        hour = 0
    try:
        return datetime(
            int(m.group("year")), month, int(m.group("day")),
            hour, int(m.group("m")), int(m.group("s")),
        )
    except ValueError as e:
        raise DateParseError(f"out-of-range date in {s!r}: {e}") from e
