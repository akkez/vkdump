from ..modules import enrich, parse_dump, save_chat, stats
from .spec import ParamSpec, TaskSpec


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


def _save_chat_open_path(result: dict) -> str | None:
    """Point the GUI's "Open output" button at the top-level index.html
    save-chat writes — landing page that lists every exported chat.
    """
    if not isinstance(result, dict):
        return None
    out = result.get("output_dir")
    if not out:
        return None
    from pathlib import Path
    p = Path(out) / "index.html"
    return str(p) if p.is_file() else None


def _chat_picker_choices() -> list[tuple[str, str]]:
    """Same list as the enrich scope picker, but without the 'All chats'
    sentinel — save-chat renders one chat at a time, so an empty pick
    is a hard error rather than a useful default.
    """
    return [opt for opt in _chat_scope_choices() if opt[0] != ""]


def _chat_scope_choices() -> list[tuple[str, str]]:
    """Populate the chat-scope dropdown from the DB at form-build time.

    Choice **value** is the chat's primary key (`chats.id`), not its
    `peer_id` — peer_id collides across multiple-account dumps (two
    different conversations can share `peer_id=2000000004` if they
    came from different VK accounts). The id is unambiguous; downstream
    code resolves the row by it. Label exposes account_id so the user
    can tell duplicates apart at pick time.
    """
    from ..core.db import connection
    out: list[tuple[str, str]] = [("", "All chats")]
    try:
        with connection() as conn:
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
        acct = r["account_id"] or "?"
        out.append(
            (
                str(r["id"]),
                f"{title} — {tag} (acct={acct}, peer_id={r['peer_id']})",
            )
        )
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
            ),
            ParamSpec(
                name="chat_scope",
                type="choice",
                label="Chat scope",
                help="Restrict downloads to one chat. Leave at 'All chats' to consider every conversation.",
                required=False,
                default="",
                choices_provider=_chat_scope_choices,
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
            ),
            ParamSpec(
                name="concurrency",
                type="int",
                label="Concurrent downloads",
                help="Total in-flight requests across all hosts.",
                required=False,
                default=16,
            ),
            ParamSpec(
                name="per_host",
                type="int",
                label="Per-host concurrency",
                help="Max in-flight requests to any one CDN subdomain.",
                required=False,
                default=8,
            ),
            ParamSpec(
                name="timeout",
                type="int",
                label="Timeout (seconds)",
                help="Per-request total timeout.",
                required=False,
                default=20,
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
                help="Which chat to render. Type to filter.",
                choices_provider=_chat_picker_choices,
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
    ),
]


def get_task(name: str) -> TaskSpec:
    for t in TASKS:
        if t.name == name:
            return t
    raise KeyError(f"Unknown task: {name}")
