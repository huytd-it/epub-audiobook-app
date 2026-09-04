"""Backfill pending legacy rows into the job queue idempotently."""
from __future__ import annotations

from datetime import datetime, timezone

from app import db, repository
from app.jobqueue import store
from app.jobqueue.backfill import (
    backfill_pending_jobs,
    build_queue,
    enqueue_pending_patch_jobs,
)


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size,
                              status, created_at, updated_at)
           VALUES (1, 'Book', 'a.epub', '/tmp/a.epub', 10, 'ready', ?, ?)""", (now, now))
    conn.commit()
    return conn


def _patch(conn, status="pending", index=0):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                               attempt_count, created_at, updated_at)
           VALUES (1, ?, 0, 0, ?, 0, ?, ?)""", (index, status, now, now))
    conn.commit()
    return cur.lastrowid


def test_pending_patches_become_voxcpm_jobs(tmp_path):
    conn = _conn(tmp_path)
    patch_id = _patch(conn)
    _patch(conn, status="done", index=1)
    assert enqueue_pending_patch_jobs(conn) == 1
    job = store.list_jobs(conn, job_type="audiobook_tts")[0]
    assert job.payload["patch_id"] == patch_id
    assert job.payload["tts_engine"] == "voxcpm2"
    assert job.dedupe_key == f"audiobook_tts:patch={patch_id}"
    assert job.book_id == 1


def test_backfill_never_queues_tts_on_its_own(tmp_path):
    """Startup calls backfill; queueing TTS must stay an explicit operator action, or a
    restart would refill a queue that was just cleared."""
    conn = _conn(tmp_path)
    _patch(conn)
    counts = backfill_pending_jobs(conn)
    assert "audiobook_tts" not in counts
    assert store.list_jobs(conn, job_type="audiobook_tts") == []


def test_pending_patch_jobs_can_be_scoped_to_one_book(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size,
                              status, created_at, updated_at)
           VALUES (2, 'Other', 'b.epub', '/tmp/b.epub', 10, 'ready', ?, ?)""", (now, now))
    mine = _patch(conn)
    conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                               attempt_count, created_at, updated_at)
           VALUES (2, 0, 0, 0, 'pending', 0, ?, ?)""", (now, now))
    conn.commit()
    assert enqueue_pending_patch_jobs(conn, book_id=1) == 1
    jobs = store.list_jobs(conn, job_type="audiobook_tts")
    assert [j.payload["patch_id"] for j in jobs] == [mine]


def test_pending_patch_jobs_snapshot_selected_tts_engine(tmp_path):
    conn = _conn(tmp_path)
    _patch(conn)
    assert enqueue_pending_patch_jobs(conn, tts_engine="vieneu-fast") == 1
    assert store.list_jobs(conn, job_type="audiobook_tts")[0].payload["tts_engine"] == "vieneu-fast"


def test_pending_book_jobs_become_video_jobs(tmp_path):
    conn = _conn(tmp_path)
    book_job = repository.enqueue_book_job(conn, 1, "video")
    counts = backfill_pending_jobs(conn)
    assert counts["video"] == 1
    assert store.list_jobs(conn, job_type="video")[0].payload["book_job_id"] == book_job.id


def test_pending_uploads_become_youtube_jobs(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO youtube_uploads (video_path, title, description, tags,
                                         privacy_status, status, created_at)
           VALUES ('/tmp/v.mp4', 'T', 'D', '', 'private', 'pending', ?)""", (now,))
    conn.commit()
    counts = backfill_pending_jobs(conn)
    assert counts["youtube_upload"] == 1
    assert store.list_jobs(conn, job_type="youtube_upload")[0].payload["upload_id"] == cur.lastrowid


def test_cancelled_upload_is_not_backfilled_after_its_job_is_cleared(tmp_path):
    conn = _conn(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    upload_id = conn.execute(
        """INSERT INTO youtube_uploads (video_path, title, description, tags,
              privacy_status, status, created_at)
           VALUES ('/tmp/v.mp4', 'T', 'D', '', 'private', 'pending', ?)""",
        (now,),
    ).lastrowid
    conn.commit()
    backfill_pending_jobs(conn)
    job_id = store.list_jobs(conn, job_type="youtube_upload")[0].id
    store.claim(conn, "youtube_upload", "worker")
    store.fail(conn, job_id, "failed", max_attempts=1)
    store.delete_terminal(conn, [job_id])

    assert backfill_pending_jobs(conn)["youtube_upload"] == 0
    assert store.list_jobs(conn, job_type="youtube_upload") == []


def test_interrupted_validation_returns_to_pending_without_resetting_count(tmp_path):
    conn = _conn(tmp_path); now = datetime.now(timezone.utc).isoformat()
    upload_id = conn.execute("INSERT INTO youtube_uploads (video_path,status,validation_status,integrity_retry_count,created_at) VALUES ('v','pending','validating',1,?)", (now,)).lastrowid; conn.commit()
    backfill_pending_jobs(conn)
    row = conn.execute("SELECT validation_status,integrity_retry_count FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert tuple(row) == ("pending", 1)


def test_waiting_patch_rerender_is_restored_by_generation(tmp_path):
    conn = _conn(tmp_path); now = datetime.now(timezone.utc).isoformat()
    upload_id = conn.execute("INSERT INTO youtube_uploads (video_path,status,validation_status,integrity_retry_count,render_source_type,render_source_id,created_at) VALUES ('v','waiting_for_rerender','waiting_for_rerender',2,'patch',9,?)", (now,)).lastrowid; conn.commit()
    backfill_pending_jobs(conn); backfill_pending_jobs(conn)
    jobs = store.list_jobs(conn, job_type="patch_video")
    assert len(jobs) == 1
    assert jobs[0].payload == {"patch_id": 9, "recovery_upload_id": upload_id}
    assert jobs[0].dedupe_key.endswith("integrity_retry=2")


def test_running_it_twice_creates_nothing_new(tmp_path):
    conn = _conn(tmp_path)
    _patch(conn)
    repository.enqueue_book_job(conn, 1, "video")
    first = backfill_pending_jobs(conn)
    second = backfill_pending_jobs(conn)
    assert second == {"video": 0, "youtube_upload": 0}
    assert len(store.list_jobs(conn)) == sum(first.values())
    assert enqueue_pending_patch_jobs(conn) == 1
    assert enqueue_pending_patch_jobs(conn) == 0


def test_finished_job_does_not_block_new_backfill(tmp_path):
    conn = _conn(tmp_path)
    _patch(conn)
    enqueue_pending_patch_jobs(conn)
    job = store.list_jobs(conn, job_type="audiobook_tts")[0]
    store.finish(conn, job.id, None)
    assert enqueue_pending_patch_jobs(conn) == 1


def test_build_queue_registers_all_four_handlers(tmp_path):
    conn = _conn(tmp_path)
    queue = build_queue(lambda: db.connect(str(tmp_path / "a.db")))
    assert queue.capacity("audiobook_tts") == 1
    assert queue.capacity("video") == 2
    assert queue.capacity("youtube_upload") == 1
    assert queue.capacity("light_tts") == 10
    assert {p["job_type"] for p in queue.pool_status()} == {
        "audiobook_tts", "video", "patch_video", "standalone_video",
        "youtube_upload", "light_tts", "background_gen", "gameplay_clip",
    }
