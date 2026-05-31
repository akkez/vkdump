"""Recover human-readable labels for deleted VK users.

Two signal sources are fused into one picker:

1. **Inline mentions** — VK exports keep mentions as `[id<vk_id>|<label>]`
   in message bodies. For users VK has since deleted, the API returns
   nothing usable, but mentions in history often still carry a real name.
2. **Chat-event payloads** — service messages (joined / left / invited /
   kicked / renamed / …) parsed into ``attachments.kind = 'chat_event'``.
   The kludges link is typically already rewritten to "DELETED" by the
   time the dump is generated, but the plain-text ``fallback_text`` —
   the body VK rendered *outside* the kludges block — usually preserves
   the real name from when the event happened. This is a much higher-
   authority source: VK itself produced it from the actor's profile at
   event time, no user typing involved.

Picking order (highest priority first):
  1. `event`    — name harvested from a chat_event payload (actor /
                  target ``display_name`` when not "DELETED", or the
                  actor/target name extracted from ``fallback_text``).
                  Among multiple event-sourced names, a "First Last"
                  pair wins; otherwise the most-frequent label.
  2. `pair`     — inline mention: two whitespace-separated words, both
                  starting with an uppercase letter; not starting with
                  `@` or `*`.
  3. `at`       — inline mention: starts with `@` but not `@id`
                  (numeric VK handles are no better than the id itself).
  4. `single`   — inline mention: one word starting with an uppercase
                  letter; not starting with `@` or `*`.
  5. `fallback` — nothing matched; returns `"DELETED"`.

Within each bucket the most-frequent matching label wins; ties broken
lexicographically for determinism.
"""
from __future__ import annotations

import re
import unicodedata
from collections import Counter, defaultdict
from typing import Any, Iterable

_MENTION = re.compile(r"\[id(\d+)\|([^\]]+)\]")

Bucket = str  # one of: "event", "pair", "at", "single", "fallback"


# Per-subtype verb-phrase patterns used to split ``fallback_text`` into
# ``(before, after)`` around the verb. ``has_target`` says whether the
# subtype renders a second user name after the verb (invite / kick).
#
# These intentionally cast a wider net than the canonical parser tables in
# ``parsers/vk/chat_events.py``: older VK exports rendered the same events
# in plain text with phrasings the kludges parser doesn't have to handle
# ("покинул беседу" vs. canonical "вышел из чата"). Subtypes whose
# fallback_text is uniformly the placeholder "Сообщение не поддерживается
# Вашим приложением." (``join_by_link``, ``message_pin``, ``message_unpin``,
# ``call_start``) are intentionally absent — nothing to extract.
_FB_VERB_PATTERNS: dict[str, tuple[re.Pattern[str], bool]] = {
    "leave": (
        re.compile(r"\s+(?:покинул(?:а)?|вышел|вышла|left)\b"),
        False,
    ),
    "rejoin": (
        re.compile(r"\s+(?:вернул(?:ся|ась)|returned)\b"),
        False,
    ),
    "invite": (
        re.compile(r"\s+(?:пригласил(?:а)?|invited)\s+"),
        True,
    ),
    "kick": (
        re.compile(r"\s+(?:исключил(?:а)?|kicked|removed)\s+"),
        True,
    ),
    "chat_create": (
        re.compile(
            r"\s+(?:создал(?:а)?\s+(?:беседу|чат)"
            r"|created\s+(?:the\s+)?(?:chat|conversation))\b"
        ),
        False,
    ),
    "chat_rename": (
        re.compile(
            r"\s+(?:измени(?:л|ла)\s+название"
            r"|renamed\s+the\s+(?:chat|conversation)"
            r"|changed\s+(?:the\s+)?(?:chat|conversation)\s+name)\b"
        ),
        False,
    ),
    "chat_photo_set": (
        re.compile(
            r"\s+(?:обновил(?:а)?\s+фотограф\w*"
            r"|(?:updated|changed)\s+(?:the\s+)?(?:chat|conversation)\s+(?:photo|cover))\b"
        ),
        False,
    ),
    "chat_photo_removed": (
        re.compile(
            r"\s+(?:удалил(?:а)?\s+фотограф\w*"
            r"|removed\s+(?:the\s+)?(?:chat|conversation)\s+(?:photo|cover))\b"
        ),
        False,
    ),
    "chat_theme_change": (
        re.compile(
            r"\s+(?:измени(?:л|ла)\s+оформление"
            r"|changed\s+(?:the\s+)?(?:chat|conversation)\s+theme)\b"
        ),
        False,
    ),
    "chat_theme_reset": (
        re.compile(
            r"\s+(?:сброси(?:л|ла)\s+оформление"
            r"|reset\s+(?:the\s+)?(?:chat|conversation)\s+theme)\b"
        ),
        False,
    ),
    "screenshot": (
        re.compile(
            r"\s+(?:сделал(?:а)?\s+скриншот"
            r"|took\s+a\s+(?:chat|conversation)\s+screenshot)\b"
        ),
        False,
    ),
}


def _starts_with_upper(word: str) -> bool:
    """True if `word`'s first character is a Unicode uppercase letter
    (so Cyrillic / Greek / etc. work, not just ASCII)."""
    return bool(word) and unicodedata.category(word[0]).startswith("Lu")


def _is_real_label(label: str) -> bool:
    """False for placeholders we don't want to count as a recovered
    name: the literal ``"DELETED"`` and re-runs of our own bracketed
    output (``"DELETED (...)"``). Everything else passes through —
    bucket-specific filtering happens later in :meth:`pick_multi`.
    """
    if not label:
        return False
    if label == "DELETED":
        return False
    if label.startswith("DELETED (") or label.startswith("DELETED("):
        return False
    return True


def format_combined_label(label: str | None) -> str:
    """Render a recovered name into the canonical ``users.display_name``
    form: ``"DELETED (Name)"`` when ``label`` is a real recovered name,
    or the bare ``"DELETED"`` when it is None / empty / itself the
    ``DELETED`` literal. ``DELETED`` stays as the leading token in
    either case so existing call sites that filter on that prefix keep
    working.
    """
    if not label or label == "DELETED" or label.startswith("DELETED ("):
        return "DELETED"
    return f"DELETED ({label})"


class DeletedLabelPicker:
    """Stateful scanner. Feed it message texts and chat_event payloads,
    then ask for picks.

    Designed to be cheap to feed — a regex per non-empty text, a dict
    lookup per chat_event — so callers can stream millions of rows
    without buffering them.
    """

    def __init__(self, deleted_vk_ids: Iterable[int]) -> None:
        self._deleted: set[int] = set(deleted_vk_ids)
        self._counts: dict[int, Counter[str]] = defaultdict(Counter)
        self._event_counts: dict[int, Counter[str]] = defaultdict(Counter)

    def feed(self, text: str | None) -> None:
        """Index one message body for inline `[id|label]` mentions.
        None / empty strings are ignored."""
        if not text:
            return
        for m in _MENTION.finditer(text):
            vk_id = int(m.group(1))
            if vk_id not in self._deleted:
                continue
            label = m.group(2)
            if not _is_real_label(label):
                continue
            self._counts[vk_id][label] += 1

    def feed_many(self, texts: Iterable[str | None]) -> None:
        for t in texts:
            self.feed(t)

    def feed_chat_event(self, payload: dict[str, Any]) -> None:
        """Index actor/target names from a parsed chat_event payload.

        Pulls names from two places in priority order, with each
        occurrence counted once:
        1. ``actor.display_name`` and ``target.display_name`` when not
           equal to ``"DELETED"`` (VK still had the name at dump time).
        2. ``fallback_text`` split around the subtype's verb phrase
           (the plain-text body, which VK often renders from older
           cached data and keeps the real name).

        Payloads with no recognised verb (``message_pin``,
        ``join_by_link``, …) or unrelated to any deleted user are
        ignored cheaply.
        """
        for role in ("actor", "target"):
            u = payload.get(role)
            if not isinstance(u, dict):
                continue
            vid = u.get("vk_id")
            if vid is None or vid not in self._deleted:
                continue
            name = (u.get("display_name") or "").strip()
            if _is_real_label(name):
                self._event_counts[vid][name] += 1

        subtype = payload.get("subtype")
        fb_raw = payload.get("fallback_text")
        if not subtype or not fb_raw:
            return
        verb = _FB_VERB_PATTERNS.get(subtype)
        if verb is None:
            return
        verb_re, has_target = verb
        fb = fb_raw.strip()
        m = verb_re.search(fb)
        if not m:
            return
        actor_name = fb[: m.start()].strip()
        target_name = fb[m.end():].strip() if has_target else ""
        if subtype == "kick":
            target_name = re.sub(r"\s+out\s*$", "", target_name)

        actor = payload.get("actor") or {}
        actor_vid = actor.get("vk_id") if isinstance(actor, dict) else None
        if actor_vid in self._deleted and _is_real_label(actor_name):
            self._event_counts[actor_vid][actor_name] += 1

        if has_target:
            target = payload.get("target") or {}
            target_vid = target.get("vk_id") if isinstance(target, dict) else None
            if target_vid in self._deleted and _is_real_label(target_name):
                self._event_counts[target_vid][target_name] += 1

    def mentions(self, vk_id: int) -> Counter[str]:
        """Raw (untrimmed) inline-mention counter for one user. Useful
        for diagnostics / dumps — the picker itself trims internally."""
        return self._counts.get(vk_id, Counter())

    def event_mentions(self, vk_id: int) -> Counter[str]:
        """Raw chat_event-derived label counter for one user."""
        return self._event_counts.get(vk_id, Counter())

    def pick(self, vk_id: int) -> tuple[str, Bucket]:
        """Return `(best_label, bucket)` for one deleted user. Returns
        `("DELETED", "fallback")` when no rule matches (including when
        the user has no inline mention and no chat_event signal)."""
        event_ranked = self._ranked(self._event_counts.get(vk_id))
        for label, _ in event_ranked:
            words = label.split()
            if (
                len(words) >= 2
                and _starts_with_upper(words[0])
                and _starts_with_upper(words[1])
            ):
                return label, "event"
        if event_ranked:
            return event_ranked[0][0], "event"

        ranked = self._ranked(self._counts.get(vk_id))

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

    @staticmethod
    def _ranked(raw: Counter[str] | None) -> list[tuple[str, int]]:
        """Trim labels (merging counts that only differed by whitespace)
        and return them sorted by frequency desc, label asc."""
        if not raw:
            return []
        agg: Counter[str] = Counter()
        for label, n in raw.items():
            t = label.strip()
            if t:
                agg[t] += n
        return sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))
