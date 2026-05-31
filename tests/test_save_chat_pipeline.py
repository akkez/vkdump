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
from vkdump.modules.save_chat.transforms import (
    InlinePhotosTransform,
    MessageAnchorTransform,
    SenderNameFromDBTransform,
)
from vkdump.modules.save_chat.pipeline import apply_pipeline
from vkdump.parsers.vk.models import ParsedMessage


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
    assert new_html.count("vkdump-inline-photo") == 2
    assert new_html.count("<img") == 2
    # Original `attachment__link` anchors are absorbed into the caption
    # chip; the URL is preserved as the chip's href.
    assert "attachment__link" not in new_html
    assert 'href="https://cdn.example.com/a.jpg?size=100x100"' in new_html
    assert 'href="https://cdn.example.com/b.jpg?size=200x200"' in new_html
    # And the local hrefs point at the right bucketed files, relative
    # to messages/ (one level up = chat root).
    assert "../assets/2024/photos/aa/aa00aa00aa00aa00.jpg" in new_html
    assert "../assets/2024/photos/bb/bb11bb11bb11bb11.jpg" in new_html


def test_photo_mixed_with_file_attachment_only_photo_injects(ctx: TransformContext) -> None:
    body = _photo_attachment("https://cdn.example.com/a.jpg?size=100x100") \
         + _file_attachment("https://example.com/doc.pdf")
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    # Photo: one inject + photo's anchor absorbed into chip.
    # File: untouched (we don't download files yet) — its anchor stays.
    assert new_html.count("vkdump-inline-photo") == 1
    assert "doc.pdf" in new_html  # original file link preserved verbatim
    assert new_html.count("attachment__link") == 1


def test_message_without_attachments_gets_anchor_only(ctx: TransformContext) -> None:
    body = "просто текст без аттачей"
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, changed = _transform_page(html, "t.html", ctx)
    # Anchor transform stamps an id regardless of attachments.
    assert changed == 1
    assert 'id="m42"' in new_html
    assert "vkdump-inline-photo" not in new_html


def test_photo_without_local_file_is_skipped(ctx: TransformContext) -> None:
    body = _photo_attachment("https://cdn.example.com/never-downloaded.jpg")
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, changed = _transform_page(html, "t.html", ctx)
    # Anchor still injected; inline-photos is the one that no-ops here.
    assert changed == 1
    assert 'id="m42"' in new_html
    assert "vkdump-inline-photo" not in new_html
    assert "never-downloaded.jpg" in new_html
    # URL not in url_to_meta at all → counted as candidate only.
    assert ctx.candidates_by_kind.get("photo") == 1
    assert ctx.injected_by_kind.get("photo", 0) == 0
    assert ctx.missing_on_disk_by_kind.get("photo", 0) == 0


def test_db_ok_but_disk_missing_bumps_missing_counter(ctx: TransformContext) -> None:
    """The exact production failure mode the user hit: DB row claims
    download_status='ok' (so it's in url_to_meta), but the file is gone
    from data/static. Inject silently bails; the missing-on-disk
    counter has to make the gap visible in the summary."""
    url = "https://cdn.example.com/gone.jpg?size=999x999"
    ctx.url_to_meta[url] = AttMeta(
        local_path="photo/zz/zz99zz99zz99zz99.jpg",  # never created on disk
        resolution="999x999",
        file_size=1234,
    )
    body = _photo_attachment(url)
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    _transform_page(html, "t.html", ctx)
    assert ctx.candidates_by_kind.get("photo") == 1
    assert ctx.injected_by_kind.get("photo", 0) == 0
    assert ctx.missing_on_disk_by_kind.get("photo") == 1


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
    # All four are mutated: anchor transform stamps an id on every msg,
    # photo transform additionally injects on the three with photos.
    assert changed == 4

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


def test_anchor_injected_even_without_inlineable_attachments(
    ctx: TransformContext,
) -> None:
    """Inline-photos has nothing to do (no photo attachments), but the
    anchor transform always stamps `id="m<vk_id>"` on every message div
    so deep links work regardless of media."""
    body = _file_attachment("https://example.com/doc.pdf") \
         + "<p>some other markup</p>"
    blocks_html = [
        _msg_block(1, "Вы, 1 янв 2024 в 12:00:00", body),
        _msg_block(2, "Вы, 2 янв 2024 в 12:00:00", body),
    ]
    html = _page(blocks_html)
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    assert 'id="m1"' in new_html
    assert 'id="m2"' in new_html
    # data-id stays — anchor sits alongside, doesn't replace.
    assert 'data-id="1"' in new_html
    assert 'data-id="2"' in new_html


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


# ---------- URL compression on inline ----------


def test_inline_photo_compresses_original_anchor_to_hostname_chip(
    ctx: TransformContext,
) -> None:
    """The original `<a href="<URL>"><URL></a>` is replaced by a small
    hostname chip inside the caption whose href is the full URL —
    visual cleanup without info loss."""
    url = "https://cdn.example.com/a.jpg?size=100x100"
    body = _photo_attachment(url)
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    # Original full-URL link text is gone (no naked huge URL).
    assert f">{url}</a>" not in new_html
    assert "attachment__link" not in new_html
    # New compact link present: href preserved, text is hostname only.
    assert f'href="{url}"' in new_html
    assert ">cdn.example.com</a>" in new_html


def test_inline_photo_leaves_anchor_alone_when_text_doesnt_match(
    ctx: TransformContext,
) -> None:
    """If VK ever ships a link whose visible text isn't a verbatim copy
    of href, the compact-link branch must bail and leave the original
    anchor untouched — consistency rule: both transformations apply or
    neither does."""
    url = "https://cdn.example.com/a.jpg?size=100x100"
    # Hand-crafted attachment block where the anchor's text contains a
    # `<` character so the `[^<]*` text capture inside `_full_link_re`
    # fails. `link_re` still matches the opening tag → image still
    # injects, but the chip-link path bails.
    weird_anchor = (
        '<div class="attachment">'
        '<div class="attachment__description">Фотография</div>'
        f"<a class='attachment__link' href='{url}'>not-a-url <span/>tail</a>"
        '</div>'
    )
    html = _page([_msg_block(
        99, "Вы, 1 янв 2024 в 12:00:00", weird_anchor,
    )])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    # Image still injected (link_re matches the opening — that path is
    # independent of the full-link regex).
    assert "vkdump-inline-photo" in new_html
    # Hostname chip NOT added — full-link match failed.
    assert ">cdn.example.com</a>" not in new_html
    # Original anchor still there.
    assert "not-a-url" in new_html


# ---------- description-strip on inline ----------


def test_inline_photo_swallows_attachment_description(ctx: TransformContext) -> None:
    """`<div class="attachment__description">Photo</div>` sits above
    every `<a class="attachment__link">` in VK markup. When we inline
    the photo, that label becomes redundant — the <img> + the caption
    strip below carry the same info — and must be stripped."""
    body = _photo_attachment("https://cdn.example.com/a.jpg?size=100x100")
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    # The label is gone for the inlined photo.
    assert ">Фотография<" not in new_html
    assert ">Photo<" not in new_html
    # The URL is preserved as the caption chip's href.
    assert 'href="https://cdn.example.com/a.jpg?size=100x100"' in new_html


def test_description_kept_when_inline_is_skipped(ctx: TransformContext) -> None:
    """Description stays put when no inline happens (photo had no local
    file), otherwise other-kind attachments (file, audio, ...) would
    lose their only label too."""
    body = _photo_attachment("https://cdn.example.com/never-downloaded.jpg")
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    assert ">Фотография<" in new_html


def test_description_kept_for_non_photo_attachments(ctx: TransformContext) -> None:
    """File / audio / other-kind attachments are not inlined, so their
    description label must survive untouched."""
    body = _file_attachment("https://example.com/doc.pdf", label="Файл")
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    assert ">Файл<" in new_html


# ---------- sender-name-from-DB transform ----------


def _msg_block_with_sender(
    msg_id: int, vk_id: int, sender_name: str, body_inner: str = "",
) -> str:
    """A VK-style message block whose header carries an `<a>` link to
    the sender (id<num> or club<num>). Used to exercise the sender-name
    transform — the existing `_msg_block` helper only emits the
    self-message form."""
    prefix = "id" if vk_id > 0 else "club"
    n = abs(vk_id)
    header = (
        f'<a href="https://vk.com/{prefix}{n}">{sender_name}</a>'
        ', 1 янв 2024 в 12:00:00'
    )
    return (
        f'<div class="item"><div class=\'item__main\'>'
        f'<div class="message" data-id="{msg_id}">'
        f'<div class="message__header">{header}</div>'
        f'<div>caption<div class="kludges">{body_inner}</div></div>'
        f'</div></div></div>'
    )


def test_sender_transform_replaces_name(ctx: TransformContext) -> None:
    ctx.user_names = {123: "DELETED (Aa Bb)"}
    html = _page([_msg_block_with_sender(1, 123, "DELETED")])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    assert ">DELETED (Aa Bb)</a>" in new_html
    assert ">DELETED</a>" not in new_html
    # The href itself is preserved verbatim.
    assert 'href="https://vk.com/id123"' in new_html


def test_sender_transform_handles_community_negative_id(ctx: TransformContext) -> None:
    """club/public/event hrefs map to a negative peer id in DB. The
    transform must flip sign when looking the name up."""
    ctx.user_names = {-456: "Renamed Community"}
    html = _page([_msg_block_with_sender(2, -456, "Old Name")])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    assert ">Renamed Community</a>" in new_html
    assert 'href="https://vk.com/club456"' in new_html


def test_sender_transform_noop_when_db_lacks_user(ctx: TransformContext) -> None:
    """Unknown vk_id → header stays untouched."""
    ctx.user_names = {}
    html = _page([_msg_block_with_sender(3, 789, "Stranger")])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    assert ">Stranger</a>" in new_html


def test_sender_transform_noop_when_name_already_matches(
    ctx: TransformContext,
) -> None:
    """No subn fire when the existing text already equals the DB value
    (idempotent re-renders)."""
    ctx.user_names = {321: "Already Right"}
    html = _page([_msg_block_with_sender(4, 321, "Already Right")])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    assert new_html.count(">Already Right</a>") == 1


def test_sender_transform_escapes_html_in_name(ctx: TransformContext) -> None:
    """Names with `<` / `&` characters must be HTML-escaped before
    going back into the markup, else they'd break the document."""
    ctx.user_names = {654: "A & <evil>"}
    html = _page([_msg_block_with_sender(5, 654, "Foo")])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    assert ">A &amp; &lt;evil&gt;</a>" in new_html


# ---------- anchor transform ----------


def _bare_msg(vk_id: int, raw_html: str | None = None) -> ParsedMessage:
    """Minimal ParsedMessage suitable for testing transforms in isolation
    (no DB, no real parse). Only the fields the transform reads matter."""
    from datetime import datetime
    return ParsedMessage(
        vk_message_id=vk_id,
        sent_at=datetime(2024, 1, 1, 12, 0, 0),
        sender_vk_id=None,
        sender_display_name=None,
        sender_is_self=False,
        text="",
        attachments=[],
        has_forwards=False,
        forwarded_count=0,
        is_reply=False,
        reply_to_message_id=None,
        is_edited=False,
        edited_at=None,
        raw_html=(
            raw_html
            if raw_html is not None
            else f'<div class="message" data-id="{vk_id}"><p>x</p></div>'
        ),
        source_file="t.html",
        fully_parsed=True,
    )


def test_anchor_transform_adds_id(ctx: TransformContext) -> None:
    msg = _bare_msg(361522)
    MessageAnchorTransform().apply(msg, ctx)
    assert 'data-id="361522"' in msg.raw_html
    assert 'id="m361522"' in msg.raw_html


def test_anchor_transform_only_first_match(ctx: TransformContext) -> None:
    """Nested message divs (unlikely but possible in malformed dumps)
    must not get a second id — the anchor only stamps the outer block."""
    raw = (
        '<div class="message" data-id="1"><div>'
        '<div class="message" data-id="2">inner</div>'
        '</div></div>'
    )
    msg = _bare_msg(1, raw_html=raw)
    MessageAnchorTransform().apply(msg, ctx)
    assert msg.raw_html.count('id="m1"') == 1
    assert 'id="m2"' not in msg.raw_html


def test_anchor_transform_noop_on_already_anchored(ctx: TransformContext) -> None:
    """If the markup already has the id attribute (e.g. re-running
    save-chat over a prior output), the regex still only adds id once —
    not catastrophic, but worth pinning behavior."""
    raw = '<div class="message" data-id="42" id="m42">hi</div>'
    msg = _bare_msg(42, raw_html=raw)
    MessageAnchorTransform().apply(msg, ctx)
    assert msg.raw_html.count('id="m42"') == 1


# ---------- known-edge / open question ----------


def test_same_url_twice_in_one_message(ctx: TransformContext) -> None:
    """Same URL appearing twice in one message (rare — e.g. a forwarded
    photo quoted twice). Each loop iteration absorbs whichever original
    anchor sits first in the current html, so by the end both originals
    are replaced and the page has two inline blocks."""
    url = "https://cdn.example.com/a.jpg?size=100x100"
    body = _photo_attachment(url) + _photo_attachment(url)
    html = _page([_msg_block(42, "Вы, 1 янв 2024 в 12:00:00", body)])
    new_html, _, _ = _transform_page(html, "t.html", ctx)
    # Both originals consumed; only the chip-style links remain.
    assert "attachment__link" not in new_html
    assert new_html.count("vkdump-inline-photo") == 2
    # URL survives via the two chips' href attributes.
    assert new_html.count(f'href="{url}"') == 2
