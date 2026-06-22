"""Unit tests for repository.retry_all_failed_patches_for_book."""
from __future__ import annotations

from datetime import datetime, timezone

from app import db, repository


def _make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _insert_book(conn, book_id=1):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                              created_at, updated_at)
           VALUES (?, 't', 'f.epub', '/tmp/f.epub', 10, 'processing', ?, ?)""",
        (book_id, now, now),
    )
    conn.commit()


def _insert_patch(conn, *, book_id, status, error_message=None, audio_path=None):
    now = datetime.now(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT COALESCE(MAX(patch_index), -1) + 1 AS n FROM patch WHERE book_id = ?",
        (book_id,),
    ).fetchone()
    patch_index = row["n"]
    cur = conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                               audio_path, error_message, attempt_count, created_at, updated_at)
           VALUES (?, ?, 0, 0, ?, ?, ?, 0, ?, ?)""",
        (book_id, patch_index, status, audio_path, error_message, now, now),
    )
    conn.commit()
    return cur.lastrowid


def test_retry_resets_failed_patches_to_pending():
    conn = _make_conn()
    _insert_book(conn)
    p1 = _insert_patch(conn, book_id=1, status="failed", error_message="oops")
    p2 = _insert_patch(conn, book_id=1, status="failed", error_message="oops 2")
    n = repository.retry_all_failed_patches_for_book(conn, 1)
    assert n == 2
    for pid in (p1, p2):
        row = conn.execute("SELECT * FROM patch WHERE id = ?", (pid,)).fetchone()
        assert row["status"] == "pending"
        assert row["error_message"] is None
        assert row["audio_path"] is None


def test_retry_skips_processing_patches():
    conn = _make_conn()
    _insert_book(conn)
    p1 = _insert_patch(conn, book_id=1, status="failed", error_message="x")
    p2 = _insert_patch(conn, book_id=1, status="processing", error_message="y")
    n = repository.retry_all_failed_patches_for_book(conn, 1)
    assert n == 1
    p1_row = conn.execute("SELECT * FROM patch WHERE id = ?", (p1,)).fetchone()
    p2_row = conn.execute("SELECT * FROM patch WHERE id = ?", (p2,)).fetchone()
    assert p1_row["status"] == "pending"
    assert p2_row["status"] == "processing"
    assert p2_row["error_message"] == "y"


def test_retry_is_noop_when_no_failed_patches():
    conn = _make_conn()
    _insert_book(conn)
    _insert_patch(conn, book_id=1, status="done", audio_path="/tmp/a.wav")
    n = repository.retry_all_failed_patches_for_book(conn, 1)
    assert n == 0
    book = conn.execute("SELECT * FROM book WHERE id = 1").fetchone()
    assert book["status"] == "processing"  # unchanged


def test_retry_does_not_touch_other_books():
    conn = _make_conn()
    _insert_book(conn, book_id=1)
    _insert_book(conn, book_id=2)
    p1 = _insert_patch(conn, book_id=1, status="failed", error_message="a")
    p2 = _insert_patch(conn, book_id=2, status="failed", error_message="b")
    n = repository.retry_all_failed_patches_for_book(conn, 1)
    assert n == 1
    p1_row = conn.execute("SELECT * FROM patch WHERE id = ?", (p1,)).fetchone()
    p2_row = conn.execute("SELECT * FROM patch WHERE id = ?", (p2,)).fetchone()
    assert p1_row["status"] == "pending"
    assert p2_row["status"] == "failed"
