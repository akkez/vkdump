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
        name="enrich",
        title="Enrich messages",
        description="Download referenced media, expand forwards, resolve deleted users. (stub)",
        params=[],
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
