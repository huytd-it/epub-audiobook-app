"""Merging the book description into a playlist's description."""
from app.youtube import merge_playlist_description

MARKER = "[epub-audiobook-app book:1]"


def test_empty_playlist_takes_the_book_description():
    assert merge_playlist_description("", "Sách hay", MARKER) == "Sách hay"


def test_existing_text_is_kept_and_marker_stays_last():
    merged = merge_playlist_description(f"Cũ\r\n{MARKER}", "Sách hay", MARKER)
    assert merged == f"Cũ\n\nSách hay\n{MARKER}"


def test_already_present_description_needs_no_update():
    assert merge_playlist_description(f"Sách hay\n{MARKER}", "Sách hay", MARKER) is None


def test_empty_addition_needs_no_update():
    assert merge_playlist_description("Cũ", "  ", MARKER) is None
