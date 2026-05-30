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
from typing import Callable, Protocol

from ...parsers.vk.models import ParsedMessage


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
    # url → local_path-relative-to-static_root mapping for this chat's
    # attachments that have a successful download. Built once upfront so
    # transforms don't hit the DB per message.
    url_to_local: dict[str, str]
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
