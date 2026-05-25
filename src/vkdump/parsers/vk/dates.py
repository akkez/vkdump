"""Parse Russian short-month dates as rendered in VK dumps."""
from datetime import datetime

# VK dumps print dates like "2 фев 2020 в 22:46:22". Months are 3-letter
# abbreviations except for May which appears in genitive form ("мая").
_MONTHS: dict[str, int] = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "май": 5,
    "мая": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
}


class DateParseError(ValueError):
    pass


def parse_vk_datetime(s: str) -> datetime:
    """Parse a string like '2 фев 2020 в 22:46:22' into a datetime.

    Raises DateParseError if the input does not match the expected layout.
    """
    # Layout: "<day> <month> <year> в <h>:<m>:<s>". Whitespace is single-space
    # in the dump but we still split defensively.
    parts = s.strip().split()
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
    month = _MONTHS.get(month_token)
    if month is None:
        raise DateParseError(f"unknown month {month_token!r} in {s!r}")
    try:
        return datetime(year, month, day, int(h), int(m), int(sec))
    except ValueError as e:
        raise DateParseError(f"out-of-range date in {s!r}: {e}") from e
