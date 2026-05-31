"""Public API of the VK HTML parser. Stateless helpers; no DB orchestration."""
from .discovery import Discovery, discover
from .footer import ArchiveFooter, parse_archive_footer
from .index import (
    CHAT_TYPE_COMMUNITY,
    CHAT_TYPE_DM,
    CHAT_TYPE_GROUP_CHAT,
    ChatIndexEntry,
    chat_type_for_peer,
    extract_account_id,
    parse_messages_index,
    parse_messages_index_file,
)
from .messages import (
    VK_DUMP_ENCODING,
    decode_dump_bytes,
    parse_chat_meta,
    parse_page,
    read_page,
)
from .models import (
    KIND_AUDIO,
    KIND_FILE,
    KIND_FORWARD,
    KIND_PHOTO,
    KIND_UNKNOWN,
    KIND_VIDEO,
    KIND_WALL_COMMENT,
    KIND_WALL_POST,
    ParsedAttachment,
    ParsedChatMeta,
    ParsedMessage,
    ParseError,
)
from .pages import (
    is_message_page_filename,
    list_message_pages,
    looks_like_chat_folder,
)
from .profile import ProfileInfo, parse_profile_file, parse_profile_page
from .sources import DirectorySource, Source, ZipSource, open_source

__all__ = [
    "CHAT_TYPE_COMMUNITY",
    "CHAT_TYPE_DM",
    "CHAT_TYPE_GROUP_CHAT",
    "ArchiveFooter",
    "DirectorySource",
    "Discovery",
    "KIND_AUDIO",
    "KIND_FILE",
    "KIND_FORWARD",
    "KIND_PHOTO",
    "KIND_UNKNOWN",
    "KIND_VIDEO",
    "KIND_WALL_COMMENT",
    "KIND_WALL_POST",
    "ChatIndexEntry",
    "ParseError",
    "ParsedAttachment",
    "ParsedChatMeta",
    "ParsedMessage",
    "ProfileInfo",
    "Source",
    "VK_DUMP_ENCODING",
    "ZipSource",
    "chat_type_for_peer",
    "decode_dump_bytes",
    "discover",
    "extract_account_id",
    "is_message_page_filename",
    "list_message_pages",
    "looks_like_chat_folder",
    "open_source",
    "parse_archive_footer",
    "parse_chat_meta",
    "parse_messages_index",
    "parse_messages_index_file",
    "parse_page",
    "parse_profile_file",
    "parse_profile_page",
    "read_page",
]
