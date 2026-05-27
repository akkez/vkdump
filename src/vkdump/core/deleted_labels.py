"""Recover human-readable labels for deleted VK users from in-message
mentions.

The VK export keeps mentions inline as `[id<vk_id>|<label>]`, where the
label is whatever the sender typed (the mentionee's name at the time,
a nickname, an `@handle`, etc.). For users that VK has since deleted,
the API returns nothing usable — but the historical mentions in chat
history often still contain a real name. This module scans message
texts, tallies labels per deleted user, and picks the best one.

Selection order (highest priority first), against trimmed labels with
count >= 1:
  1. `pair`     — exactly two whitespace-separated words, both starting
                  with an uppercase letter; the label as a whole does
                  not start with `@` or `*`.
  2. `at`       — starts with `@` but not `@id` (numeric VK handles are
                  no better than the id we already have).
  3. `single`   — a single word that doesn't start with `@` or `*` and
                  starts with an uppercase letter.
  4. `fallback` — none of the above matched; returns `"DELETED"`.

Within each bucket the most-frequent matching label wins; ties broken
lexicographically for determinism.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from typing import Iterable

_MENTION = re.compile(r"\[id(\d+)\|([^\]]+)\]")

Bucket = str  # one of: "pair", "at", "single", "fallback"


def _starts_with_upper(word: str) -> bool:
    """True if `word`'s first character is a Unicode uppercase letter
    (so Cyrillic / Greek / etc. work, not just ASCII)."""
    return bool(word) and unicodedata.category(word[0]).startswith("Lu")


class DeletedLabelPicker:
    """Stateful scanner. Feed it message texts, then ask for picks.

    Designed to be cheap to feed — just a regex per non-empty text — so
    you can stream millions of rows through it without buffering them.
    """

    def __init__(self, deleted_vk_ids: Iterable[int]) -> None:
        self._deleted: set[int] = set(deleted_vk_ids)
        self._counts: dict[int, Counter[str]] = defaultdict(Counter)

    def feed(self, text: str | None) -> None:
        """Index one message body. None / empty strings are ignored."""
        if not text:
            return
        for m in _MENTION.finditer(text):
            vk_id = int(m.group(1))
            if vk_id in self._deleted:
                self._counts[vk_id][m.group(2)] += 1

    def feed_many(self, texts: Iterable[str | None]) -> None:
        for t in texts:
            self.feed(t)

    def mentions(self, vk_id: int) -> Counter[str]:
        """Raw (untrimmed) mention counter for one user. Useful for
        diagnostics / dumps — the picker itself trims internally."""
        return self._counts.get(vk_id, Counter())

    def pick(self, vk_id: int) -> tuple[str, Bucket]:
        """Return `(best_label, bucket)` for one deleted user. Returns
        `("DELETED", "fallback")` when no rule matches (including when
        the user was never mentioned)."""
        ranked = self._ranked(vk_id)

        for label, _ in ranked:
            if label.startswith(("@", "*")):
                continue
            words = label.split()
            if (
                len(words) == 2
                and _starts_with_upper(words[0])
                and _starts_with_upper(words[1])
            ):
                return label, "pair"

        for label, _ in ranked:
            if label.startswith("@") and not label.startswith("@id"):
                return label, "at"

        for label, _ in ranked:
            if label.startswith(("@", "*")):
                continue
            words = label.split()
            if len(words) == 1 and _starts_with_upper(words[0]):
                return label, "single"

        return "DELETED", "fallback"

    def picks(self) -> dict[int, tuple[str, Bucket]]:
        """All picks for every vk_id registered in the constructor —
        including ones that were never mentioned (those fall back)."""
        return {vk_id: self.pick(vk_id) for vk_id in self._deleted}

    def _ranked(self, vk_id: int) -> list[tuple[str, int]]:
        """Trim labels (merging counts that only differed by whitespace)
        and return them sorted by frequency desc, label asc."""
        agg: Counter[str] = Counter()
        for label, n in self._counts.get(vk_id, Counter()).items():
            t = label.strip()
            if t:
                agg[t] += n
        return sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))
