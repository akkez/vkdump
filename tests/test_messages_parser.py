"""End-to-end tests for `vkdump.parsers.vk.messages.parse_page`.

Every HTML snippet here is synthetic — names, ids, dates, URLs are
placeholders. The structural details (tags, attributes, quoting,
nesting, NBSP, ZWJ, edited-span, "Вы"/"You", etc.) are reproduced from
real VK dumps we've parsed so the tests cover the regex / balanced-div
machinery that the live parser relies on.
"""
from __future__ import annotations

from datetime import datetime

import pytest

from vkdump.parsers.vk.messages import parse_page
from vkdump.parsers.vk.models import (
    KIND_APP_ACTION,
    KIND_ARTICLE,
    KIND_ARTIST,
    KIND_AUDIO,
    KIND_CALL,
    KIND_CHANNEL_MESSAGE,
    KIND_COMMUNITY_DONATION,
    KIND_DELETED_MESSAGE,
    KIND_FILE,
    KIND_FORWARD,
    KIND_GEO,
    KIND_GIFT,
    KIND_LINK,
    KIND_MARKET_ALBUM,
    KIND_MARKET_ITEM,
    KIND_MOMENT,
    KIND_MONEY_REQUEST,
    KIND_PHOTO,
    KIND_PHOTO_ALBUM,
    KIND_PLAYLIST,
    KIND_PODCAST,
    KIND_POLL,
    KIND_STICKER,
    KIND_STORY,
    KIND_UNKNOWN,
    KIND_VIDEO,
    KIND_WALL_COMMENT,
    KIND_WALL_POST,
    KIND_WIDGET,
)


def _wrap(message_inner: str, msg_id: int = 1) -> str:
    """Wrap an inner `<div class="message">…</div>` payload in the
    `<div class="item">` envelope VK uses on every page.
    """
    return (
        f'<div class="item"><div class="item__main">'
        f'<div class="message" data-id="{msg_id}">{message_inner}</div>'
        f'</div></div>'
    )


def _parse_one(html: str):
    """Convenience: parse and assert there are exactly zero parse errors
    and exactly one message. Returns that message.
    """
    msgs, errs = parse_page(html, source_file="t.html")
    assert errs == [], f"unexpected parse errors: {[e.error for e in errs]}"
    assert len(msgs) == 1
    return msgs[0]


# ----------------------------- senders ------------------------------


def test_sender_user_id_link_ru() -> None:
    """`vk.com/id<N>` link + Russian date."""
    m = _parse_one(_wrap(
        '<div class="message__header"><a href="https://vk.com/id12345">'
        'Ivan Ivanov</a>, 2 фев 2020 в 22:46:22</div>'
        '<div>hi<div class="kludges"></div></div>'
    ))
    assert m.sender_vk_id == 12345
    assert m.sender_display_name == "Ivan Ivanov"
    assert m.sender_is_self is False
    assert m.sent_at == datetime(2020, 2, 2, 22, 46, 22)


def test_sender_user_id_link_en() -> None:
    """English `at H:MM:SS am/pm on D MMM YYYY` date format."""
    m = _parse_one(_wrap(
        '<div class="message__header"><a href="https://vk.com/id12345">'
        'Jane Doe</a>, at 7:15:40 pm on 3 Nov 2014</div>'
        '<div>hello<div class="kludges"></div></div>'
    ))
    assert m.sender_vk_id == 12345
    assert m.sender_display_name == "Jane Doe"
    assert m.sent_at == datetime(2014, 11, 3, 19, 15, 40)


@pytest.mark.parametrize(
    "prefix, expected_signed",
    [
        ("public", -100),   # community
        ("club", -100),     # community alias
        ("event", -100),    # event community
    ],
)
def test_sender_community_link_signed(prefix: str, expected_signed: int) -> None:
    """public/club/event prefixes all collapse to a negative vk_id."""
    m = _parse_one(_wrap(
        f'<div class="message__header"><a href="https://vk.com/{prefix}100">'
        f'Example Group</a>, 1 янв 2024 в 0:00:00</div>'
        f'<div>x<div class="kludges"></div></div>'
    ))
    assert m.sender_vk_id == expected_signed
    assert m.sender_display_name == "Example Group"


def test_sender_self_ru() -> None:
    m = _parse_one(_wrap(
        '<div class="message__header">Вы, 1 янв 2024 в 0:00:00</div>'
        '<div>hi<div class="kludges"></div></div>'
    ))
    assert m.sender_is_self is True
    assert m.sender_vk_id is None
    assert m.sender_display_name == "Вы"


def test_sender_self_en() -> None:
    m = _parse_one(_wrap(
        '<div class="message__header">You, at 1:00:00 pm on 1 Jan 2024</div>'
        '<div>hi<div class="kludges"></div></div>'
    ))
    assert m.sender_is_self is True
    assert m.sender_vk_id is None
    assert m.sender_display_name == "You"


def test_sender_plain_text_ru() -> None:
    """No link, Russian date — deleted community / unresolved sender."""
    m = _parse_one(_wrap(
        '<div class="message__header">Частное сообщество, '
        '29 ноя 2018 в 22:40:32</div>'
        '<div>hi<div class="kludges"></div></div>'
    ))
    assert m.sender_vk_id is None
    assert m.sender_is_self is False
    assert m.sender_display_name == "Частное сообщество"
    assert m.sent_at == datetime(2018, 11, 29, 22, 40, 32)


def test_sender_plain_text_en() -> None:
    """No link, English date."""
    m = _parse_one(_wrap(
        '<div class="message__header">Private community, '
        'at 5:54:20 pm on 10 Jul 2018</div>'
        '<div>hi<div class="kludges"></div></div>'
    ))
    assert m.sender_vk_id is None
    assert m.sender_display_name == "Private community"
    assert m.sent_at == datetime(2018, 7, 10, 17, 54, 20)


def test_sender_plain_text_email_like_en() -> None:
    """Plain-text sender that looks like an email — seen in dump."""
    m = _parse_one(_wrap(
        '<div class="message__header">support@example.com, '
        'at 7:01:31 pm on 31 Mar 2014</div>'
        '<div>hi<div class="kludges"></div></div>'
    ))
    assert m.sender_display_name == "support@example.com"
    assert m.sent_at == datetime(2014, 3, 31, 19, 1, 31)


def test_sender_deleted_name() -> None:
    """Linked user whose display_name is literally 'DELETED'."""
    m = _parse_one(_wrap(
        '<div class="message__header"><a href="https://vk.com/id77">'
        'DELETED</a>, 1 янв 2024 в 0:00:00</div>'
        '<div>x<div class="kludges"></div></div>'
    ))
    assert m.sender_vk_id == 77
    assert m.sender_display_name == "DELETED"


# ----------------------------- edited -------------------------------


def test_edited_span_extracts_edited_at() -> None:
    """`(ред.)` trailer carries the edit timestamp in the title attr."""
    m = _parse_one(_wrap(
        '<div class="message__header"><a href="https://vk.com/id1">'
        'X</a>, 14 фев 2018 в 17:46:15'
        "<span class='message-edited' title='14 фев 2018 в 19:11:09'>"
        ' (ред.)</span></div>'
        '<div>hi<div class="kludges"></div></div>'
    ))
    assert m.is_edited is True
    assert m.edited_at == datetime(2018, 2, 14, 19, 11, 9)
    assert m.sent_at == datetime(2018, 2, 14, 17, 46, 15)


def test_edited_span_double_quotes() -> None:
    """Both single- and double-quoted attribute forms."""
    m = _parse_one(_wrap(
        '<div class="message__header"><a href="https://vk.com/id1">'
        'X</a>, 14 фев 2018 в 17:46:15'
        '<span class="message-edited" title="14 фев 2018 в 19:11:09">'
        ' (ред.)</span></div>'
        '<div>hi<div class="kludges"></div></div>'
    ))
    assert m.is_edited is True
    assert m.edited_at == datetime(2018, 2, 14, 19, 11, 9)


# --------------------------- attachments -----------------------------


def _attachment_block(description: str, url: str | None = None) -> str:
    link = (
        f'<a class=\'attachment__link\' href=\'{url}\'>{url}</a>'
        if url else ""
    )
    return (
        f'<div class="attachment">'
        f'  <div class="attachment__description">{description}</div>'
        f'  {link}'
        f'</div>'
    )


def _message_with_attachments(*blocks: str) -> str:
    inner = "".join(blocks)
    return _wrap(
        '<div class="message__header">Вы, 1 янв 2024 в 0:00:00</div>'
        f'<div>caption<div class="kludges">{inner}</div></div>'
    )


@pytest.mark.parametrize(
    "description, expected_kind",
    [
        # Russian set.
        ("Фотография", KIND_PHOTO),
        ("Видеозапись", KIND_VIDEO),
        ("Аудиозапись", KIND_AUDIO),
        ("Файл", KIND_FILE),
        ("Запись на стене", KIND_WALL_POST),
        ("Комментарий на стене", KIND_WALL_COMMENT),
        ("Стикер", KIND_STICKER),
        ("Ссылка", KIND_LINK),
        ("Опрос", KIND_POLL),
        ("История", KIND_STORY),
        ("Подарок", KIND_GIFT),
        ("Карта", KIND_GEO),
        ("Запрос на денежный перевод", KIND_MONEY_REQUEST),
        ("Плейлист", KIND_PLAYLIST),
        ("Статья", KIND_ARTICLE),
        ("Сообщение удалено", KIND_DELETED_MESSAGE),
        ("Музыкант", KIND_ARTIST),
        ("Сообщество с VK Донатом", KIND_COMMUNITY_DONATION),  # NBSP
        ("Сообщество с VK Донатом", KIND_COMMUNITY_DONATION),            # plain
        ("Товар", KIND_MARKET_ITEM),
        ("Подкаст", KIND_PODCAST),
        ("Звонок", KIND_CALL),
        ("Виджет", KIND_WIDGET),
        ("Момент", KIND_MOMENT),
        ("Альбом фотографий", KIND_PHOTO_ALBUM),
        ("Подборка товаров", KIND_MARKET_ALBUM),
        # English set.
        ("Photo", KIND_PHOTO),
        ("Video", KIND_VIDEO),
        ("Audio", KIND_AUDIO),
        ("File", KIND_FILE),
        ("Wall post", KIND_WALL_POST),
        ("Wall comment", KIND_WALL_COMMENT),
        ("Sticker", KIND_STICKER),
        ("Link", KIND_LINK),
        # Locale-independent placeholder strings VK left untranslated.
        ("attachment app action", KIND_APP_ACTION),
        ("attachment channel message", KIND_CHANNEL_MESSAGE),
    ],
)
def test_attachment_kind_mapping(description: str, expected_kind: str) -> None:
    m = _parse_one(_message_with_attachments(_attachment_block(description)))
    assert len(m.attachments) == 1
    assert m.attachments[0].kind == expected_kind
    assert m.attachments[0].description.replace(" ", " ") == description.replace(" ", " ")


def test_attachment_with_url_preserves_url() -> None:
    m = _parse_one(_message_with_attachments(
        _attachment_block("Photo", url="https://example.com/p.jpg")
    ))
    assert m.attachments[0].url == "https://example.com/p.jpg"


def test_multiple_attachments_keep_order() -> None:
    m = _parse_one(_message_with_attachments(
        _attachment_block("Photo", url="https://example.com/a.jpg"),
        _attachment_block("Video", url="https://example.com/b.mp4"),
        _attachment_block("Sticker"),
    ))
    kinds = [a.kind for a in m.attachments]
    assert kinds == [KIND_PHOTO, KIND_VIDEO, KIND_STICKER]
    assert [a.position for a in m.attachments] == [0, 1, 2]


def test_unknown_attachment_marks_message_not_fully_parsed() -> None:
    m = _parse_one(_message_with_attachments(
        _attachment_block("Совсем новый тип вложения")
    ))
    assert m.attachments[0].kind == KIND_UNKNOWN
    assert m.fully_parsed is False


# ----------------------------- forwards ------------------------------


@pytest.mark.parametrize(
    "description, expected_count",
    [
        ("1 прикреплённое сообщение", 1),
        ("2 прикреплённых сообщения", 2),
        ("5 прикреплённых сообщений", 5),
        ("85 прикреплённых сообщений", 85),
        ("1 attached message", 1),
        ("12 attached messages", 12),
    ],
)
def test_forward_attachment(description: str, expected_count: int) -> None:
    m = _parse_one(_message_with_attachments(_attachment_block(description)))
    assert len(m.attachments) == 1
    assert m.attachments[0].kind == KIND_FORWARD
    assert m.attachments[0].forward_count == expected_count
    assert m.has_forwards is True
    assert m.forwarded_count == expected_count


# --------------------------- body / text -----------------------------


def test_text_with_br_becomes_newline() -> None:
    m = _parse_one(_wrap(
        '<div class="message__header">Вы, 1 янв 2024 в 0:00:00</div>'
        '<div>first<br>second<br>third<div class="kludges"></div></div>'
    ))
    assert m.text == "first\nsecond\nthird"


def test_empty_text_when_only_attachment() -> None:
    m = _parse_one(_message_with_attachments(_attachment_block("Photo")))
    # `caption` is the body text we put before kludges in the helper.
    assert m.text == "caption"


def test_html_entities_decoded() -> None:
    m = _parse_one(_wrap(
        '<div class="message__header">Вы, 1 янв 2024 в 0:00:00</div>'
        '<div>&lt;hello&gt; &amp; goodbye<div class="kludges"></div></div>'
    ))
    assert m.text == "<hello> & goodbye"


def test_service_message_kludges_marks_not_fully_parsed() -> None:
    """`im_srv_lnk` inside kludges = service message; we don't model
    those yet, so the message is kept with `fully_parsed=False` and
    its raw_html is preserved by the orchestrator.
    """
    html = _wrap(
        '<div class="message__header"><a href="https://vk.com/public10">'
        'Group</a>, 1 янв 2024 в 0:00:00</div>'
        '<div><div class="kludges">'
        '<a class="im_srv_lnk" href="https://vk.com/public10">Group</a>'
        ' created chat «<b class="im_srv_lnk">Hello</b>»'
        '</div></div>'
    )
    m = _parse_one(html)
    assert m.fully_parsed is False


# ----------------------------- multi ---------------------------------


def test_multiple_items_in_page() -> None:
    """Two consecutive item envelopes — most pages have 50."""
    page = _wrap(
        '<div class="message__header">Вы, 1 янв 2024 в 0:00:00</div>'
        '<div>a<div class="kludges"></div></div>',
        msg_id=1,
    ) + _wrap(
        '<div class="message__header"><a href="https://vk.com/id7">'
        'Other</a>, 2 янв 2024 в 0:00:00</div>'
        '<div>b<div class="kludges"></div></div>',
        msg_id=2,
    )
    msgs, errs = parse_page(page, source_file="t.html")
    assert errs == []
    assert [m.vk_message_id for m in msgs] == [1, 2]
    assert msgs[0].sender_is_self is True
    assert msgs[1].sender_vk_id == 7


def test_broken_item_isolated_from_good_ones() -> None:
    """A garbage header doesn't take the next item down with it."""
    page = _wrap(
        '<div class="message__header">no comma here just words</div>'
        '<div>a<div class="kludges"></div></div>',
        msg_id=1,
    ) + _wrap(
        '<div class="message__header">Вы, 1 янв 2024 в 0:00:00</div>'
        '<div>b<div class="kludges"></div></div>',
        msg_id=2,
    )
    msgs, errs = parse_page(page, source_file="t.html")
    assert len(errs) == 1
    assert len(msgs) == 1
    assert msgs[0].vk_message_id == 2
