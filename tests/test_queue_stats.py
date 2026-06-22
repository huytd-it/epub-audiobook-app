"""Unit tests for repository.get_queue_stats.

Uses an in-memory DB so the test is fast and self-contained. Inserts known
rows into patch + book_job, then asserts the returned counts and the
last_errors ordering.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db, repository


def _utc(ts: str) -> str:
    # helper: parse an ISO timestamp and normalize to UTC isoformat with offset
    return ts


def _make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _insert_patch(conn, *, book_id, status, error_message=None, updated_at=None, audio_path=None,
                  patch_index=None):
    now = updated_at or datetime.now(timezone.utc).isoformat()
    # patch_index is unique per book. If the caller doesn't supply one, find the next free value.
    if patch_index is None:
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


def _insert_book_job(conn, *, book_id, status, error_message=None, updated_at=None, output_path=None):
    now = updated_at or datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO book_job (book_id, job_type, status, attempt_count,
                                  error_message, output_path, created_at, updated_at)
           VALUES (?, 'video', ?, 0, ?, ?, ?, ?)""",
        (book_id, status, error_message, output_path, now, now),
    )
    conn.commit()
    return cur.lastrowid


def _insert_book(conn, book_id=1, **fields):
    now = datetime.now(timezone.utc).isoformat()
    cols = [
        "title", "original_filename", "epub_path", "patch_size", "status",
        "final_audio_path", "final_video_path", "background_image_path",
        "voice_clip_path", "voice_transcript", "created_at", "updated_at",
    ]
    defaults = {
        "title": "t", "original_filename": "f.epub", "epub_path": "/tmp/f.epub",
        "patch_size": 10, "status": "ready", "final_audio_path": None,
        "final_video_path": None, "background_image_path": None,
        "voice_clip_path": None, "voice_transcript": None,
        "created_at": now, "updated_at": now,
    }
    defaults.update(fields)
    conn.execute(
        f"INSERT INTO book ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        tuple(defaults[c] for c in cols),
    )
    conn.commit()


def test_queue_stats_empty_database():
    conn = _make_conn()
    stats = repository.get_queue_stats(conn)
    assert stats["patch"] == {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    assert stats["book_job"] == {"pending": 0, "processing": 0, "done": 0, "failed": 0}
    assert stats["oldest_pending_patch_age_seconds"] == 0.0
    assert stats["last_errors"] == []


def test_queue_stats_mixed_state():
    conn = _make_conn()
    _insert_book(conn)
    old_pending = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    recent = datetime.now(timezone.utc).isoformat()
    # 3 pending, 1 processing, 50 done, 3 failed (only 2 with error_message).
    # patch.failed counts ALL failed patches; last_errors only includes the ones
    # with a non-NULL error_message.
    for _ in range(3):
        _insert_patch(conn, book_id=1, status="pending", updated_at=old_pending)
    _insert_patch(conn, book_id=1, status="processing", updated_at=recent)
    for _ in range(50):
        _insert_patch(conn, book_id=1, status="done", audio_path="/tmp/a.wav")
    _insert_patch(conn, book_id=1, status="failed", error_message="oom 1", updated_at=recent)
    _insert_patch(conn, book_id=1, status="failed", error_message="oom 2",
                  updated_at=(datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat())
    _insert_patch(conn, book_id=1, status="failed")  # no error_message, excluded from last_errors
    _insert_book_job(conn, book_id=1, status="pending")

    stats = repository.get_queue_stats(conn)
    assert stats["patch"]["pending"] == 3
    assert stats["patch"]["processing"] == 1
    assert stats["patch"]["done"] == 50
    assert stats["patch"]["failed"] == 3  # all failed, including the one without a message
    assert stats["book_job"]["pending"] == 1
    assert stats["oldest_pending_patch_age_seconds"] >= 110.0
    assert len(stats["last_errors"]) == 2  # only the ones with error_message
    # most recent first
    assert stats["last_errors"][0]["error_message"] == "oom 1"


def test_queue_stats_last_errors_capped_at_5():
    conn = _make_conn()
    _insert_book(conn)
    for i in range(20):
        _insert_patch(
            conn,
            book_id=1,
            status="failed",
            error_message=f"err {i}",
            updated_at=(datetime.now(timezone.utc) - timedelta(seconds=i)).isoformat(),
        )
    stats = repository.get_queue_stats(conn)
    assert len(stats["last_errors"]) == 5
    assert stats["last_errors"][0]["error_message"] == "err 0"


def test_queue_stats_mixes_patch_and_book_job_errors():
    conn = _make_conn()
    _insert_book(conn)
    _insert_patch(conn, book_id=1, status="failed", error_message="patch boom")
    _insert_book_job(conn, book_id=1, status="failed", error_message="video boom")
    stats = repository.get_queue_stats(conn)
    entities = {e["entity"] for e in stats["last_errors"]}
    assert entities == {"patch", "book_job"}
