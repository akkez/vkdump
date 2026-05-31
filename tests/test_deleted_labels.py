"""Regression tests for core.deleted_labels.DeletedLabelPicker.

Synthetic fixtures only — no real VK ids, names, or message bodies. The
two-letter Cyrillic placeholder names "Aa Bb" / "Cc Dd" exercise the
Unicode-uppercase rule without resembling any real archive.
"""
from vkdump.core.deleted_labels import (
    DeletedLabelPicker,
    format_combined_label,
)


def test_pick_pair_beats_single_and_at() -> None:
    p = DeletedLabelPicker([100])
    p.feed("hello [id100|Aa Bb] there")
    p.feed("[id100|@handle] hi")
    p.feed("[id100|Aa]")
    assert p.pick(100) == ("Aa Bb", "pair")


def test_pick_at_when_no_pair() -> None:
    p = DeletedLabelPicker([100])
    p.feed("[id100|@handle]")
    p.feed("[id100|@handle]")
    p.feed("[id100|Aa]")
    assert p.pick(100) == ("@handle", "at")


def test_pick_rejects_at_id_handle() -> None:
    p = DeletedLabelPicker([100])
    p.feed("[id100|@id12345]")
    assert p.pick(100) == ("DELETED", "fallback")


def test_pick_fallback_for_unknown_user() -> None:
    p = DeletedLabelPicker([100])
    assert p.pick(999) == ("DELETED", "fallback")


def test_ignores_non_deleted_ids() -> None:
    p = DeletedLabelPicker([100])
    p.feed("[id200|Aa Bb]")
    assert p.pick(100) == ("DELETED", "fallback")


# ---- chat_event sourcing ----


def _payload(**kw):
    base = {"subtype": "leave", "lang": "ru"}
    base.update(kw)
    return base


def test_event_kludges_name_wins_over_inline() -> None:
    p = DeletedLabelPicker([100])
    p.feed("[id100|Xx Yy]")
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 100, "display_name": "Aa Bb",
               "profile_url": "https://vk.com/id100"},
    ))
    assert p.pick(100) == ("Aa Bb", "event")


def test_event_ignores_kludges_deleted_placeholder() -> None:
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 100, "display_name": "DELETED",
               "profile_url": "https://vk.com/id100"},
    ))
    assert p.pick(100) == ("DELETED", "fallback")


def test_event_fallback_text_actor_only_subtype() -> None:
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 100, "display_name": "DELETED",
               "profile_url": "https://vk.com/id100"},
        fallback_text="Aa Bb покинул беседу",
    ))
    assert p.pick(100) == ("Aa Bb", "event")


def test_event_fallback_text_rejoin_feminine() -> None:
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="rejoin",
        actor={"vk_id": 100, "display_name": "DELETED",
               "profile_url": "https://vk.com/id100"},
        fallback_text="Aa Bb вернулась в беседу",
    ))
    assert p.pick(100) == ("Aa Bb", "event")


def test_event_fallback_text_invite_extracts_both() -> None:
    p = DeletedLabelPicker([100, 200])
    p.feed_chat_event({
        "subtype": "invite",
        "lang": "ru",
        "actor": {"vk_id": 100, "display_name": "DELETED",
                  "profile_url": "https://vk.com/id100"},
        "target": {"vk_id": 200, "display_name": "DELETED",
                   "profile_url": "https://vk.com/id200"},
        "fallback_text": "Aa Bb пригласил Cc Dd",
    })
    assert p.pick(100) == ("Aa Bb", "event")
    assert p.pick(200) == ("Cc Dd", "event")


def test_event_fallback_text_kick_english() -> None:
    p = DeletedLabelPicker([100, 200])
    p.feed_chat_event({
        "subtype": "kick",
        "lang": "en",
        "actor": {"vk_id": 100, "display_name": "DELETED",
                  "profile_url": "https://vk.com/id100"},
        "target": {"vk_id": 200, "display_name": "DELETED",
                   "profile_url": "https://vk.com/id200"},
        "fallback_text": "Aa Bb kicked Cc Dd out",
    })
    assert p.pick(100) == ("Aa Bb", "event")
    assert p.pick(200) == ("Cc Dd", "event")


def test_event_fallback_text_chat_photo_set_ru_inflected() -> None:
    """The RU body uses 'фотографию' (accusative), not 'фотограф'.
    Pattern must absorb morphological endings."""
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="chat_photo_set",
        actor={"vk_id": 100, "display_name": "DELETED",
               "profile_url": "https://vk.com/id100"},
        fallback_text="Aa Bb обновил фотографию беседы",
    ))
    assert p.pick(100) == ("Aa Bb", "event")


def test_event_fallback_text_chat_rename() -> None:
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="chat_rename",
        actor={"vk_id": 100, "display_name": "DELETED",
               "profile_url": "https://vk.com/id100"},
        fallback_text="Aa Bb изменил название беседы на «новое»",
    ))
    assert p.pick(100) == ("Aa Bb", "event")


def test_event_fallback_text_literal_DELETED_is_ignored() -> None:
    """Old VK clients render 'X removed Y' with literal 'DELETED' in
    both slots when both users were deleted at dump time — must not
    surface 'DELETED' as a recovered label."""
    p = DeletedLabelPicker([100, 200])
    p.feed_chat_event({
        "subtype": "kick",
        "lang": "en",
        "actor": {"vk_id": 100, "display_name": "DELETED",
                  "profile_url": "https://vk.com/id100"},
        "target": {"vk_id": 200, "display_name": "DELETED",
                   "profile_url": "https://vk.com/id200"},
        "fallback_text": "DELETED removed DELETED",
    })
    assert p.pick(100) == ("DELETED", "fallback")
    assert p.pick(200) == ("DELETED", "fallback")


def test_event_fallback_text_unknown_subtype_skipped() -> None:
    """Placeholder subtypes (message_pin, join_by_link, …) carry the
    generic 'not supported by your application' body — must not be parsed."""
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="message_pin",
        actor={"vk_id": 100, "display_name": "DELETED",
               "profile_url": "https://vk.com/id100"},
        fallback_text="Сообщение не поддерживается Вашим приложением.",
    ))
    assert p.pick(100) == ("DELETED", "fallback")


def test_event_frequency_picks_most_common_pair() -> None:
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 100, "display_name": "Aa Bb",
               "profile_url": "https://vk.com/id100"},
    ))
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 100, "display_name": "Aa Bb",
               "profile_url": "https://vk.com/id100"},
    ))
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 100, "display_name": "Ee Ff",
               "profile_url": "https://vk.com/id100"},
    ))
    assert p.pick(100) == ("Aa Bb", "event")


def test_event_single_name_used_when_no_pair_available() -> None:
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 100, "display_name": "Mononym",
               "profile_url": "https://vk.com/id100"},
    ))
    assert p.pick(100) == ("Mononym", "event")


def test_event_ignores_non_deleted_actor() -> None:
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 999, "display_name": "Aa Bb",
               "profile_url": "https://vk.com/id999"},
        fallback_text="Aa Bb покинул беседу",
    ))
    assert p.pick(100) == ("DELETED", "fallback")


def test_feed_many_handles_none() -> None:
    p = DeletedLabelPicker([100])
    p.feed_many([None, "", "[id100|Aa Bb]"])
    assert p.pick(100) == ("Aa Bb", "pair")


# ---- pick_multi + format_combined_label ----


def test_pick_multi_returns_event_then_inline_pair() -> None:
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 100, "display_name": "Aa Bb",
               "profile_url": "https://vk.com/id100"},
    ))
    p.feed("[id100|Cc Dd]")
    assert p.pick_multi(100) == [("Aa Bb", "event"), ("Cc Dd", "pair")]


def test_pick_multi_dedupes_identical_label_across_buckets() -> None:
    """When the same label wins in two buckets, only the higher-priority
    bucket entry survives — no duplicates in the final list."""
    p = DeletedLabelPicker([100])
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 100, "display_name": "Aa Bb",
               "profile_url": "https://vk.com/id100"},
    ))
    p.feed("[id100|Aa Bb]")
    assert p.pick_multi(100) == [("Aa Bb", "event")]


def test_pick_multi_empty_for_unknown_user() -> None:
    p = DeletedLabelPicker([100])
    assert p.pick_multi(100) == []
    assert p.pick_multi(999) == []


def test_pick_multi_collects_pair_at_single_when_no_event() -> None:
    p = DeletedLabelPicker([100])
    p.feed("[id100|Aa Bb]")
    p.feed("[id100|@handle]")
    p.feed("[id100|Mononym]")
    picks = p.pick_multi(100)
    assert picks == [
        ("Aa Bb", "pair"),
        ("@handle", "at"),
        ("Mononym", "single"),
    ]


def test_pick_multi_ignores_re_run_output() -> None:
    """Mentions / event names that look like a prior run's bracketed
    output ('DELETED (...)') must not feed back into the picker —
    otherwise re-runs would compound their own labels."""
    p = DeletedLabelPicker([100])
    p.feed("[id100|DELETED]")
    p.feed("[id100|DELETED (Aa Bb)]")
    p.feed_chat_event(_payload(
        subtype="leave",
        actor={"vk_id": 100, "display_name": "DELETED (Aa Bb)",
               "profile_url": "https://vk.com/id100"},
    ))
    assert p.pick_multi(100) == []


def test_format_combined_label_no_picks() -> None:
    assert format_combined_label([]) == "DELETED"


def test_format_combined_label_one_pick() -> None:
    assert format_combined_label([("Aa Bb", "event")]) == "DELETED (Aa Bb)"


def test_format_combined_label_two_picks_slash_joined() -> None:
    picks = [("Aa Bb", "event"), ("Cc Dd", "pair")]
    assert format_combined_label(picks) == "DELETED (Aa Bb / Cc Dd)"
