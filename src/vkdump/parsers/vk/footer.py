"""Parse the VK archive footer — the one-line "generated" stamp at the
bottom of the root ``index.html``.

Two locale flavours surface (only RU has a real fixture today; EN is a
best-guess shape from VK's usual i18n style):

- RU: ``Архив был создан 25 мая 2026 в 18:41:09 за 1,333.23 секунд``
- EN: ``Archive was created 25 May 2026 at 18:41:09 in 1,333.23 seconds``

The duration carries an English-style thousands comma even in the RU
locale (the source data above is a real sample). It's stripped before
``float()``.

When neither pattern matches but a "generated"-class div was found, we
still return the raw text with ``lang='unknown'`` and the parsed fields
left None — the upstream archive row keeps the raw signature for human
inspection so we can iterate the regex when a new locale shows up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .dates import _MONTHS_EN, _MONTHS_RU


ArchiveLang = Literal["ru", "en", "unknown"]


@dataclass
class ArchiveFooter:
    lang: ArchiveLang
    generated_at: datetime | None
    duration_seconds: float | None
    raw_text: str | None


_FOOTER_BLOCK_RE = re.compile(
    r'<div\s+class="generated"[^>]*>(?P<body>[^<]+)</div>'
)

_RU_RE = re.compile(
    r"Архив\s+был\s+создан\s+"
    r"(?P<day>\d{1,2})\s+(?P<mon>[А-Яа-яЁё]+)\s+(?P<year>\d{4})\s+в\s+"
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})\s+за\s+"
    r"(?P<dur>[\d,.]+)\s+секунд"
)

_EN_RE = re.compile(
    r"Archive\s+was\s+created\s+"
    r"(?P<day>\d{1,2})\s+(?P<mon>[A-Za-z]+)\s+(?P<year>\d{4})\s+at\s+"
    r"(?P<h>\d{1,2}):(?P<m>\d{2}):(?P<s>\d{2})\s+in\s+"
    r"(?P<dur>[\d,.]+)\s+seconds",
    re.IGNORECASE,
)


def parse_archive_footer(html: str) -> ArchiveFooter | None:
    """Extract the footer signature from a root ``index.html``.

    Returns None when no ``<div class="generated">`` exists in the page —
    that file probably isn't an archive root. Returns an ``ArchiveFooter``
    with ``lang='unknown'`` and None fields when the div is present but
    the text doesn't match any known shape.
    """
    m_block = _FOOTER_BLOCK_RE.search(html)
    if m_block is None:
        return None
    raw = m_block.group("body").strip()

    m_ru = _RU_RE.search(raw)
    if m_ru:
        return ArchiveFooter(
            lang="ru",
            generated_at=_to_datetime(m_ru, _MONTHS_RU, raw),
            duration_seconds=_to_seconds(m_ru.group("dur")),
            raw_text=raw,
        )

    m_en = _EN_RE.search(raw)
    if m_en:
        return ArchiveFooter(
            lang="en",
            generated_at=_to_datetime(m_en, _MONTHS_EN, raw),
            duration_seconds=_to_seconds(m_en.group("dur")),
            raw_text=raw,
        )

    return ArchiveFooter(
        lang="unknown",
        generated_at=None,
        duration_seconds=None,
        raw_text=raw,
    )


def _to_datetime(
    m: re.Match[str], months: dict[str, int], raw: str
) -> datetime | None:
    mon = months.get(m.group("mon").lower())
    if mon is None:
        return None
    try:
        return datetime(
            int(m.group("year")),
            mon,
            int(m.group("day")),
            int(m.group("h")),
            int(m.group("m")),
            int(m.group("s")),
        )
    except ValueError:
        return None


def _to_seconds(token: str) -> float | None:
    try:
        return float(token.replace(",", ""))
    except ValueError:
        return None
