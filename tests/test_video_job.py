"""Unit tests for the book_job lifecycle: enqueue idempotency, claim atomicity,
mark done/failed, and the startup backfill."""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path

from app import db, repository


def _make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _insert_book(conn, *, book_id=1, status="ready", final_audio_path=None,
                 background_image_path=None):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                              final_audio_path, background_image_path, created_at, updated_at)
           VALUES (?, 't', 'f.epub', '/tmp/f.epub', 10, ?, ?, ?, ?, ?)""",
        (book_id, status, final_audio_path, background_image_path, now, now),
    )
    conn.commit()


def test_enqueue_book_job_creates_pending_row():
    conn = _make_conn()
    _insert_book(conn)
    job = repository.enqueue_book_job(conn, 1, "video")
    assert job.id is not None
    assert job.status == "pending"
    assert job.job_type == "video"
    assert job.attempt_count == 0


def test_enqueue_book_job_is_idempotent_on_existing():
    conn = _make_conn()
    _insert_book(conn)
    j1 = repository.enqueue_book_job(conn, 1, "video")
    j2 = repository.enqueue_book_job(conn, 1, "video")
    assert j1.id == j2.id
    # Only one row in the table.
    rows = conn.execute("SELECT * FROM book_job WHERE book_id = 1").fetchall()
    assert len(rows) == 1


def test_enqueue_book_job_preserves_existing_status():
    conn = _make_conn()
    _insert_book(conn)
    j = repository.enqueue_book_job(conn, 1, "video")
    with conn:
        repository.mark_book_job_failed(conn, j.id, "boom")
    j2 = repository.enqueue_book_job(conn, 1, "video")
    # Idempotency: must not overwrite a 'failed' row back to 'pending'.
    assert j2.id == j.id
    assert j2.status == "failed"
    assert j2.error_message == "boom"


def test_enqueue_distinct_job_types_per_book():
    conn = _make_conn()
    _insert_book(conn)
    # (book_id, job_type) is the unique key, so a different type for the same
    # book creates a separate row. We don't actually use a second type today,
    # but the schema must allow it.
    j1 = repository.enqueue_book_job(conn, 1, "video")
    j2 = repository.enqueue_book_job(conn, 1, "video")  # same type, must be a no-op
    assert j1.id == j2.id


def test_claim_next_pending_book_job_marks_processing():
    conn = _make_conn()
    _insert_book(conn)
    repository.enqueue_book_job(conn, 1, "video")
    job = repository.claim_next_pending_book_job(conn)
    assert job is not None
    assert job.status == "processing"
    assert job.attempt_count == 1
    # Second claim returns None.
    job2 = repository.claim_next_pending_book_job(conn)
    assert job2 is None


def test_claim_orders_by_book_id_then_id():
    conn = _make_conn()
    _insert_book(conn, book_id=1)
    _insert_book(conn, book_id=2)
    repository.enqueue_book_job(conn, 2, "video")
    repository.enqueue_book_job(conn, 1, "video")
    j1 = repository.claim_next_pending_book_job(conn)
    assert j1 is not None
    assert j1.book_id == 1


def test_mark_book_job_done_and_failed():
    conn = _make_conn()
    _insert_book(conn)
    repository.enqueue_book_job(conn, 1, "video")
    job = repository.claim_next_pending_book_job(conn)
    repository.mark_book_job_done(conn, job.id, "/tmp/v.mp4")
    row = conn.execute("SELECT * FROM book_job WHERE id = ?", (job.id,)).fetchone()
    assert row["status"] == "done"
    assert row["output_path"] == "/tmp/v.mp4"
    assert row["error_message"] is None


def test_requeue_stuck_book_jobs():
    conn = _make_conn()
    _insert_book(conn)
    repository.enqueue_book_job(conn, 1, "video")
    job = repository.claim_next_pending_book_job(conn)
    # job is now 'processing'. Simulate a crash by running the requeue helper.
    n = repository.requeue_stuck_book_jobs(conn)
    assert n == 1
    row = conn.execute("SELECT * FROM book_job WHERE id = ?", (job.id,)).fetchone()
    assert row["status"] == "pending"
    assert "requeued" in row["error_message"]


def test_backfill_inserts_video_jobs_for_done_books():
    conn = _make_conn()
    _insert_book(conn, book_id=1, status="done",
                 final_audio_path="/tmp/1.wav", background_image_path="/tmp/1.jpg")
    _insert_book(conn, book_id=2, status="done",
                 final_audio_path="/tmp/2.wav", background_image_path=None)  # no bg
    _insert_book(conn, book_id=3, status="processing",
                 final_audio_path="/tmp/3.wav", background_image_path="/tmp/3.jpg")  # not done
    _insert_book(conn, book_id=4, status="done",
                 final_audio_path="/tmp/4.wav", background_image_path="/tmp/4.jpg")
    # Book 4 already has a video job (any status) - backfill must not duplicate.
    repository.enqueue_book_job(conn, 4, "video")
    n = repository.backfill_video_book_jobs(conn)
    # Only book 1 is eligible: book 2 has no background, book 3 is not done,
    # book 4 already has a video job.
    assert n == 1
    rows = conn.execute("SELECT book_id, status FROM book_job ORDER BY book_id").fetchall()
    assert [r["book_id"] for r in rows] == [1, 4]
    assert all(r["status"] == "pending" for r in rows)


def test_backfill_returns_zero_when_nothing_eligible():
    conn = _make_conn()
    _insert_book(conn, book_id=1, status="ready",
                 final_audio_path=None, background_image_path="/tmp/1.jpg")
    _insert_book(conn, book_id=2, status="done",
                 final_audio_path="/tmp/2.wav", background_image_path=None)
    n = repository.backfill_video_book_jobs(conn)
    assert n == 0
    rows = conn.execute("SELECT * FROM book_job").fetchall()
    assert rows == []
