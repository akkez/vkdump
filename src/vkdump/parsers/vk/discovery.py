"""Detect what kind of VK dump input we're looking at and surface the
canonical pieces (profile file, messages index, chat folders) the
orchestrator needs.

The input is normalised into a `Source` first, so the rest of discovery
talks rel-paths and doesn't care whether the bytes ultimately come from a
real directory or a still-zipped archive.

Supported layouts:

- **archive root** — `<root>/index.html`, `<root>/messages/index-messages.html`,
  `<root>/profile/page-info.html`, chat folders at `<root>/messages/<peer>/`.
  This is the layout VK actually ships.
- **messages root** — pointing at the `messages/` directory itself
  (`<root>/index-messages.html`, chat folders at `<root>/<peer>/`).
- **single chat folder** — directly at the chat folder (contains
  `messagesN.html`).
- **single HTML file** — one `messagesN.html` page (parsed standalone), or
  the archive's `index.html` (treated as a marker — we redispatch against
  its containing dir), or a stray `index-messages.html` /
  `profile/page-info.html` (preload only).

Everything else returns an empty discovery — the orchestrator raises.
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .pages import is_message_page_filename, looks_like_chat_folder
from .sources import Source, join, open_source


_INDEX_HTML = "index.html"
_INDEX_MESSAGES_HTML = "index-messages.html"
_PAGE_INFO_HTML = "page-info.html"
_MESSAGES_DIR = "messages"
_PROFILE_DIR = "profile"


@dataclass
class Discovery:
    source: Source
    profile_file: str | None = None          # rel-path within the source
    messages_index_file: str | None = None   # rel-path within the source
    chat_folders: list[str] = field(default_factory=list)
    single_html_files: list[str] = field(default_factory=list)
    initial_input: Path | None = None        # what the user actually passed in

    @property
    def has_anything(self) -> bool:
        return bool(
            self.chat_folders
            or self.single_html_files
            or self.messages_index_file
            or self.profile_file
        )


def discover(path: Path) -> Discovery:
    """Open a Source for `path` and walk it for known dump pieces."""
    initial = path.expanduser().resolve()

    if initial.is_file():
        # A ZIP is a multi-file source; we walk it like a directory.
        if zipfile.is_zipfile(initial):
            source = open_source(initial)
            d = _discover_root(source)
            d.initial_input = initial
            return d
        if initial.suffix.lower() != ".html":
            raise ValueError(f"Unrecognised input file: {initial}")
        source = open_source(initial)  # DirectorySource at the file's parent
        return _discover_single_html(source, initial)

    source = open_source(initial)
    d = _discover_root(source)
    d.initial_input = initial
    return d


# ---------- multi-file roots ----------


def _discover_root(source: Source) -> Discovery:
    """Walk the source root looking for the canonical layouts."""
    d = Discovery(source=source)

    # Archive root: <root>/index.html + <root>/messages/ (+ optional profile).
    if source.is_file(_INDEX_HTML) and source.is_dir(_MESSAGES_DIR):
        d.messages_index_file = _pick_if_file(source, join(_MESSAGES_DIR, _INDEX_MESSAGES_HTML))
        d.profile_file = _pick_if_file(source, join(_PROFILE_DIR, _PAGE_INFO_HTML))
        d.chat_folders = _list_chat_folders(source, _MESSAGES_DIR)
        return d

    # Messages root: pointing directly at the `messages/` dir.
    if source.is_file(_INDEX_MESSAGES_HTML):
        d.messages_index_file = _INDEX_MESSAGES_HTML
        d.chat_folders = _list_chat_folders(source, "")
        return d

    # Single chat folder (root *is* the chat folder).
    if looks_like_chat_folder(source, ""):
        d.chat_folders = [""]
        return d

    # Last resort: parent of one or more chat folders, no index nearby.
    chat_folders = _list_chat_folders(source, "")
    if chat_folders:
        d.chat_folders = chat_folders
        return d

    return d


# ---------- single HTML file ----------


def _discover_single_html(source: Source, file_path: Path) -> Discovery:
    name = file_path.name.lower()
    d = Discovery(source=source, initial_input=file_path)
    file_rel = file_path.name

    if name == _INDEX_HTML:
        # Re-dispatch against the containing dir as if the user pointed there.
        nested = _discover_root(source)
        nested.initial_input = file_path
        return nested

    if name == _INDEX_MESSAGES_HTML:
        d.messages_index_file = file_rel
        d.chat_folders = _list_chat_folders(source, "")
        return d

    if name == _PAGE_INFO_HTML:
        d.profile_file = file_rel
        return d

    if is_message_page_filename(file_path.name):
        d.single_html_files = [file_rel]
        return d

    raise ValueError(f"Unrecognised single HTML file: {file_path}")


# ---------- helpers ----------


def _pick_if_file(source: Source, rel: str) -> str | None:
    return rel if source.is_file(rel) else None


def _list_chat_folders(source: Source, parent_rel: str) -> list[str]:
    if not source.is_dir(parent_rel):
        return []
    out: list[str] = []
    for name in source.listdir(parent_rel):
        child = join(parent_rel, name)
        if looks_like_chat_folder(source, child):
            out.append(child)
    return sorted(out)
