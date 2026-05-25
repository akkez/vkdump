"""Discover and order messagesN.html pages inside one chat folder.

All path inputs are POSIX-style rel-paths against a `Source` — works
identically for filesystem directories and live ZIP archives.
"""
from __future__ import annotations

import re

from .sources import Source, join

_PAGE_NAME_RE = re.compile(r"^messages(\d+)\.html$", re.IGNORECASE)


def is_message_page_filename(name: str) -> bool:
    """Return True if `name` is shaped like `messagesN.html` (basename only)."""
    return _PAGE_NAME_RE.match(name) is not None


def looks_like_chat_folder(source: Source, rel: str) -> bool:
    """A chat folder is one that contains at least one messagesN.html file."""
    if not source.is_dir(rel):
        return False
    for name in source.listdir(rel):
        if is_message_page_filename(name) and source.is_file(join(rel, name)):
            return True
    return False


def list_message_pages(source: Source, chat_rel: str) -> list[str]:
    """Return rel-paths of all messagesN.html files in `chat_rel`, ordered
    by their numeric offset.
    """
    pages: list[tuple[int, str]] = []
    if not source.is_dir(chat_rel):
        return []
    for name in source.listdir(chat_rel):
        m = _PAGE_NAME_RE.match(name)
        if not m:
            continue
        child = join(chat_rel, name)
        if source.is_file(child):
            pages.append((int(m.group(1)), child))
    pages.sort(key=lambda t: t[0])
    return [rel for _, rel in pages]
