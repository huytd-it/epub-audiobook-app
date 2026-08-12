"""API job and /health compatibility tests."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.jobqueue import store
from app.jobqueue.joblog import JobLogger


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_root", str(tmp_path))


class _FakeQueue:
    state = "idle"
    current_patch_id = None
    current_chunk_index = 0
    current_chunk_count = 0

    def __init__(self):
        self.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
        self.cancelled = []

    def pool_status(self):
        return [{"job_type": "video", "capacity": 2, "running": 1, "pending": 3}]

    def request_cancel(self, job_id):
        self.cancelled.append(job_id)


@pytest.fixture
def client(tmp_path):
    from app.main import app
    from app.config import settings
    settings.db_path = str(tmp_path / "app.db")
    settings.enable_worker = False
    conn = db.connect(str(tmp_path / "app.db"))
    db.init_schema(conn)
    app.state.conn = conn
    app.state.db_lock = threading.Lock()
    queue = _FakeQueue()
    app.state.worker = queue
    app.state.job_queue = queue
    with TestClient(app) as c:
        app.state.conn = conn
        app.state.db_lock = threading.Lock()
        app.state.worker = queue
        app.state.job_queue = queue
        yield c, conn, queue


def test_list_detail_log_cancel_retry_and_filters(client):
    c, conn, queue = client
    job_id = store.enqueue(conn, "video", payload={"book_job_id": 1}, book_id=7)
    assert c.get("/queue/jobs?type=video").json()["jobs"][0]["payload"] == {"book_job_id": 1}
    assert c.get(f"/queue/jobs/{job_id}").json()["book_id"] == 7
    logger = JobLogger(job_id, "video")
    logger.log("dòng thứ nhất")
    logger.close()
    assert "dòng thứ nhất" in c.get(f"/queue/jobs/{job_id}/log").text
    assert c.post(f"/queue/jobs/{job_id}/cancel").json()["status"] == "cancelled"
    assert queue.cancelled == [job_id]
    assert c.post(f"/queue/jobs/{job_id}/retry").json()["retried"] is True


def test_health_preserves_legacy_keys_and_adds_pools(client):
    body = client[0].get("/health").json()
    assert {"status", "worker_state", "current_patch_id", "current_chunk_index",
            "current_chunk_count", "queue_depth", "last_heartbeat_at"} <= body.keys()
    assert body["pools"][0]["job_type"] == "video"


def test_queue_page_and_stats_keep_shapes(client):
    c, conn, _ = client
    store.enqueue(conn, "video")
    assert c.get("/queue").status_code == 200
    body = c.get("/queue/stats").json()
    assert "patch" in body and "book_job" in body and "jobs" in body


def test_clear_queue_deletes_inactive_and_preserves_active(client):
    c, conn, _ = client
    for status in ("pending", "done", "failed", "cancelled", "running", "cancelling"):
        job_id = store.enqueue(conn, "video")
        conn.execute("UPDATE job SET status=? WHERE id=?", (status, job_id))
    conn.commit()

    response = c.post("/queue/clear")

    assert response.status_code == 200
    assert response.json() == {"cleared": 4}
    assert {job.status for job in store.list_jobs(conn)} == {"running", "cancelling"}


def test_retry_all_failed_jobs(client):
    c, conn, _ = client
    failed = store.enqueue(conn, "video")
    done = store.enqueue(conn, "youtube_upload")
    conn.execute("UPDATE job SET status='failed', error_message='boom' WHERE id=?", (failed,))
    conn.execute("UPDATE job SET status='done' WHERE id=?", (done,))
    conn.commit()

    assert c.post("/queue/jobs/retry-all-failed").json() == {"retried": 1}
    assert store.get(conn, failed).status == "pending"
    assert store.get(conn, done).status == "done"


def test_delete_removes_only_pending_job(client):
    c, conn, _ = client
    pending = store.enqueue(conn, "video")
    running = store.enqueue(conn, "video")
    conn.execute("UPDATE job SET status='running' WHERE id=?", (running,))
    conn.commit()
    assert c.delete(f"/queue/jobs/{pending}").json()["deleted"] is True
    assert store.get(conn, pending) is None
    assert c.delete(f"/queue/jobs/{running}").status_code == 409








def test_requeue_stuck_reports_how_many_tts_jobs_it_queued(client):
    c, conn, _ = client
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,created_at,updated_at)
           VALUES (1,'Sách','a.epub','/tmp/a.epub',10,'ready',?,?)""", (now, now))
    for index, status in enumerate(("processing", "pending", "done")):
        conn.execute(
            """INSERT INTO patch (book_id,patch_index,chapter_start,chapter_end,status,
                                   attempt_count,created_at,updated_at)
               VALUES (1,?,0,0,?,0,?,?)""", (index, status, now, now))
    conn.commit()

    body = c.post("/queue/requeue-stuck").json()

    assert body["requeued"] == 1  # only the stuck 'processing' patch was flipped
    assert body["queued"] == 2  # ... but both it and the already-pending one got queued
    assert len(store.list_jobs(conn, job_type="audiobook_tts")) == 2
