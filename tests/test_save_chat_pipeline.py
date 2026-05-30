"""Regression coverage for the save-chat photo pipeline.

Synthetic HTML pages run through `_transform_page` and
`InlinePhotosTransform` directly — no DB, no real downloads, no GUI.
Each test asserts a specific invariant of the splice-and-inject path
the user can already break by hand.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from vkdump.modules.save_chat.pipeline import AttMeta, TransformContext
from vkdump.modules.save_chat.run import _transform_page
from vkdump.modules.save_chat.transforms import InlinePhotosTransform


def _msg_block(msg_id: int, header: str, body_inner: str) -> str:
    """Wrap one message in VK's `<div class="item"><div class="item__main">…`
    envelope, including the `<div class="kludges">` block where
    attachments live — without it the parser sees zero attachments.
    """
    return (
        f'<div class="item"><div class=\'item__main\'>'
        f'<div class="message" data-id="{msg_id}">'
        f'<div class="message__header">{header}</div>'
        f'<div>caption<div class="kludges">{body_inner}</div></div>'
        f'</div></div></div>'
    )


def _photo_attachment(url: str) -> str:
    return (
        '<div class="attachment">'
        '<div class="attachment__description">Фотография</div>'
        f"<a class='attachment__link' href='{url}'>{url}</a>"
        '</div>'
    )


def _file_attachment(url: str, label: str = "Файл") -> str:
    return (
        '<div class="attachment">'
        f'<div class="attachment__description">{label}</div>'
        f"<a class='attachment__link' href='{url}'>{url}</a>"
        '</div>'
    )


def _page(body_chunks: list[str]) -> str:
    return (
        '<html><body>'
        + "".join(body_chunks)
        + '</body></html>'
    )


@pytest.fixture
def ctx(tmp_path: Path):
    """A live-ish TransformContext with two pre-stashed local photos
    so the injector can find files on disk."""
    static = tmp_path / "static"
    pages_dir = tmp_path / "out" / "messages"
    chat_out = tmp_path / "out"
    pages_dir.mkdir(parents=True)
    # Hash-shaped names so bucket = stem[:2] gives a stable 2-char dir,
    # matching how enrich.py names downloaded files.
    files = {
        "aa00aa00aa00aa00.jpg": "https://cdn.example.com/a.jpg?size=100x100",
        "bb11bb11bb11bb11.jpg": "https://cdn.example.com/b.jpg?size=200x200",
        "cc22cc22cc22cc22.jpg": "https://cdn.example.com/c.jpg?size=300x300",
    }
    url_to_meta = {}
    for nm, url in files.items():
        bucket = nm[:2]
        p = static / "photo" / bucket / nm
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"\xff\xd8\xff" + b"x" * 200)
        url_to_meta[url] = AttMeta(
            local_path=f"photo/{bucket}/{nm}",
            resolution=url.split("size=", 1)[1],
            file_size=203,
        )
    return TransformContext(
        chat_id=1,
        chat_source_folder="x",
        output_chat_dir=chat_out,
        pages_dir=pages_dir,
        static_root=static,
        url_to_meta=url_to_meta,
    )


# ---------- single-message tests ----------


def test_two_photos_in_one_message_both_inject(ctx: TransformContext) -> None:
    body = _photo_attachment("https://cdn.example.com/a.jpg?size=100x100") \
         + _photo_attachment("https://cdn.example.com/b.jpg?size=200x200")
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, blocks, changed = _transform_page(html, "t.html", ctx)
    assert blocks == 1
    assert changed == 1
    # Two injects (one per photo), both original links preserved.
    assert new_html.count("vkdump-inline-photo") == 2
    assert new_html.count("<img") == 2
    assert new_html.count("attachment__link") == 2
    # And the local hrefs point at the right bucketed files, relative
    # to messages/ (one level up = chat root).
    assert "../assets/2024/photos/aa/aa00aa00aa00aa00.jpg" in new_html
    assert "../assets/2024/photos/bb/bb11bb11bb11bb11.jpg" in new_html


def test_photo_mixed_with_file_attachment_only_photo_injects(ctx: TransformContext) -> None:
    body = _photo_attachment("https://cdn.example.com/a.jpg?size=100x100") \
         + _file_attachment("https://example.com/doc.pdf")
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    # Photo: one inject. File: untouched (we don't download files yet).
    assert new_html.count("vkdump-inline-photo") == 1
    assert "doc.pdf" in new_html  # original file link preserved verbatim
    assert new_html.count("attachment__link") == 2


def test_message_without_attachments_is_unchanged(ctx: TransformContext) -> None:
    body = "просто текст без аттачей"
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, changed = _transform_page(html, "t.html", ctx)
    assert changed == 0
    assert new_html == html  # byte-identical


def test_photo_without_local_file_is_skipped(ctx: TransformContext) -> None:
    body = _photo_attachment("https://cdn.example.com/never-downloaded.jpg")
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, changed = _transform_page(html, "t.html", ctx)
    assert changed == 0
    assert "vkdump-inline-photo" not in new_html
    assert "never-downloaded.jpg" in new_html


def test_vk_com_urls_skipped_even_if_in_url_to_meta(ctx: TransformContext, tmp_path: Path) -> None:
    """on-site vk.com URLs are excluded by design (match enrich's queue
    exclusion). Make sure the transform respects that even if a stale
    meta entry happens to exist."""
    url = "https://vk.com/im?some_inline_photo"
    ctx.url_to_meta[url] = AttMeta(
        local_path="photo/a./a.jpg", resolution="x", file_size=1,
    )
    body = _photo_attachment(url)
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    assert "vkdump-inline-photo" not in new_html


# ---------- multi-message tests ----------


def test_multi_message_page_preserves_order_and_ids(ctx: TransformContext) -> None:
    msg_ids = [1001, 1002, 1003, 1004]
    bodies = [
        _photo_attachment("https://cdn.example.com/a.jpg?size=100x100"),
        "no attachments here",
        _photo_attachment("https://cdn.example.com/b.jpg?size=200x200"),
        _photo_attachment("https://cdn.example.com/c.jpg?size=300x300"),
    ]
    blocks_html = [
        _msg_block(mid, f"Вы, {i} янв 2024 в 12:00:00", body)
        for i, (mid, body) in enumerate(zip(msg_ids, bodies), start=1)
    ]
    # Add filler text between messages — pagination markers, comments,
    # whatever — to make sure the splice preserves it.
    inter = "\n<!-- separator -->\n"
    html = _page([inter.join(blocks_html)])

    new_html, blocks, changed = _transform_page(html, "t.html", ctx)

    assert blocks == 4
    assert changed == 3  # only the photo messages

    # IDs must appear in original order, no shuffling.
    positions = [new_html.index(f'data-id="{mid}"') for mid in msg_ids]
    assert positions == sorted(positions), (
        f"messages shuffled: ids appear at {positions}"
    )

    # Gap content (the separator) survived around every gap.
    assert new_html.count("<!-- separator -->") == 3


def test_dates_stay_attached_to_their_message_ids(ctx: TransformContext) -> None:
    """The smoking-gun symptom from the user's report: date floating
    between message blocks. The id->date binding lives inside the
    message__header div, which we never touch — assert it survives a
    full pipeline pass."""
    pairs = [
        (1994448, "12 фев 2015 в 19:43:10"),
        (4341765, "26 сен 2023 в 18:20:45"),
    ]
    body = _photo_attachment("https://cdn.example.com/a.jpg?size=100x100")
    blocks_html = [_msg_block(mid, f"Вы, {date}", body) for mid, date in pairs]
    html = _page(blocks_html)

    new_html, _, _ = _transform_page(html, "t.html", ctx)

    # For every (id, date) pair, the date must still appear inside
    # the same data-id="..." block — i.e. the date must come after
    # that id's opening tag and before the next message's opening.
    for mid, date in pairs:
        marker = f'data-id="{mid}"'
        start = new_html.index(marker)
        # Find the next message opener (if any) so we bound the search.
        rest = new_html[start + len(marker):]
        next_msg = rest.find('<div class="message" data-id=')
        scope = rest if next_msg < 0 else rest[:next_msg]
        assert date in scope, (
            f"date {date!r} not inside the block for id {mid}"
            f" — splice probably misaligned"
        )


def test_byte_for_byte_when_no_attachments_match(ctx: TransformContext) -> None:
    """When nothing gets injected, the output must be byte-identical to
    the input. This is the strongest guarantee against splice drift."""
    body = _file_attachment("https://example.com/doc.pdf") \
         + "<p>some other markup</p>"
    blocks_html = [
        _msg_block(1, "Вы, 1 янв 2024 в 12:00:00", body),
        _msg_block(2, "Вы, 2 янв 2024 в 12:00:00", body),
    ]
    html = _page(blocks_html)
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    assert new_html == html


# ---------- counters ----------


def test_counters_count_per_attachment_not_per_block(ctx: TransformContext) -> None:
    body = (
        _photo_attachment("https://cdn.example.com/a.jpg?size=100x100")
        + _photo_attachment("https://cdn.example.com/b.jpg?size=200x200")
        + _photo_attachment("https://example.com/never.jpg")  # no local
    )
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    _transform_page(html, "t.html", ctx)
    # 3 photo mentions (all have URLs, none vk.com), 2 inlined.
    assert ctx.candidates_by_kind.get("photo") == 3
    assert ctx.injected_by_kind.get("photo") == 2


# ---------- known-edge / open question ----------


def test_same_url_twice_in_one_message_known_limitation(ctx: TransformContext) -> None:
    """Documents current behaviour for the rare same-URL-twice case
    (e.g. a forwarded photo quoted twice in the same message). The
    regex-based injector finds the first occurrence both times, so
    the second `<a>` never gets the image — but the page stays
    structurally valid. If this becomes a real problem we'll switch
    to a finditer-based pass that tracks per-URL ordinal."""
    url = "https://cdn.example.com/a.jpg?size=100x100"
    body = _photo_attachment(url) + _photo_attachment(url)
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    # Both original links survive (no replacement, just injection).
    assert new_html.count(f"href='{url}'") == 2
    # Current behaviour: first link gets two stacked injections, the
    # second one stays bare. If this ever changes to "one inject per
    # occurrence" the test will break and we update the assertion.
    assert new_html.count("vkdump-inline-photo") == 2
