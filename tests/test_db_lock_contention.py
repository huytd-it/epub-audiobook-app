"""Regression tests for the app-wide freeze: the shared db_lock must never be held
across network or ffmpeg work.

One sqlite3 connection is shared by every route handler and both workers, guarded by a
single threading.Lock (app.state.db_lock). Anything that keeps that lock while doing slow
I/O stalls every other request in the process, which is what made the UI appear frozen.
Each test here pins one such path and asserts the lock is free while the slow call runs.
"""
from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import db, repository


def _make_conn(tmp_path):
    conn = db.connect(str(tmp_path / "app.db"))
    db.init_schema(conn)
    return conn


def _insert_book(conn, *, book_id=1, final_audio_path="/tmp/final.wav"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                              final_audio_path, created_at, updated_at)
           VALUES (?, 'Book', 'f.epub', '/tmp/f.epub', 10, 'ready', ?, ?, ?)""",
        (book_id, final_audio_path, now, now),
    )
    conn.commit()


def test_queue_handlers_never_touch_the_shared_db_lock(tmp_path, monkeypatch):
    from pathlib import Path
    matches = []
    for path in Path("app/jobqueue").rglob("*.py"):
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "db_lock" in line:
                matches.append(f"{path}:{line_number}: {line}")
    assert not matches, "jobqueue references db_lock:\n" + "\n".join(matches)


class _LockProbe:
    """Records whether db_lock was already held at the moment a slow call started."""

    def __init__(self, lock: threading.Lock):
        self.lock = lock
        self.held_during_call = None

    def observe(self) -> None:
        # acquire(blocking=False) fails only when someone else already holds it.
        acquired = self.lock.acquire(blocking=False)
        self.held_during_call = not acquired
        if acquired:
            self.lock.release()


def test_worker_auto_upload_does_not_hold_db_lock_over_the_network(tmp_path, monkeypatch):
    """worker._process_book_job used to call youtube.upload_video() inside `with
    self.db_lock`. That is a resumable chunked upload of the whole MP4, so the lock stayed
    held for the entire transfer and every HTTP request in the process blocked behind it."""
    from app import worker as worker_module
    from app.config import settings

    conn = _make_conn(tmp_path)
    _insert_book(conn)
    job = repository.enqueue_book_job(conn, 1, "video")
    repository.claim_next_pending_book_job(conn)

    lock = threading.Lock()
    w = worker_module.PatchWorker(conn, engine=None, data_root=str(tmp_path), db_lock=lock)

    out_path = tmp_path / "video.mp4"
    out_path.write_bytes(b"fake mp4")
    monkeypatch.setattr(w, "_run_video_job", lambda j: str(out_path))
    monkeypatch.setattr(settings, "youtube_auto_upload", True)

    import app.youtube as youtube_module
    monkeypatch.setattr(youtube_module, "is_configured", lambda: True)

    probe = _LockProbe(lock)

    def _fake_upload(*args, **kwargs):
        probe.observe()
        return {"youtube_video_id": "vid123", "status": "done"}

    # Whichever path the worker takes to reach the network, it must not be under the lock.
    monkeypatch.setattr(youtube_module, "upload_video", _fake_upload)
    monkeypatch.setattr(youtube_module, "process_upload", _fake_upload)

    asyncio.run(w._process_book_job(job))

    if probe.held_during_call is not None:
        assert probe.held_during_call is False, (
            "db_lock was held while the YouTube upload ran - this freezes the whole app"
        )

    # The upload must still be queued for the UploadWorker to drain.
    rows = conn.execute("SELECT * FROM youtube_uploads").fetchall()
    assert len(rows) == 1, f"expected exactly one queued upload row, got {len(rows)}"
    assert rows[0]["status"] == "pending"




def test_patch_publish_stage_runs_off_the_shared_connection(tmp_path, monkeypatch):
    """patch_publishing.run_patch_publish_stage renders video with ffmpeg and calls the
    YouTube API. Routes invoked it on the shared connection while holding db_lock."""
    from app.routes import patches as patches_routes

    conn = _make_conn(tmp_path)
    lock = threading.Lock()

    class _Request:
        class app:  # noqa: N801 - mimics starlette's request.app.state
            class state:
                pass

    _Request.app.state.conn = conn
    _Request.app.state.db_lock = lock

    probe = _LockProbe(lock)
    seen = {}

    def _fake_stage(publish_conn, patch_id):
        probe.observe()
        seen["conn"] = publish_conn
        return {"stage": "upload"}

    monkeypatch.setattr(patches_routes, "run_patch_publish_stage", _fake_stage)

    result = patches_routes._run_publish_stage(_Request, 7)

    assert result == {"stage": "upload"}
    assert probe.held_during_call is False, (
        "db_lock was held while the publish stage did ffmpeg/network work"
    )
    assert seen["conn"] is not conn, (
        "publish stage ran on the shared connection; it must use its own"
    )
