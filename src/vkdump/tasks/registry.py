from ..modules import parse_dump, enrich, stats
from .spec import ParamSpec, TaskSpec


def _chat_scope_choices() -> list[tuple[str, str]]:
    """Populate the enrich-media chat-scope dropdown from the DB at
    form-build time. Returns (peer_id-as-string, label) pairs ordered
    by message count desc so the busiest chats land at the top.
    """
    from ..core.db import connection
    out: list[tuple[str, str]] = [("", "All chats")]
    try:
        with connection() as conn:
            rows = conn.execute(
                """
                SELECT peer_id, title, type, message_count
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
        out.append(
            (str(r["peer_id"]), f"{title} — {tag} (peer_id={r['peer_id']})")
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
