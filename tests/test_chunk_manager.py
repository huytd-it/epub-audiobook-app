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


def test_build_patch_chunk_plan_keeps_chapters_separate_and_marks_starts():
    conn = _make_conn()
    _insert_book(conn)
    conn.execute("UPDATE chapter SET text = 'Ch0 text.', char_count = 9 WHERE book_id = 1 AND chapter_index = 0")
    conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (1, 1, ?, ?, ?)",
        ("Ch1", "Ch1 This is chapter one.", len("Ch1 This is chapter one.")),
    )
    pid = _insert_patch(conn, max_chars=100)
    conn.execute("UPDATE patch SET chapter_end = 1 WHERE id = ?", (pid,))
    conn.commit()
    plan = repository.build_patch_chunk_plan(conn, repository.get_patch(conn, pid))
    assert all(set(item) == {"text", "chapter_index", "chapter_title", "is_chapter_start"} for item in plan)
    assert [item["chapter_index"] for item in plan] == [0, 1]
    assert [item["is_chapter_start"] for item in plan] == [True, True]


def test_build_patch_chunk_plan_skips_excluded_and_empty_after_replacement():
    conn = _make_conn()
    _insert_book(conn)
    conn.execute("UPDATE chapter SET is_excluded = 1 WHERE book_id = 1 AND chapter_index = 0")
    conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (1, 1, 'Ch1', 'remove me', 9)"
    )
    conn.execute(
        "INSERT INTO text_replace_rule (book_id, find, replace, is_regex, position) VALUES (1, 'remove me', '', 0, 0)"
    )
    pid = _insert_patch(conn)
    conn.execute("UPDATE patch SET chapter_end = 1 WHERE id = ?", (pid,))
    conn.commit()
    assert repository.build_patch_chunk_plan(conn, repository.get_patch(conn, pid)) == []


def test_build_patch_chunk_plan_marks_later_chunks_not_chapter_start():
    conn = _make_conn()
    _insert_book(conn)
    pid = _insert_patch(conn, max_chars=20)
    plan = repository.build_patch_chunk_plan(conn, repository.get_patch(conn, pid))
    assert len(plan) > 1
    assert [item["is_chapter_start"] for item in plan] == [True] + [False] * (len(plan) - 1)


def test_build_patch_chunk_plan_empty_title_is_safe():
    conn = _make_conn()
    _insert_book(conn)
    conn.execute("UPDATE chapter SET title = '' WHERE book_id = 1")
    conn.commit()
    pid = _insert_patch(conn)
    plan = repository.build_patch_chunk_plan(conn, repository.get_patch(conn, pid))
    assert plan and plan[0]["chapter_title"] == ""


# ---------------------------------------------------------------------------
# Cue ngắt nghỉ (docs/toi_uu_tts.md) đi vào chunk text mà TTS thực sự đọc
# ---------------------------------------------------------------------------


def _plan_text(conn, pid):
    plan = repository.build_patch_chunk_plan(conn, repository.get_patch(conn, pid))
    return " ".join(item["text"] for item in plan)


def test_chunk_plan_carries_break_cues():
    conn = _make_conn()
    _insert_book(conn)
    conn.execute(
        "UPDATE chapter SET text = ?, title = 'Ch0' WHERE book_id = 1",
        ("Ch0\n\nNgày mai chúng tôi sẽ họp bàn về kế hoạch sản xuất.",),
    )
    conn.commit()
    pid = _insert_patch(conn)
    assert "Ngày mai, chúng tôi" in _plan_text(conn, pid)


def test_chunk_plan_without_breaks_flag_is_unchanged():
    conn = _make_conn()
    _insert_book(conn)
    conn.execute(
        "UPDATE chapter SET text = ?, title = 'Ch0' WHERE book_id = 1",
        ("Ch0\n\nNgày mai chúng tôi sẽ họp bàn về kế hoạch sản xuất.",),
    )
    conn.execute("UPDATE book SET normalize_breaks_enabled = 0 WHERE id = 1")
    conn.commit()
    pid = _insert_patch(conn)
    assert "Ngày mai, chúng tôi" not in _plan_text(conn, pid)
    assert "Ngày mai chúng tôi" in _plan_text(conn, pid)


def test_chunk_plan_expands_abbreviations():
    conn = _make_conn()
    _insert_book(conn)
    conn.execute(
        "UPDATE chapter SET text = ?, title = 'Ch0' WHERE book_id = 1",
        ("Ch0\n\nUBND ra thông báo mới.",),
    )
    conn.commit()
    pid = _insert_patch(conn)
    assert "Ủy ban nhân dân" in _plan_text(conn, pid)
