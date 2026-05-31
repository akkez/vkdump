"""Per-message transform pipeline for save-chat.

A `Transform` takes one `ParsedMessage` and returns a (possibly mutated)
one — usually by editing `msg.raw_html` so the rendered page keeps the
original markup but gains augmentations (inline images now; later: video
posters, audio players, file icons, etc.). The orchestrator applies the
chain in order. Transforms communicate through `TransformContext` so a
later step can read what an earlier one did (e.g. an asset registry).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, NamedTuple, Protocol

from ...parsers.vk.models import ParsedMessage


class AttMeta(NamedTuple):
    """Per-URL metadata pulled from the DB for downloaded attachments.

    `resolution` is a kind-agnostic "<W>x<H>" string (cheaply extracted
    from the URL by enrich when possible; later we can backfill via
    Pillow/ffmpeg). `file_size` is bytes-on-disk.
    """

    local_path: str
    resolution: str | None
    file_size: int | None


@dataclass
class TransformContext:
    """Per-export state shared with every transform invocation."""

    chat_id: int
    chat_source_folder: str
    # `<output>/<chat_slug>/` — where assets live and where the
    # messages/ subfolder sits.
    output_chat_dir: Path
    # `<output>/<chat_slug>/messages/` — where the rendered HTML pages
    # are written. Asset hrefs in the HTML are computed relative to
    # *this* directory (so `<img src="../assets/...">`).
    pages_dir: Path
    # Repo's `data/static/` root where enrich-media stashes downloaded
    # files; transform reads originals from here.
    static_root: Path
    # url → AttMeta (local_path + resolution + file_size) for this
    # chat's successfully downloaded attachments. Built once upfront so
    # transforms don't hit the DB per message.
    url_to_meta: dict[str, AttMeta]
    # vk_id → users.display_name for every user the DB knows. The
    # sender-name transform reads this to swap the original VK-export
    # name in the message header for the current DB value — in
    # particular the bracketed ``"DELETED (...)"`` form that the
    # deleted-labels backfill writes for re-identified deleted users.
    user_names: dict[int, str] = field(default_factory=dict)
    # Hardlink/copy bookkeeping: maps the absolute source path on disk to
    # the path under output_chat_dir we wrote it to. Lets the renderer
    # dedupe identical assets (same URL across many messages → one file).
    copied_assets: dict[Path, Path] = field(default_factory=dict)
    # Per-attachment counters, keyed by kind. `candidates_*` is bumped
    # for every attachment of that kind we *could* have inlined (had a
    # usable URL); `injected_*` only when the page HTML actually
    # changed. End-of-run summary derives the match rate from these
    # without a second DB scan — the old SELECT COUNT(*) here was
    # blocking the worker for seconds on big chats.
    candidates_by_kind: dict[str, int] = field(default_factory=dict)
    injected_by_kind: dict[str, int] = field(default_factory=dict)
    # DB row said download_status='ok' but the file is gone from disk.
    # Bumped per-occurrence so a chat where most photos vanished
    # (manual cleanup, disk-full mid-download, etc.) surfaces in the
    # summary instead of silently skipping inject.
    missing_on_disk_by_kind: dict[str, int] = field(default_factory=dict)
    log: Callable[[str], None] = lambda _msg: None


class Transform(Protocol):
    """One step in the save-chat rendering pipeline."""

    name: str

    def apply(self, msg: ParsedMessage, ctx: TransformContext) -> ParsedMessage:
        ...


def apply_pipeline(
    msg: ParsedMessage,
    transforms: list[Transform],
    ctx: TransformContext,
) -> ParsedMessage:
    for t in transforms:
        msg = t.apply(msg, ctx)
    return msg
