"""Parse VK service messages (joined / left / renamed / pinned / …) out of a
kludges HTML fragment.

VK renders these inside `<div class="kludges">` as raw markup with no
`<div class="attachment">` wrapper. The output is a single ParsedAttachment
with kind=KIND_CHAT_EVENT and a structured payload in `data` matching
``ChatEventPayload`` below.

The types below are the **single source of truth** for the chat-event schema:
- ``ChatEventSubtype`` lists every recognised subtype slug.
- ``CHAT_EVENT_SUBTYPES`` is the iterable companion (same values, tuple form).
- ``ChatEventUser`` / ``ChatEventPayload`` describe the JSON payload shape
  stored in ``attachments.data_json``.

Parser pattern tables ``_RU_PATTERNS`` / ``_EN_PATTERNS`` are keyed by
``ChatEventSubtype`` and a module-level assertion (see bottom of file)
enforces that the RU table covers every subtype slug — drift between the
type union and the parser is caught at import time. The EN table is allowed
to be incomplete (covers what we have real fixtures for); extend as new VK
locales surface.
"""
from __future__ import annotations

import re
from html import unescape
from typing import Literal, NotRequired, TypedDict

from .models import KIND_CHAT_EVENT, ParsedAttachment


# === Schema (single source of truth) ==========================================

ChatEventSubtype = Literal[
    "leave",
    "rejoin",
    "join_by_link",
    "invite",
    "kick",
    "chat_create",
    "chat_rename",
    "chat_photo_set",
    "chat_photo_removed",
    "message_pin",
    "message_unpin",
    "screenshot",
    "call_start",
    "chat_theme_change",
    "chat_theme_reset",
]

CHAT_EVENT_SUBTYPES: tuple[ChatEventSubtype, ...] = (
    "leave",
    "rejoin",
    "join_by_link",
    "invite",
    "kick",
    "chat_create",
    "chat_rename",
    "chat_photo_set",
    "chat_photo_removed",
    "message_pin",
    "message_unpin",
    "screenshot",
    "call_start",
    "chat_theme_change",
    "chat_theme_reset",
)

ChatEventLang = Literal["ru", "en"]


class ChatEventUser(TypedDict):
    """Actor / target inside a chat event. `vk_id` is omitted only when the
    profile URL doesn't expose a numeric id (rare; vanity URLs)."""
    display_name: str
    profile_url: str
    vk_id: NotRequired[int]


class ChatEventPayload(TypedDict):
    """The JSON payload stored in ``attachments.data_json`` for every row
    where ``attachments.kind = 'chat_event'``. Optional keys are omitted
    (not set to null) when not applicable to that subtype.
    """
    subtype: ChatEventSubtype
    lang: ChatEventLang
    actor: ChatEventUser
    target: NotRequired[ChatEventUser]          # invite / kick
    title_before: NotRequired[str]              # chat_rename, when «A» → «B»
    title_after: NotRequired[str]               # chat_create, chat_rename
    theme: NotRequired[str]                     # chat_theme_change
    pinned_excerpt: NotRequired[str]            # message_pin
    pinned_excerpt_truncated: NotRequired[bool] # message_pin: cut off with …
    fallback_text: NotRequired[str]             # body text outside kludges, when non-trivial


# Subtypes that take a second <USER> link as the target.
_SUBTYPES_WITH_TARGET: frozenset[ChatEventSubtype] = frozenset({"invite", "kick"})


# === Markup helpers ===========================================================

_SRV_USER_RE = re.compile(
    r'<a\s+class="im_srv_lnk\s*"[^>]*href="(?P<href>[^"]+)"[^>]*>(?P<name>[^<]*)</a>'
)
_SRV_VAL_RE = re.compile(
    r'<b\s+class="im_srv_lnk"[^>]*>(?P<val>.*?)</b>', re.DOTALL,
)
# Plain <b> is also used for chat titles in older dumps.
_PLAIN_B_RE = re.compile(r'<b>(?P<val>.*?)</b>', re.DOTALL)
_PINNED_EXCERPT_RE = re.compile(
    r'<span\s+class="im_srv_mess_link"[^>]*>(?P<excerpt>.*?)</span>', re.DOTALL,
)
# Theme messages have an extra cosmetic tail with a promo link to the mobile
# app — stripped before residue analysis. Both phrasings VK uses appear:
# "Оформление чата доступно …" and "Оформление чата показываем …".
_THEME_TAIL_RU = re.compile(
    r"\.\s*Оформление\s+чат(?:а|ов)\s+(?:доступно|показываем)\s+в\s+мобильном\s+приложении\.\s*"
    r"(?:<a\s+class=\"im_srv_lnk[^\"]*\"[^>]*>[^<]*</a>)?\s*$"
)
# Theme name itself is plain text inside «…» right after "на" / "to" (no <b>).
_THEME_VALUE_RU = re.compile(r"на\s+«([^»]+)»")
_THEME_VALUE_EN = re.compile(r"to\s+«([^»]+)»", re.IGNORECASE)


def _vk_id_from_href(href: str) -> int | None:
    """Resolve a VK profile URL to a numeric peer id. Communities (club/public/event)
    come back negative to match VK's peer-id convention.
    """
    m = re.search(r"https?://vk\.com/(id|club|public|event)(\d+)", href)
    if not m:
        return None
    n = int(m.group(2))
    return n if m.group(1) == "id" else -n


def _user_dict(href: str, name: str) -> ChatEventUser:
    out: ChatEventUser = {
        "display_name": unescape(name).strip(),
        "profile_url": href,
    }
    vk_id = _vk_id_from_href(href)
    if vk_id is not None:
        out["vk_id"] = vk_id
    return out


def _strip_inner_html(s: str) -> str:
    """Collapse inner markup (e.g. emoji imgs) into plain text."""
    s = re.sub(r'<img[^>]*\salt="([^"]*)"[^>]*/?>', r'\1', s)
    s = re.sub(r"<[^>]+>", "", s)
    return unescape(s).strip()


# === Verb-phrase patterns =====================================================
#
# Each pattern is matched via fullmatch against the cleaned residue (HTML
# stripped, «…» content removed, delimiters scrubbed). Anchoring to the
# entire residue keeps us honest — leftover words mean an unrecognised
# variant, not a silent partial match.

_RU_PATTERNS: dict[ChatEventSubtype, re.Pattern[str]] = {
    "leave":              re.compile(r"вы(?:шел|шла)\s+из\s+чата"),
    "rejoin":             re.compile(r"верн(?:улся|улась)\s+в\s+чат"),
    "join_by_link":       re.compile(r"присоединил(?:ся|ась)\s+к\s+чату\s+по\s+ссылке"),
    "invite":             re.compile(r"пригласил(?:а)?"),
    "kick":               re.compile(r"исключил(?:а)?"),
    "chat_create":        re.compile(r"создал(?:а)?\s+чат"),
    "chat_rename":        re.compile(r"измени(?:л|ла)\s+название\s+чата"),
    "chat_photo_set":     re.compile(r"обновил(?:а)?\s+фотографию\s+чата"),
    "chat_photo_removed": re.compile(r"удалил(?:а)?\s+фотографию\s+чата"),
    "message_pin":        re.compile(r"закрепил(?:а)?\s+сообщение"),
    "message_unpin":      re.compile(r"открепил(?:а)?\s+сообщение"),
    "screenshot":         re.compile(r"сделал(?:а)?\s+скриншот\s+чата"),
    "call_start":         re.compile(r"начал(?:а)?\s+групповой\s+звонок"),
    "chat_theme_change":  re.compile(r"измени(?:л|ла)\s+оформление\s+чата\s+на"),
    "chat_theme_reset":   re.compile(r"сброси(?:л|ла)\s+оформление\s+чата"),
}

# English patterns derived from real fallback_text fixtures observed in the
# dump (older VK exports rendered the same service messages in English in the
# message body when the client locale was non-Russian). Subtypes without a
# real fixture are still mapped to plausible UI strings so future EN-locale
# dumps don't need a parser update first.
_EN_PATTERNS: dict[ChatEventSubtype, re.Pattern[str]] = {
    "leave":              re.compile(r"left\s+the\s+(?:chat|conversation)", re.IGNORECASE),
    "rejoin":             re.compile(r"returned\s+to\s+the\s+(?:chat|conversation)", re.IGNORECASE),
    "join_by_link":       re.compile(r"joined\s+the\s+(?:chat|conversation)\s+via\s+link", re.IGNORECASE),
    "invite":             re.compile(r"invited", re.IGNORECASE),
    # "X kicked Y out" — the trailing "out" lands in the residue after we
    # strip the target <USER>. "removed" is the modern UI phrasing.
    "kick":               re.compile(r"(?:kicked\s+out|removed)", re.IGNORECASE),
    "chat_create":        re.compile(r"created\s+(?:the\s+)?(?:chat|conversation)", re.IGNORECASE),
    # Older dumps: "changed the conversation name to". Modern: "renamed … to".
    "chat_rename":        re.compile(
        r"(?:renamed\s+the\s+(?:chat|conversation)\s+to"
        r"|changed\s+the\s+(?:chat|conversation)\s+name\s+to)",
        re.IGNORECASE,
    ),
    # Older dumps: "changed conversation cover". Modern: "updated … photo".
    "chat_photo_set":     re.compile(
        r"(?:updated|changed)\s+(?:the\s+)?(?:chat|conversation)\s+(?:photo|cover)",
        re.IGNORECASE,
    ),
    "chat_photo_removed": re.compile(r"removed\s+(?:the\s+)?(?:chat|conversation)\s+(?:photo|cover)", re.IGNORECASE),
    "message_pin":        re.compile(r"pinned\s+(?:a\s+)?message", re.IGNORECASE),
    "message_unpin":      re.compile(r"unpinned\s+(?:a\s+)?message", re.IGNORECASE),
    "screenshot":         re.compile(r"took\s+a\s+(?:chat|conversation)\s+screenshot", re.IGNORECASE),
    "call_start":         re.compile(r"started\s+a\s+group\s+call", re.IGNORECASE),
    "chat_theme_change":  re.compile(r"changed\s+(?:the\s+)?(?:chat|conversation)\s+theme\s+to", re.IGNORECASE),
    "chat_theme_reset":   re.compile(r"reset\s+(?:the\s+)?(?:chat|conversation)\s+theme", re.IGNORECASE),
}

# Drift guard: every subtype slug must have an RU pattern. EN is allowed to
# lag because real EN fixtures arrive piecemeal — but extras (typos) are
# always rejected.
_missing_ru = set(CHAT_EVENT_SUBTYPES) - set(_RU_PATTERNS)
_extra_ru = set(_RU_PATTERNS) - set(CHAT_EVENT_SUBTYPES)
_extra_en = set(_EN_PATTERNS) - set(CHAT_EVENT_SUBTYPES)
assert not _missing_ru, f"_RU_PATTERNS missing subtypes: {sorted(_missing_ru)}"
assert not _extra_ru, f"_RU_PATTERNS has unknown subtypes: {sorted(_extra_ru)}"
assert not _extra_en, f"_EN_PATTERNS has unknown subtypes: {sorted(_extra_en)}"


# === Parser ===================================================================


def parse_chat_event(
    kludges_html: str, plain_text: str = "",
) -> ParsedAttachment | None:
    """Return a chat_event attachment if `kludges_html` is a recognised VK
    service message, else None.

    `plain_text` is the message-body text that sits *outside* the kludges block.
    When it differs from a trivial duplicate of the canonical phrase we keep
    it as `fallback_text` — VK occasionally rendered a different name there.
    """
    if not kludges_html or not kludges_html.strip():
        return None
    work = kludges_html.strip()

    # Pull out the actor — the leading <a class="im_srv_lnk"> link.
    m_actor = _SRV_USER_RE.search(work)
    if not m_actor or m_actor.start() != 0:
        return None
    actor = _user_dict(m_actor.group("href"), m_actor.group("name"))

    # Capture trailing <b>/<b class="im_srv_lnk"> "after" value(s). Older
    # rename dumps render «<b>BEFORE</b>» &rarr; «<b>AFTER</b>».
    val_after: str | None = None
    val_before: str | None = None
    vals = [m.group("val") for m in _SRV_VAL_RE.finditer(work)]
    if not vals:
        vals = [m.group("val") for m in _PLAIN_B_RE.finditer(work)]
    if len(vals) >= 2:
        val_before = _strip_inner_html(vals[0])
        val_after = _strip_inner_html(vals[1])
    elif len(vals) == 1:
        val_after = _strip_inner_html(vals[0])

    # Pinned-message excerpt.
    pinned_excerpt: str | None = None
    pinned_truncated = False
    m_pin = _PINNED_EXCERPT_RE.search(work)
    if m_pin:
        excerpt = _strip_inner_html(m_pin.group("excerpt"))
        pinned_truncated = excerpt.endswith("…")
        if pinned_truncated:
            excerpt = excerpt.rstrip("…").rstrip()
        pinned_excerpt = excerpt

    # Theme name lives in plain «…» (no <b>) after "на" / "to".
    theme: str | None = None
    m_theme = _THEME_VALUE_RU.search(work) or _THEME_VALUE_EN.search(work)
    if m_theme:
        theme = _strip_inner_html(m_theme.group(1))

    # Strip markup + cosmetic theme tail so the verb-phrase residue is plain text.
    residue = work
    residue = _THEME_TAIL_RU.sub("", residue)
    residue = _SRV_USER_RE.sub("", residue)
    residue = _SRV_VAL_RE.sub("", residue)
    residue = _PLAIN_B_RE.sub("", residue)
    residue = _PINNED_EXCERPT_RE.sub("", residue)
    residue = unescape(residue)
    residue = re.sub(r"«[^»]*»", " ", residue)
    residue = re.sub(r"[«»:\-→]", " ", residue)
    residue = re.sub(r"\s+", " ", residue).strip()

    # Match against RU, then EN.
    subtype, lang = _match_subtype(residue)
    if subtype is None:
        return None

    # invite/kick: a second <USER> link is the target.
    target: ChatEventUser | None = None
    if subtype in _SUBTYPES_WITH_TARGET:
        m_target = _SRV_USER_RE.search(work, m_actor.end())
        if m_target:
            target = _user_dict(m_target.group("href"), m_target.group("name"))

    payload: ChatEventPayload = {  # type: ignore[typeddict-item]
        "subtype": subtype,
        "lang": lang,
        "actor": actor,
    }
    if target is not None:
        payload["target"] = target
    if subtype == "chat_create" and val_after is not None:
        payload["title_after"] = val_after
    if subtype == "chat_rename":
        if val_after is not None:
            payload["title_after"] = val_after
        if val_before is not None:
            payload["title_before"] = val_before
    if subtype == "chat_theme_change" and theme is not None:
        payload["theme"] = theme
    if subtype == "message_pin" and pinned_excerpt is not None:
        payload["pinned_excerpt"] = pinned_excerpt
        if pinned_truncated:
            payload["pinned_excerpt_truncated"] = True

    pt = (plain_text or "").strip()
    if pt:
        payload["fallback_text"] = pt

    return ParsedAttachment(
        kind=KIND_CHAT_EVENT,
        description=_strip_inner_html(work),
        url=actor.get("profile_url"),
        data=dict(payload),
    )


def _match_subtype(
    residue: str,
) -> tuple[ChatEventSubtype, ChatEventLang] | tuple[None, ChatEventLang]:
    for sub, pat in _RU_PATTERNS.items():
        if pat.fullmatch(residue):
            return sub, "ru"
    for sub, pat in _EN_PATTERNS.items():
        if pat.fullmatch(residue):
            return sub, "en"
    return None, "ru"
