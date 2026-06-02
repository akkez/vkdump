from typing import Callable

from ..modules import enrich, fetch_data, parse_dump, save_chat, stats
from .spec import ParamSpec, TaskSpec


def _remember(key: str) -> Callable[[], str | None]:
    """Tiny factory for `default_provider`s that just read one
    `app_config` key — avoids a wall of near-identical helpers when a
    task wants every field to round-trip.
    """
    def provider() -> str | None:
        from ..core import app_config
        return app_config.get(key)
    provider.__name__ = f"_last_{key.replace('.', '_')}"
    return provider


def _last_parse_dump_source() -> str | None:
    """Pre-fill parse-dump's source field with the path used on the
    previous run. parse-dump writes its own key, so this is the
    primary signal; save-chat's mirror only kicks in for the first
    open if save-chat ran before parse-dump (rare but harmless)."""
    from ..core import app_config
    return app_config.get("parse_dump.last_source")


def _last_save_chat_source() -> str | None:
    """save-chat reuses the parse-dump source by default — both tasks
    point at the same VK dump. If the user explicitly set a different
    source via save-chat (e.g. a separate snapshot), that one wins."""
    from ..core import app_config
    return app_config.get("save_chat.last_source") or app_config.get("parse_dump.last_source")


def _last_save_chat_output() -> str | None:
    from ..core import app_config
    return app_config.get("save_chat.last_output")


def _last_save_chat_chat() -> str | None:
    """Re-select whichever chat the user picked in the previous save-chat
    run. Value is the chats.id as a string — matches the picker's
    choice values, so the combobox lands on the right row.
    """
    from ..core import app_config
    return app_config.get("save_chat.last_chat")


def _save_chat_open_path(result: dict) -> str | None:
    """Point the GUI's "Open output" button at the top-level index.html
    save-chat writes — landing page that lists every exported chat.

    When the run rendered a single chat (i.e. not "All chats" mode),
    append a `#chat-<id>` fragment so the browser scrolls to and
    highlights the freshly-exported row. The fragment is parsed out
    in ``task_panel._on_open_output`` so the file URI is built cleanly.
    Using the numeric chats.id (not the slug) keeps the anchor stable
    if the chat is later renamed and re-exported under a new slug.
    """
    if not isinstance(result, dict):
        return None
    out = result.get("output_dir")
    if not out:
        return None
    from pathlib import Path
    p = Path(out) / "index.html"
    if not p.is_file():
        return None
    chat_id = result.get("chat_id")
    if chat_id:
        return f"{p}#chat-{chat_id}"
    return str(p)


def _chat_scope_choices() -> list[tuple[str, str]]:
    """Populate the chat-scope dropdown from the DB at form-build time.

    Filtered by ``app_config['active_account_id']`` when set: chats
    from other accounts are hidden so the dropdown only shows what's
    sensibly pickable in the current scope.

    Choice **value** is the chat's primary key (`chats.id`), not its
    `peer_id` — peer_id collides across multiple-account dumps (two
    different conversations can share `peer_id=2000000004` if they
    came from different VK accounts). The id is unambiguous;
    downstream code resolves the row by it.
    """
    from ..core import app_config
    from ..core.db import connection
    out: list[tuple[str, str]] = [("", "All chats")]
    active = app_config.get("active_account_id") or ""
    try:
        with connection() as conn:
            if active:
                rows = conn.execute(
                    """
                    SELECT id, peer_id, account_id, title, type, message_count
                      FROM chats
                     WHERE peer_id IS NOT NULL AND peer_id != ''
                       AND account_id = ?
                     ORDER BY message_count DESC, id
                     LIMIT 500
                    """,
                    (str(active),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id, peer_id, account_id, title, type, message_count
                      FROM chats
                     WHERE peer_id IS NOT NULL AND peer_id != ''
                     ORDER BY message_count DESC, id
                     LIMIT 500
                    """
                ).fetchall()
    except Exception:
        return out
    for r in rows:
        title = (r["title"] or "").strip() or "(untitled)"
        tag = r["type"] or "?"
        # Hide the acct= chip when filtered — it'd be the same for every
        # row and add nothing.
        if active:
            label = f"{title} — {tag} (peer_id={r['peer_id']})"
        else:
            acct = r["account_id"] or "?"
            label = f"{title} — {tag} (acct={acct}, peer_id={r['peer_id']})"
        out.append((str(r["id"]), label))
    return out


TASKS: list[TaskSpec] = [
    TaskSpec(
        name="parse-dump",
        title="Import VK dump",
        description="",
        params=[
            ParamSpec(
                name="source",
                type="path_any",
                label="Dump source",
                help="A VK dump: ZIP archive, archive root folder, messages/ folder, single chat folder, or a single HTML file.",
                default_provider=_last_parse_dump_source,
            ),
        ],
        run=parse_dump.run,
    ),
    TaskSpec(
        name="fetch-data",
        title="Fetch data",
        description="Pull full VK message payloads (with forwards/reply chains) via messages.getById for every locally-parsed message that contains forwards. Idempotent — already-cached ids count toward the progress bar so a re-run resumes at the % it left off.",
        params=[],
        run=fetch_data.run,
        requires_vk_token=True,
    ),
    TaskSpec(
        name="enrich-media",
        title="Download media",
        description="Async-download attachment URLs (photos for now) into data/static/. Idempotent — only re-fetches pending / failed / missing files.",
        params=[
            ParamSpec(
                name="kinds",
                type="str",
                label="Attachment kinds",
                help="Comma-separated kinds to download. Default: photo.",
                required=False,
                default="photo",
                default_provider=_remember("enrich_media.last_kinds"),
            ),
            ParamSpec(
                name="chat_scope",
                type="choice",
                label="Chat scope",
                help="Restrict downloads to one chat. Leave at 'All chats' to consider every conversation.",
                required=False,
                default="",
                choices_provider=_chat_scope_choices,
                default_provider=_remember("enrich_media.last_chat_scope"),
            ),
            ParamSpec(
                name="strategy",
                type="choice",
                label="Strategy",
                help="Ordering — every queued attachment still gets downloaded eventually, this just controls what goes first.",
                required=False,
                default="newest-first",
                choices=[
                    ("newest-first", "Newest first"),
                    ("oldest-first", "Oldest first"),
                    ("groups-first", "Group chats first"),
                    ("dms-first", "DMs first"),
                    ("my-uploads-first", "My uploads first"),
                ],
                default_provider=_remember("enrich_media.last_strategy"),
            ),
            ParamSpec(
                name="concurrency",
                type="int",
                label="Concurrent downloads",
                help="Total in-flight requests across all hosts.",
                required=False,
                default=16,
                default_provider=_remember("enrich_media.last_concurrency"),
            ),
            ParamSpec(
                name="per_host",
                type="int",
                label="Per-host concurrency",
                help="Max in-flight requests to any one CDN subdomain.",
                required=False,
                default=8,
                default_provider=_remember("enrich_media.last_per_host"),
            ),
            ParamSpec(
                name="timeout",
                type="int",
                label="Timeout (seconds)",
                help="Per-request total timeout.",
                required=False,
                default=20,
                default_provider=_remember("enrich_media.last_timeout"),
            ),
        ],
        run=enrich.run,
    ),
    TaskSpec(
        name="save-chat",
        title="Save chat",
        description=(
            "Render one chat's original HTML into a portable folder, with"
            " locally-downloaded photos inlined alongside the original"
            " links. Re-export updates the same output dir's index in place."
        ),
        params=[
            ParamSpec(
                name="source",
                type="path_any",
                label="Dump source",
                help="Same VK dump you fed to parse-dump (ZIP or extracted folder).",
                default_provider=_last_save_chat_source,
            ),
            ParamSpec(
                name="chat",
                type="choice",
                label="Chat",
                help="Which chat to render. Pick 'All chats' to export every chat in one run. Type to filter.",
                required=False,
                default="",
                choices_provider=_chat_scope_choices,
                default_provider=_last_save_chat_chat,
            ),
            ParamSpec(
                name="output",
                type="dir",
                label="Output folder",
                help="Folder where the per-chat subfolder + index.html will live. Reused across runs.",
                default_provider=_last_save_chat_output,
            ),
        ],
        run=save_chat.run,
        result_open_path=_save_chat_open_path,
    ),
    TaskSpec(
        name="stats",
        title="Stats",
        description="Brief summary: chats, users, messages, attachments by kind, top chats / senders, parse errors.",
        params=[],
        run=stats.run,
        mutates_data=False,
    ),
]


def get_task(name: str) -> TaskSpec:
    for t in TASKS:
        if t.name == name:
            return t
    raise KeyError(f"Unknown task: {name}")
