"""Unit tests for chunk-manager repository functions: per-patch max_chars override,
resume-from-chunk, and the on-demand chunk status view (see repository.py)."""
from __future__ import annotations

import math
from datetime import datetime, timezone

from app import db, repository
from app.chunker import split_into_tts_chunks

_NOW = datetime.now(timezone.utc).isoformat()
_CHAPTER_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "Sphinx of black quartz, judge my vow. "
    "Pack my box with five dozen liquor jugs. "
    "How vexingly quick daft zebras jump."
)


def _make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _insert_book(conn, book_id=1):
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                              created_at, updated_at)
           VALUES (?, 't', 'f.epub', '/tmp/f.epub', 10, 'ready', ?, ?)""",
        (book_id, _NOW, _NOW),
    )
    conn.execute(
        """INSERT INTO chapter (book_id, chapter_index, title, text, char_count)
           VALUES (?, 0, 'Ch0', ?, ?)""",
        (book_id, _CHAPTER_TEXT, len(_CHAPTER_TEXT)),
    )
    conn.commit()


def _insert_patch(
    conn, *, book_id=1, status="pending", next_chunk_index=0,
    chunk_count=0, max_chars=None, error_message=None,
):
    cur = conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                               next_chunk_index, chunk_count, max_chars, error_message,
                               created_at, updated_at)
           VALUES (?, 0, 0, 0, ?, ?, ?, ?, ?, ?, ?)""",
        (book_id, status, next_chunk_index, chunk_count, max_chars, error_message, _NOW, _NOW),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# set_patch_max_chars
# ---------------------------------------------------------------------------


def test_set_max_chars_allowed_when_pending():
    conn = _make_conn()
    _insert_book(conn)
    pid = _insert_patch(conn, status="pending")
    assert repository.set_patch_max_chars(conn, pid, 20) is True
    patch = repository.get_patch(conn, pid)
    assert patch.max_chars == 20
    assert patch.chunk_count == max(1, math.ceil(len(_CHAPTER_TEXT) / 20))


def test_set_max_chars_none_clears_override():
    conn = _make_conn()
    _insert_book(conn)
    pid = _insert_patch(conn, status="pending", max_chars=20)
    assert repository.set_patch_max_chars(conn, pid, None) is True
    patch = repository.get_patch(conn, pid)
    assert patch.max_chars is None


def test_set_max_chars_rejected_when_not_pending():
    conn = _make_conn()
    _insert_book(conn)
    pid = _insert_patch(conn, status="done")
    assert repository.set_patch_max_chars(conn, pid, 20) is False
    patch = repository.get_patch(conn, pid)
    assert patch.max_chars is None


# ---------------------------------------------------------------------------
# resume_patch_from_chunk
# ---------------------------------------------------------------------------


def test_resume_from_chunk_allowed_when_failed():
    conn = _make_conn()
    _insert_book(conn)
    pid = _insert_patch(conn, status="failed", next_chunk_index=3, chunk_count=5, error_message="boom")
    assert repository.resume_patch_from_chunk(conn, pid, 1) is True
    patch = repository.get_patch(conn, pid)
    assert patch.status == "pending"
    assert patch.next_chunk_index == 1
    assert patch.error_message is None


def test_resume_from_chunk_clamps_to_next_chunk_index():
    conn = _make_conn()
    _insert_book(conn)
    pid = _insert_patch(conn, status="failed", next_chunk_index=2, chunk_count=5)
    # Asking to resume from a chunk beyond what was ever synthesized clamps down.
    repository.resume_patch_from_chunk(conn, pid, 10)
    patch = repository.get_patch(conn, pid)
    assert patch.next_chunk_index == 2


def test_resume_from_chunk_rejected_when_not_failed():
    conn = _make_conn()
    _insert_book(conn)
    pid = _insert_patch(conn, status="pending")
    assert repository.resume_patch_from_chunk(conn, pid, 0) is False


# ---------------------------------------------------------------------------
# get_patch_chunk_view
# ---------------------------------------------------------------------------


def test_chunk_view_all_done_when_patch_done():
    conn = _make_conn()
    _insert_book(conn)
    pid = _insert_patch(conn, status="done", max_chars=20)
    patch = repository.get_patch(conn, pid)
    view = repository.get_patch_chunk_view(conn, patch)
    assert len(view) > 1
    assert all(c["status"] == "done" for c in view)


def test_chunk_view_failed_patch_marks_boundary():
    conn = _make_conn()
    _insert_book(conn)
    chunks = split_into_tts_chunks(_CHAPTER_TEXT, max_chars=20)
    assert len(chunks) >= 3
    pid = _insert_patch(conn, status="failed", max_chars=20, next_chunk_index=1, error_message="boom")
    patch = repository.get_patch(conn, pid)
    view = repository.get_patch_chunk_view(conn, patch)
    assert view[0]["status"] == "done"
    assert view[1]["status"] == "failed"
    assert view[2]["status"] == "pending"


def test_chunk_view_pending_patch_all_pending():
    conn = _make_conn()
    _insert_book(conn)
    pid = _insert_patch(conn, status="pending", max_chars=20)
    patch = repository.get_patch(conn, pid)
    view = repository.get_patch_chunk_view(conn, patch)
    assert all(c["status"] == "pending" for c in view)


def test_chunk_view_processing_uses_live_worker_index():
    conn = _make_conn()
    _insert_book(conn)
    pid = _insert_patch(conn, status="processing", max_chars=20, next_chunk_index=1)

    class FakeWorker:
        current_patch_id = pid
        current_chunk_index = 1

    patch = repository.get_patch(conn, pid)
    view = repository.get_patch_chunk_view(conn, patch, FakeWorker())
    assert view[0]["status"] == "done"
    assert view[1]["status"] == "processing"
