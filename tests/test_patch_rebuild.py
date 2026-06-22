"""Unit tests for patch rebuild and build_patch_text."""
import sqlite3

import pytest

from app import db as app_db
from app import repository


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    app_db.init_schema(c)
    yield c
    c.close()


def _seed_book(conn, chapter_count: int = 10):
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES ('test', 't.epub', 't.epub', 10, 'ready', ?, ?)",
        (now, now),
    )
    book_id = cur.lastrowid
    for i in range(chapter_count):
        conn.execute(
            "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, i, f"Ch {i}", f"content of chapter {i} " * 5, 50),
        )
    conn.commit()
    return book_id


def test_rebuild_patches_creates_correct_patches(conn):
    book_id = _seed_book(conn, 10)
    patches = repository.rebuild_patches(conn, book_id, [(0, 3), (5, 9)])
    assert len(patches) == 2
    assert patches[0].patch_index == 0
    assert patches[0].chapter_start == 0
    assert patches[0].chapter_end == 3
    assert patches[1].patch_index == 1
    assert patches[1].chapter_start == 5
    assert patches[1].chapter_end == 9


def test_rebuild_overlapping_ranges_rejected(conn):
    book_id = _seed_book(conn, 10)
    with pytest.raises(ValueError, match="overlapping"):
        repository.rebuild_patches(conn, book_id, [(0, 5), (3, 7)])


def test_rebuild_excluded_chapter_in_range_rejected(conn):
    book_id = _seed_book(conn, 5)
    repository.set_chapter_excluded(conn, book_id, 2, True)
    with pytest.raises(ValueError, match="excluded chapter"):
        repository.rebuild_patches(conn, book_id, [(0, 4)])


def test_rebuild_out_of_bounds_rejected(conn):
    book_id = _seed_book(conn, 3)
    with pytest.raises(ValueError, match="out of bounds"):
        repository.rebuild_patches(conn, book_id, [(0, 5)])


def test_build_patch_text_skips_excluded(conn):
    book_id = _seed_book(conn, 5)
    repository.set_chapter_excluded(conn, book_id, 2, True)
    conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
        "VALUES (?, 0, 0, 4, 'pending', '2026-01-01', '2026-01-01')",
        (book_id,),
    )
    conn.commit()
    patch = repository.get_patch(conn, 1)
    text = repository.build_patch_text(conn, patch)
    assert "chapter 2" not in text
    assert "chapter 0" in text
    assert "chapter 4" in text


def test_build_patch_text_applies_replace_rules(conn):
    book_id = _seed_book(conn, 3)
    repository.create_replace_rule(conn, book_id, "content", "CONTENT", False, 0)
    conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
        "VALUES (?, 0, 0, 2, 'pending', '2026-01-01', '2026-01-01')",
        (book_id,),
    )
    conn.commit()
    patch = repository.get_patch(conn, 1)
    text = repository.build_patch_text(conn, patch)
    assert "CONTENT" in text
    assert "content" not in text
