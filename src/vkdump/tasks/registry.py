from ..modules import parse_dump, enrich, stats
from .spec import ParamSpec, TaskSpec


TASKS: list[TaskSpec] = [
    TaskSpec(
        name="parse-dump",
        title="Parse VK dump",
        description="Read a VK dump directory and load chats / users / messages / attachments into SQLite.",
        params=[
            ParamSpec(
                name="source",
                type="path_any",
                label="Dump source",
                help="A VK dump: ZIP archive, archive root folder, messages/ folder, single chat folder, or a single HTML file.",
            ),
            ParamSpec(
                name="source_timezone",
                type="str",
                label="Source timezone",
                help="IANA zone the dump's timestamps are in. Stored on chats; conversion happens at display time. Default 'UTC'.",
                required=False,
                default="UTC",
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
        title="Compute statistics",
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
