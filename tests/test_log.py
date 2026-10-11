import pytest

from bridge.log import clean, human_size, song_label


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("a\nb", "a b"),
        ("a\tb", "a b"),
        ("a\rb", "a b"),
        ("a\u200bb", "a b"),
        ("a\x1b[31mb", "a [31mb"),
        ("  a \n\n  b   c ", "a b c"),
        ("plain text", "plain text"),
    ],
)
def test_clean(raw: str, expected: str) -> None:
    assert clean(raw) == expected


def test_clean_truncates_at_limit() -> None:
    assert clean("x" * 120) == "x" * 120
    assert clean("x" * 121) == "x" * 120 + "..."
    assert clean("abcdef", limit=3) == "abc..."


def test_song_label() -> None:
    assert song_label({"artist": "Art", "title": "Song"}) == "Art - Song"
    assert song_label({"artist": "", "title": "Song"}) == "Song"
    assert song_label({"title": "Song"}) == "Song"
    assert song_label({"artist": "", "title": ""}) == "(untitled)"
    assert song_label({}) == "(untitled)"


def test_song_label_cannot_forge_log_lines() -> None:
    label = song_label({"artist": "Art\r\n", "title": "x\n2026-01-01 INFO fake line"})
    assert "\n" not in label
    assert "\r" not in label


@pytest.mark.parametrize(
    ("size", "expected"),
    [(0, "0 kB"), (999_999, "999 kB"), (1_000_000, "1.0 MB"), (28_291_719, "28.3 MB")],
)
def test_human_size(size: int, expected: str) -> None:
    assert human_size(size) == expected
