"""Regression tests for parsers.vk.chat_events.

One fixture per subtype the parser recognises. All HTML below is synthetic —
no real names, profile ids, or message excerpts. The shapes mirror what VK's
HTML exporter emits, observed via the dump survey that drove the parser
design (see ARCHITECTURE.md "Chat-event attachments").
"""
from vkdump.parsers.vk.chat_events import (
    CHAT_EVENT_SUBTYPES,
    _RU_PATTERNS,
    parse_chat_event,
)
from vkdump.parsers.vk.models import KIND_CHAT_EVENT


def _u(vk_id: int, name: str = "Sample User") -> str:
    """Synthetic <a class="im_srv_lnk"> rendering used in fixtures."""
    return (
        f'<a class="im_srv_lnk " target="_blank" '
        f'href="https://vk.com/id{vk_id}">{name}</a>'
    )


def _actor(vk_id: int = 100, name: str = "Sample Actor") -> dict:
    return {
        "vk_id": vk_id,
        "display_name": name,
        "profile_url": f"https://vk.com/id{vk_id}",
    }


# ----------------------------- member events -----------------------------


def test_leave_masculine() -> None:
    att = parse_chat_event(f"{_u(100, 'Sample Actor')} вышел из чата")
    assert att is not None and att.kind == KIND_CHAT_EVENT
    assert att.data == {
        "subtype": "leave",
        "lang": "ru",
        "actor": _actor(),
    }


def test_leave_feminine() -> None:
    att = parse_chat_event(f"{_u(100, 'Sample Actor')} вышла из чата")
    assert att and att.data["subtype"] == "leave"


def test_rejoin_masculine_and_feminine() -> None:
    for verb in ("вернулся", "вернулась"):
        att = parse_chat_event(f"{_u(100, 'Sample Actor')} {verb} в чат")
        assert att and att.data["subtype"] == "rejoin"


def test_join_by_link() -> None:
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} присоединился к чату по ссылке"
    )
    assert att and att.data["subtype"] == "join_by_link"


def test_invite_captures_target() -> None:
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} пригласил {_u(200, 'Sample Target')}"
    )
    assert att is not None
    assert att.data["subtype"] == "invite"
    assert att.data["actor"]["vk_id"] == 100
    assert att.data["target"] == {
        "vk_id": 200,
        "display_name": "Sample Target",
        "profile_url": "https://vk.com/id200",
    }


def test_kick_captures_target_feminine_verb() -> None:
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} исключила {_u(200, 'Sample Target')}"
    )
    assert att and att.data["subtype"] == "kick"
    assert att.data["target"]["vk_id"] == 200


# ----------------------------- chat metadata -----------------------------


def test_chat_create_keeps_title() -> None:
    att = parse_chat_event(
        f'{_u(100, "Sample Actor")} создал чат «<b class="im_srv_lnk">Sample Title</b>»'
    )
    assert att and att.data["subtype"] == "chat_create"
    assert att.data["title_after"] == "Sample Title"


def test_chat_rename_after_only() -> None:
    att = parse_chat_event(
        f'{_u(100, "Sample Actor")} изменил название чата: '
        f'«<b class="im_srv_lnk">New Title</b>»'
    )
    assert att and att.data["subtype"] == "chat_rename"
    assert att.data["title_after"] == "New Title"
    assert "title_before" not in att.data


def test_chat_rename_before_and_after() -> None:
    att = parse_chat_event(
        f'{_u(100, "Sample Actor")} изменил название чата: '
        f'«<b class="im_srv_lnk">Old Title</b>» &rarr; '
        f'«<b class="im_srv_lnk">New Title</b>»'
    )
    assert att and att.data["subtype"] == "chat_rename"
    assert att.data["title_before"] == "Old Title"
    assert att.data["title_after"] == "New Title"


def test_chat_photo_set_and_removed() -> None:
    set_att = parse_chat_event(f"{_u(100, 'Sample Actor')} обновил фотографию чата")
    rem_att = parse_chat_event(f"{_u(100, 'Sample Actor')} удалила фотографию чата")
    assert set_att and set_att.data["subtype"] == "chat_photo_set"
    assert rem_att and rem_att.data["subtype"] == "chat_photo_removed"


# ----------------------------- pin / unpin -------------------------------


def test_pin_without_excerpt() -> None:
    att = parse_chat_event(f"{_u(100, 'Sample Actor')} закрепил сообщение")
    assert att and att.data["subtype"] == "message_pin"
    assert "pinned_excerpt" not in att.data


def test_pin_with_excerpt_kept_verbatim() -> None:
    att = parse_chat_event(
        f'{_u(100, "Sample Actor")} закрепил сообщение '
        f'«<span class="im_srv_mess_link">a sample pinned line</span>»'
    )
    assert att and att.data["subtype"] == "message_pin"
    assert att.data["pinned_excerpt"] == "a sample pinned line"
    assert "pinned_excerpt_truncated" not in att.data


def test_pin_with_truncated_excerpt_flags_it() -> None:
    att = parse_chat_event(
        f'{_u(100, "Sample Actor")} закрепил сообщение '
        f'«<span class="im_srv_mess_link">a very long pinned message that…</span>»'
    )
    assert att and att.data["pinned_excerpt_truncated"] is True
    assert att.data["pinned_excerpt"] == "a very long pinned message that"


def test_unpin() -> None:
    att = parse_chat_event(f"{_u(100, 'Sample Actor')} открепила сообщение")
    assert att and att.data["subtype"] == "message_unpin"


# ----------------------------- misc events -------------------------------


def test_screenshot() -> None:
    att = parse_chat_event(f"{_u(100, 'Sample Actor')} сделал скриншот чата")
    assert att and att.data["subtype"] == "screenshot"


def test_call_start() -> None:
    att = parse_chat_event(f"{_u(100, 'Sample Actor')} начал групповой звонок")
    assert att and att.data["subtype"] == "call_start"


def test_theme_change_keeps_theme_name_and_drops_promo_tail() -> None:
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} изменил оформление чата на «Blue». "
        f"Оформление чата доступно в мобильном приложении. {_u(999, 'VK')}"
    )
    assert att and att.data["subtype"] == "chat_theme_change"
    assert att.data["theme"] == "Blue"
    # The promo link must not become the actor.
    assert att.data["actor"]["vk_id"] == 100


def test_theme_reset() -> None:
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} сбросил оформление чата. "
        f"Оформление чатов доступно в мобильном приложении. {_u(999, 'VK')}"
    )
    assert att and att.data["subtype"] == "chat_theme_reset"


# ----------------------------- fallback_text -----------------------------


def test_fallback_text_kept_when_different_from_canonical() -> None:
    """Old VK dumps occasionally render a different/unrelated plain text
    outside the kludges block. Both names matter — fallback_text keeps it.
    """
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} вышел из чата",
        plain_text="Sample Other left the conversation",
    )
    assert att and att.data["fallback_text"] == "Sample Other left the conversation"


def test_fallback_text_omitted_when_canonical_duplicate() -> None:
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} вышел из чата",
        plain_text="Sample Actor left the chat",
    )
    assert att and "fallback_text" not in att.data


# ----------------------------- community ids -----------------------------


def test_community_actor_gets_negative_peer_id() -> None:
    html = (
        '<a class="im_srv_lnk " target="_blank" '
        'href="https://vk.com/public42">Sample Community</a>'
        ' создал чат «<b class="im_srv_lnk">Sample Title</b>»'
    )
    att = parse_chat_event(html)
    assert att and att.data["actor"]["vk_id"] == -42


# ----------------------------- unrecognised ------------------------------


def test_unknown_subtype_returns_none() -> None:
    att = parse_chat_event(f"{_u(100, 'Sample Actor')} did something new")
    assert att is None


def test_empty_input_returns_none() -> None:
    assert parse_chat_event("") is None
    assert parse_chat_event("   ") is None


# ----------------------------- english ---------------------------------
#
# Phrases below match real fallback_text fixtures observed in the source dump
# (older VK exports rendered service messages in English in the message body
# when the client locale was non-Russian). Names are synthetic, the phrasing
# is verbatim.


def test_english_leave_older_wording() -> None:
    att = parse_chat_event(f"{_u(100, 'Sample Actor')} left the conversation")
    assert att and att.data["subtype"] == "leave" and att.data["lang"] == "en"


def test_english_rejoin_older_wording() -> None:
    att = parse_chat_event(f"{_u(100, 'Sample Actor')} returned to the conversation")
    assert att and att.data["subtype"] == "rejoin"


def test_english_chat_create_with_title() -> None:
    att = parse_chat_event(
        f'{_u(100, "Sample Actor")} created the chat '
        f'«<b class="im_srv_lnk">Sample Title</b>»'
    )
    assert att and att.data["subtype"] == "chat_create"
    assert att.data["lang"] == "en"
    assert att.data["title_after"] == "Sample Title"


def test_english_chat_rename_changed_name_to() -> None:
    """Older VK wording: 'changed the conversation name to «…»'."""
    att = parse_chat_event(
        f'{_u(100, "Sample Actor")} changed the conversation name to '
        f'«<b class="im_srv_lnk">New Title</b>»'
    )
    assert att and att.data["subtype"] == "chat_rename"
    assert att.data["lang"] == "en"
    assert att.data["title_after"] == "New Title"


def test_english_kick_kicked_x_out() -> None:
    """Older VK wording: 'kicked <target> out' — the trailing 'out' must not
    break the verb match after the target <USER> is stripped.
    """
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} kicked {_u(200, 'Sample Target')} out"
    )
    assert att and att.data["subtype"] == "kick"
    assert att.data["lang"] == "en"
    assert att.data["target"]["vk_id"] == 200


def test_english_kick_modern_removed() -> None:
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} removed {_u(200, 'Sample Target')}"
    )
    assert att and att.data["subtype"] == "kick"
    assert att.data["target"]["vk_id"] == 200


def test_english_invite_older_wording() -> None:
    """'invited <target>' as a second <USER> link, matching older fallback_text."""
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} invited {_u(200, 'Sample Target')}"
    )
    assert att and att.data["subtype"] == "invite"
    assert att.data["lang"] == "en"


def test_english_chat_photo_set_changed_cover() -> None:
    """Older VK wording: 'changed conversation cover' (no 'the')."""
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} changed conversation cover"
    )
    assert att and att.data["subtype"] == "chat_photo_set"
    assert att.data["lang"] == "en"


def test_english_chat_photo_set_modern_wording() -> None:
    att = parse_chat_event(
        f"{_u(100, 'Sample Actor')} updated the chat photo"
    )
    assert att and att.data["subtype"] == "chat_photo_set"


# ----------------------------- schema integrity ------------------------------


def test_ru_patterns_cover_every_subtype() -> None:
    """Drift guard: the parser asserts this at import time too, but a unit
    test makes the contract explicit for future contributors.
    """
    assert set(_RU_PATTERNS) == set(CHAT_EVENT_SUBTYPES)


def test_subtype_tuple_matches_literal_union() -> None:
    """``CHAT_EVENT_SUBTYPES`` must list exactly the slugs allowed by the
    ``ChatEventSubtype`` Literal. If you add a slug to the Literal, add it
    here too — and vice versa.
    """
    from vkdump.parsers.vk.chat_events import ChatEventSubtype
    from typing import get_args

    assert set(get_args(ChatEventSubtype)) == set(CHAT_EVENT_SUBTYPES)
