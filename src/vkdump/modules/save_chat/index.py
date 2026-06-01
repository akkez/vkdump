"""Top-level export index: a small HTML landing page + a JSON sidecar.

The user can call save-chat multiple times against the same output dir
with different chats; the JSON is the source of truth, the HTML is
re-rendered from it. Re-exporting the same chat updates the entry
in place.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path

_SIDECAR = "_export.json"
_INDEX = "index.html"

# Tolerant matcher for both the current `YYYY-MM-DD HH:MM:SS` format and
# the legacy ISO form (`YYYY-MM-DDTHH:MM:SS+00:00`) sitting in older
# sidecars — so a re-render of a long-lived output dir still produces
# clean timestamps without having to migrate the JSON.
_EXPORTED_AT_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})"
)


@dataclass
class ExportEntry:
    chat_slug: str          # folder name under the output dir
    peer_id: str
    title: str
    type: str               # 'dm' / 'group_chat' / etc.
    message_count: int
    exported_at: str        # ISO-8601 UTC
    # Filename of the first page inside the chat folder (e.g.
    # 'messages0.html'). Stored so the index can deep-link to it
    # exactly the way VK's own index.html does — opening
    # `<chat_slug>/` would just show a dir listing in most browsers.
    first_page: str = ""
    # chats.id at export time — primary key in the source DB, so a
    # re-export of the same chat (with a possibly renamed title) can
    # reuse the original folder slug instead of stranding the old one.
    # Optional/zero for entries written before this field existed.
    chat_id: int = 0
    # Filename of the per-chat photo-only gallery page, or "" when the
    # chat has no downloaded photos and the page was skipped.
    photos_page: str = ""
    # Photo count surfaced on the index so the user can scan from the
    # landing page without opening the gallery.
    photos_count: int = 0


@dataclass
class ExportManifest:
    version: int = 1
    chats: list[ExportEntry] = field(default_factory=list)


def _now() -> str:
    """UTC timestamp formatted plain — no `T`, no `+00:00` tail. The
    value is implicitly UTC; carrying the tz suffix in user-facing
    output was noise without information."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fmt_exported_at(raw: str) -> str:
    """Normalise an `exported_at` value for display. Accepts both the
    current `YYYY-MM-DD HH:MM:SS` and the legacy ISO form, returns the
    space-separated date+time without timezone."""
    if not raw:
        return ""
    m = _EXPORTED_AT_RE.match(raw)
    if not m:
        return raw
    return f"{m.group(1)} {m.group(2)}"


def load(output_dir: Path) -> ExportManifest:
    path = output_dir / _SIDECAR
    if not path.is_file():
        return ExportManifest()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ExportManifest()
    # Tolerate older sidecars that lack fields added later (`first_page`,
    # `chat_id`, …) by dropping unknown keys and letting dataclass
    # defaults fill in missing ones. Keeps existing exports loadable.
    known = {f.name for f in fields(ExportEntry)}
    chats = [ExportEntry(**{k: v for k, v in c.items() if k in known}) for c in raw.get("chats", [])]
    return ExportManifest(version=raw.get("version", 1), chats=chats)


def find_slug_for_chat(output_dir: Path, chat_id: int) -> str | None:
    """Return the existing slug for `chat_id` if any past export to
    this dir used it. Lets us reuse the original folder name even when
    the chat's display title later changes.
    """
    if not chat_id:
        return None
    manifest = load(output_dir)
    for c in manifest.chats:
        if c.chat_id == chat_id:
            return c.chat_slug
    return None


def upsert(
    output_dir: Path,
    chat_slug: str,
    peer_id: str,
    title: str,
    type_: str,
    message_count: int,
    first_page: str = "",
    chat_id: int = 0,
    photos_page: str = "",
    photos_count: int = 0,
) -> ExportManifest:
    """Insert or update the manifest entry for `chat_slug`. Returns the
    updated manifest so the caller can re-render the HTML index.
    """
    manifest = load(output_dir)
    entry = ExportEntry(
        chat_slug=chat_slug,
        peer_id=peer_id,
        title=title,
        type=type_,
        message_count=message_count,
        exported_at=_now(),
        first_page=first_page,
        chat_id=chat_id,
        photos_page=photos_page,
        photos_count=photos_count,
    )
    # Dedupe by slug AND by chat_id — covers the case where an earlier
    # export used a different slug for the same chat (e.g. before
    # `chat_id` was tracked, or after a title rename).
    manifest.chats = [
        c for c in manifest.chats
        if c.chat_slug != chat_slug and (chat_id == 0 or c.chat_id != chat_id)
    ]
    manifest.chats.append(entry)
    manifest.chats.sort(key=lambda c: (-c.message_count, c.title.lower()))
    _write(output_dir, manifest)
    return manifest


def _write(output_dir: Path, manifest: ExportManifest) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    sidecar = output_dir / _SIDECAR
    sidecar.write_text(
        json.dumps(
            {"version": manifest.version, "chats": [asdict(c) for c in manifest.chats]},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (output_dir / _INDEX).write_text(_render_html(manifest), encoding="utf-8")


def _render_html(manifest: ExportManifest) -> str:
    rows = []
    for c in manifest.chats:
        title = html_escape(c.title or c.chat_slug)
        slug = html_escape(c.chat_slug, quote=True)
        # Deep-link straight to the first page if we know it (matches
        # the way VK's own index.html points at messages0.html), else
        # fall back to the chat folder.
        href = f"{slug}/{html_escape(c.first_page, quote=True)}" if c.first_page else f"{slug}/"
        photos_link = (
            f' · <a class="photos-link" href="{slug}/{html_escape(c.photos_page, quote=True)}">'
            f'photos ({c.photos_count})</a>'
            if c.photos_page else ""
        )
        # `id="chat-<chat_id>"` lets the GUI "Open output" button jump
        # straight to the row of the chat that was just exported
        # (`index.html#chat-<id>`); the `:target` CSS rule below
        # highlights that row so the user spots it without scanning.
        # Using the numeric DB id (not the slug) keeps the anchor
        # stable across title renames and ASCII-safe by construction.
        row_id = f' id="chat-{c.chat_id}"' if c.chat_id else ""
        rows.append(
            f'<li{row_id}><a href="{href}">{title}</a>'
            f'{photos_link}'
            f' <span class="meta">· {c.type} · {c.message_count} messages'
            f' · {html_escape(c.peer_id)} · exported'
            f' {html_escape(_fmt_exported_at(c.exported_at))}</span></li>'
        )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>vkdump exports</title>"
        "<style>"
        "body{font:14px/1.45 -apple-system,Segoe UI,sans-serif;margin:32px;max-width:900px}"
        "h1{margin:0 0 16px;font-size:20px}"
        "ul{list-style:none;padding:0}"
        "li{padding:8px 0;border-bottom:1px solid #eee;scroll-margin-top:16px}"
        "li:target,li.flash{background:#ffe066;border-left:4px solid #f0a500;"
        "padding-left:10px}"
        "@keyframes vkdump-flash{0%,100%{background:#ffe066}"
        "20%,60%{background:#ffae00}}"
        "li.flash{animation:vkdump-flash 1.6s ease-in-out 2}"
        "a{color:#0a66c2;text-decoration:none}a:hover{text-decoration:underline}"
        ".photos-link{font-size:12px}"
        ".meta{color:#777;font-size:12px}"
        "</style>"
        "</head><body>"
        f"<h1>Exported chats ({len(manifest.chats)})</h1>"
        "<ul>" + "\n".join(rows) + "</ul>"
        # Inline fallback — :target alone is unreliable when the
        # document is large enough that the browser navigates to the
        # fragment before the relevant <li> has been parsed. We
        # re-resolve `location.hash`, scroll the row into view, and
        # add `.flash` so the row visibly pulses even if it was
        # already on-screen.
        "<script>"
        "(function(){var h=location.hash;if(!h||h.length<2)return;"
        "var el=document.getElementById(h.slice(1));if(!el)return;"
        "el.scrollIntoView({block:'center'});"
        "el.classList.add('flash');"
        "})();"
        "</script>"
        "</body></html>"
    )
