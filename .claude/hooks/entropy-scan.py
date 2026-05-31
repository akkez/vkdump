#!/usr/bin/env python3
r"""PreToolUse entropy scanner for the vk-dump repo.

Blocks any Edit / Write / MultiEdit / Bash invocation whose payload
contains low-entropy text that looks like real data from the
maintainer's VK archive. Real values have telltale low entropy: a
specific recent year, a non-trivial timestamp, a thousands-comma
decimal — none of which appear in synthetic placeholders.

Detection heuristics (any match → exit 2, blocking the tool call):

- RU footer signature with a year > 2024 ("Архив был создан … 20XX в
  HH:MM:SS …"). The synthetic placeholder allowlist uses 2020.
- EN footer signature with a year > 2024 ("This data copy was created
  on at … 20XX in … seconds").
- Bare date literal: "DD MON YYYY" or "DD <ru-month> YYYY" where YYYY
  is non-synthetic — catches dates that landed in commit messages or
  comments without the full footer wrapper.
- Decimal duration of form `N,NNN.NN` (English thousands comma)
  outside the small synthetic allowlist (1,234.56 / 9,999.99).
- Cyrillic capitalised-word pair ≥3 letters each (e.g.
  `[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}`). Synthetic 2-letter
  placeholders like `Aa Bb` don't match.
- Numeric vk-id-looking strings: standalone 5-12 digit numbers
  outside the known VK chat-folder convention prefix `2000000`
  (which the codebase already references generically). Anything ≤4
  digits is treated as too low-entropy to bother with.

Synthetic allowlists:

- Years in dates: any of `2020`, `2024`.
- Durations: `1,234.56`, `9,999.99`.
- Ids: standalone numbers ≤4 digits are ignored; standalone
  2000000XXX (chat-folder format demo) is ignored.

Invocation: stdin = JSON hook payload (Claude Code spec). Reads
`tool_name` + `tool_input`. Block by exit 2 + stderr message.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


_SYNTH_YEARS = {"2020", "2024"}
_SYNTH_DURATIONS = {"1,234.56", "9,999.99"}

_RU_MONTHS = (
    r"янв(?:аря)?|фев(?:раля)?|мар(?:та)?|апр(?:еля)?|мая?|май|"
    r"июн(?:я)?|июл(?:я)?|авг(?:уста)?|сен(?:тября)?|окт(?:ября)?|"
    r"ноя(?:бря)?|дек(?:абря)?"
)
_EN_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|"
    r"nov(?:ember)?|dec(?:ember)?"
)

_RU_FOOTER_RE = re.compile(
    r"Архив\s+был\s+создан\s+\d{1,2}\s+\S+\s+(?P<year>\d{4})\s+в\s+\d{1,2}:\d{2}:\d{2}"
)
_EN_FOOTER_RE = re.compile(
    r"This\s+data\s+copy\s+was\s+created\s+on\s+at\s+\d{1,2}:\d{2}:\d{2}"
    r"\s+(?:am|pm)\s+on\s+\d{1,2}\s+\S+\s+(?P<year>\d{4})",
    re.IGNORECASE,
)

# Bare date literal — covers commit-message / docstring leaks where
# the whole footer phrase isn't present but a specific date is.
_DATE_LITERAL_RE = re.compile(
    rf"\b\d{{1,2}}\s+(?:{_RU_MONTHS}|{_EN_MONTHS})\s+(?P<year>\d{{4}})\b",
    re.IGNORECASE,
)

_DURATION_RE = re.compile(r"\b\d{1,3}(?:,\d{3})+\.\d+\b")

_CYRILLIC_NAME_PAIR_RE = re.compile(
    r"[А-ЯЁ][а-яё]{2,}\s+[А-ЯЁ][а-яё]{2,}"
)

_NUMERIC_ID_RE = re.compile(r"(?<![\d.])\d{5,12}(?![\d.])")
_ID_ALLOWLIST_PREFIXES = ("2000000",)


def _scan_text(text: str, path: str = "") -> list[str]:
    out: list[str] = []

    for m in _RU_FOOTER_RE.finditer(text):
        if m.group("year") not in _SYNTH_YEARS:
            out.append(
                f"RU archive-footer with non-synthetic year "
                f"{m.group('year')}: {m.group(0)!r}"
            )

    for m in _EN_FOOTER_RE.finditer(text):
        if m.group("year") not in _SYNTH_YEARS:
            out.append(
                f"EN archive-footer with non-synthetic year "
                f"{m.group('year')}: {m.group(0)!r}"
            )

    for m in _DATE_LITERAL_RE.finditer(text):
        if m.group("year") not in _SYNTH_YEARS:
            out.append(
                f"bare date literal with non-synthetic year "
                f"{m.group('year')}: {m.group(0)!r}"
            )

    for m in _DURATION_RE.finditer(text):
        if m.group(0) not in _SYNTH_DURATIONS:
            out.append(
                f"thousands-comma duration outside synthetic allowlist: "
                f"{m.group(0)!r} (use 1,234.56 / 9,999.99 in tests)"
            )

    for m in _CYRILLIC_NAME_PAIR_RE.finditer(text):
        out.append(
            f"Cyrillic capitalised-word pair (potential real name): "
            f"{m.group(0)!r}"
        )

    for m in _NUMERIC_ID_RE.finditer(text):
        token = m.group(0)
        if any(token.startswith(p) for p in _ID_ALLOWLIST_PREFIXES):
            continue
        out.append(
            f"bare 5-12 digit id outside synthetic range: {token!r}"
        )

    return out


def _resolve_payload(tool_name: str, tool_input: dict) -> tuple[str, str]:
    if tool_name == "Bash":
        return "<bash>", tool_input.get("command", "") or ""
    if tool_name == "Write":
        return (
            tool_input.get("file_path", "") or "",
            tool_input.get("content", "") or "",
        )
    if tool_name == "Edit":
        return (
            tool_input.get("file_path", "") or "",
            tool_input.get("new_string", "") or "",
        )
    if tool_name == "MultiEdit":
        path = tool_input.get("file_path", "") or ""
        parts: list[str] = []
        for edit in tool_input.get("edits") or []:
            parts.append(edit.get("new_string") or "")
        return path, "\n".join(parts)
    return "", ""


def _path_in_repo_scope(path: str) -> bool:
    """Bash commands always scan; file scans limit to VCS-tracked
    surfaces under the repo."""
    if not path:
        return True
    p = Path(path)
    interesting = {"src", "tests", "migrations", "scripts", ".github"}
    if any(seg in interesting for seg in p.parts):
        return True
    return p.suffix in {".md", ".sql", ".py", ".toml", ".yml", ".yaml"}


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    tool_name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {}) or {}
    if tool_name not in {"Bash", "Write", "Edit", "MultiEdit"}:
        return 0

    path, text = _resolve_payload(tool_name, tool_input)
    if tool_name != "Bash" and not _path_in_repo_scope(path):
        return 0

    violations = _scan_text(text, path)
    if not violations:
        return 0

    header = (
        f"entropy hook blocked {tool_name} on {path or '<no path>'} — "
        f"content contains low-entropy text matching real-archive "
        f"leakage patterns. See `memory/feedback_entropy.md` for the "
        f"rule + synthetic placeholder allowlist."
    )
    sys.stderr.write(header + "\n")
    for v in violations[:10]:
        sys.stderr.write(f"  - {v}\n")
    if len(violations) > 10:
        sys.stderr.write(f"  - … and {len(violations) - 10} more\n")
    return 2


if __name__ == "__main__":
    sys.exit(main())
