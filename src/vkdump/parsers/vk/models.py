"""Dataclasses returned by the VK HTML parser."""
from dataclasses import dataclass, field
from datetime import datetime


# Attachment kind slugs (English) used downstream. The raw Russian label is
# kept in ParsedAttachment.description so we never lose information.
KIND_PHOTO = "photo"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
KIND_FILE = "file"
KIND_WALL_POST = "wall_post"
KIND_WALL_COMMENT = "wall_comment"
KIND_FORWARD = "forward"
KIND_UNKNOWN = "unknown"


@dataclass
class ParsedAttachment:
    kind: str
    description: str | None
    url: str | None = None
    forward_count: int | None = None  # only set when kind == KIND_FORWARD
    position: int = 0


@dataclass
class ParsedMessage:
    vk_message_id: int
    sent_at: datetime
    sender_vk_id: int | None
    sender_display_name: str | None
    sender_is_self: bool
    text: str
    attachments: list[ParsedAttachment] = field(default_factory=list)
    has_forwards: bool = False
    forwarded_count: int = 0
    is_reply: bool = False
    reply_to_message_id: int | None = None
    is_edited: bool = False
    edited_at: datetime | None = None
    raw_html: str = ""
    source_file: str = ""
    # True when every meaningful HTML fragment of this message has been
    # captured into typed fields (no unknown attachments, no leftover markup
    # in kludges). Callers may drop `raw_html` for these — the parsed row
    # then represents the message losslessly on its own.
    fully_parsed: bool = True


@dataclass
class ParsedChatMeta:
    source_folder: str
    title: str | None
    account_id: str | None  # VK numeric id of the dump owner, as a string
    total_expected_count: int | None  # estimated from pagination on the first page
    dump_generated_at: datetime | None = None  # `time_current` from the jd meta


@dataclass
class ParseError:
    source_file: str
    raw_html: str
    error: str
    traceback: str
