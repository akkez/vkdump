"""Top-level export index: a small HTML landing page + a JSON sidecar.

The user can call save-chat multiple times against the same output dir
with different chats; the JSON is the source of truth, the HTML is
re-rendered from it. Re-exporting the same chat updates the entry
in place.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import escape as html_escape
from pathlib import Path

_SIDECAR = "_export.json"
_INDEX = "index.html"


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


@dataclass
class ExportManifest:
    version: int = 1
    chats: list[ExportEntry] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load(output_dir: Path) -> ExportManifest:
    path = output_dir / _SIDECAR
    if not path.is_file():
        return ExportManifest()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return ExportManifest()
    chats = [ExportEntry(**c) for c in raw.get("chats", [])]
    return ExportManifest(version=raw.get("version", 1), chats=chats)


def upsert(
    output_dir: Path,
    chat_slug: str,
    peer_id: str,
    title: str,
    type_: str,
    message_count: int,
    first_page: str = "",
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
    )
    manifest.chats = [c for c in manifest.chats if c.chat_slug != chat_slug]
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
        rows.append(
            f'<li><a href="{href}">{title}</a>'
            f' <span class="meta">· {c.type} · {c.message_count} messages'
            f' · {html_escape(c.peer_id)} · exported {html_escape(c.exported_at)}</span></li>'
        )
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        "<title>vkdump exports</title>"
        "<style>"
        "body{font:14px/1.45 -apple-system,Segoe UI,sans-serif;margin:32px;max-width:900px}"
        "h1{margin:0 0 16px;font-size:20px}"
        "ul{list-style:none;padding:0}"
        "li{padding:8px 0;border-bottom:1px solid #eee}"
        "a{color:#0a66c2;text-decoration:none}a:hover{text-decoration:underline}"
        ".meta{color:#777;font-size:12px}"
        "</style>"
        "</head><body>"
        f"<h1>Exported chats ({len(manifest.chats)})</h1>"
        "<ul>" + "\n".join(rows) + "</ul>"
        "</body></html>"
    )
