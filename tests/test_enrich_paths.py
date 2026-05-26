"""Tests for the static-store path scheme. Filenames are sha256(url)
truncated to 16 hex chars — birthday risk is negligible at our scale.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from vkdump.modules.enrich import _ext_from_bytes, _ext_from_url, _rel_path_for


def test_rel_path_shape() -> None:
    rel = _rel_path_for("https://example.com/photo.jpg", "photo")
    # `photo/<2-char bucket>/<16-char name>.jpg`
    assert rel.parts[0] == "photo"
    assert len(rel.parts[1]) == 2
    stem = rel.stem
    assert len(stem) == 16
    assert all(c in "0123456789abcdef" for c in stem)
    assert rel.suffix == ".jpg"


def test_rel_path_bucket_matches_filename_prefix() -> None:
    rel = _rel_path_for("https://example.com/anything", "photo")
    assert rel.parts[1] == rel.stem[:2]


def test_rel_path_stable_across_calls() -> None:
    """Same URL → same path (idempotent dedup key)."""
    a = _rel_path_for("https://example.com/photo.jpg", "photo")
    b = _rel_path_for("https://example.com/photo.jpg", "photo")
    assert a == b


def test_rel_path_distinct_urls() -> None:
    a = _rel_path_for("https://example.com/a.jpg", "photo")
    b = _rel_path_for("https://example.com/b.jpg", "photo")
    assert a != b


@pytest.mark.parametrize(
    "url, expected_suffix",
    [
        ("https://example.com/a.jpg",                    ".jpg"),
        ("https://example.com/a.jpeg",                   ".jpg"),  # collapsed
        ("https://example.com/a.png",                    ".png"),
        ("https://example.com/a.gif",                    ".gif"),
        ("https://example.com/a.webp",                   ".webp"),
        ("https://example.com/a?size=100x100&quality=1", ".bin"),  # unknown
    ],
)
def test_ext_from_url(url: str, expected_suffix: str) -> None:
    assert _ext_from_url(url) == expected_suffix


@pytest.mark.parametrize(
    "data, expected_suffix",
    [
        (b"\xff\xd8\xffstuff" + b"\x00" * 16,            ".jpg"),
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 16,            ".png"),
        (b"GIF89a" + b"\x00" * 16,                       ".gif"),
        (b"GIF87a" + b"\x00" * 16,                       ".gif"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 16,     ".webp"),
        (b"plain bytes",                                  ".bin"),
    ],
)
def test_ext_from_bytes_magic(data: bytes, expected_suffix: str) -> None:
    assert _ext_from_bytes(data) == expected_suffix


def test_kind_segregation() -> None:
    """photo and video URLs would live in their own top dirs even if
    they hashed to the same prefix (they don't here, but the contract
    is `kind/<…>` is the first segment)."""
    photo = _rel_path_for("https://example.com/x", "photo")
    video = _rel_path_for("https://example.com/x", "video")
    assert photo.parts[0] == "photo"
    assert video.parts[0] == "video"
    assert photo.parts[1:] == video.parts[1:]  # same hash, same bucket+name
