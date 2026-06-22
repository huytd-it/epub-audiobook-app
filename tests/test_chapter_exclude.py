"""Unit tests for chapter exclude functionality."""
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


def _seed_book(conn, chapter_count: int = 5):
    """Insert a minimal book + chapters for testing."""
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
            (book_id, i, f"Chapter {i}", f"text of chapter {i} " * 10, len(f"text of chapter {i} " * 10)),
        )
    conn.commit()
    return book_id


def test_set_chapter_excluded(conn):
    book_id = _seed_book(conn, 3)
    ok = repository.set_chapter_excluded(conn, book_id, 0, True)
    assert ok is True
    row = conn.execute(
        "SELECT is_excluded FROM chapter WHERE book_id = ? AND chapter_index = 0", (book_id,)
    ).fetchone()
    assert row["is_excluded"] == 1


def test_set_chapter_excluded_returns_false_for_unknown_chapter(conn):
    book_id = _seed_book(conn, 2)
    ok = repository.set_chapter_excluded(conn, book_id, 99, True)
    assert ok is False


def test_list_included_chapters_skips_excluded(conn):
    book_id = _seed_book(conn, 5)
    repository.set_chapter_excluded(conn, book_id, 1, True)
    repository.set_chapter_excluded(conn, book_id, 3, True)
    included = repository.list_included_chapters(conn, book_id)
    indices = [ch.chapter_index for ch in included]
    assert indices == [0, 2, 4]


def test_default_is_excluded_is_zero(conn):
    book_id = _seed_book(conn, 2)
    row = conn.execute(
        "SELECT is_excluded FROM chapter WHERE book_id = ? AND chapter_index = 0", (book_id,)
    ).fetchone()
    assert row["is_excluded"] == 0
