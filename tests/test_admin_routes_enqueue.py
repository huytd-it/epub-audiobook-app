"""Admin actions enqueue jobs while preserving their legacy responses."""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, repository
from app.jobqueue import store


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    from app.main import app
    conn = db.connect(str(tmp_path / "app.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,final_audio_path,background_image_path,created_at,updated_at) VALUES (1,'Sách','a.epub','/tmp/a.epub',10,'ready','/tmp/f.wav','/tmp/bg.jpg',?,?)", (now, now))
    conn.commit()
    app.state.conn, app.state.db_lock = conn, threading.Lock()
    app.state.worker = app.state.job_queue = None
    with TestClient(app) as c:
        app.state.conn = conn
        app.state.db_lock = threading.Lock()
        yield c, conn


def _patch(conn, status):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute("INSERT INTO patch (book_id,patch_index,chapter_start,chapter_end,status,error_message,attempt_count,created_at,updated_at) VALUES (1,0,0,0,?,'cũ',0,?,?)", (status, now, now))
    conn.commit()
    return cur.lastrowid


def test_admin_actions_enqueue_jobs(client):
    c, conn = client
    patch_id = _patch(conn, "processing")
    assert c.post("/queue/requeue-stuck").json()["requeued"] == 1
    assert store.list_jobs(conn, job_type="audiobook_tts")[0].payload["patch_id"] == patch_id
    conn.execute("UPDATE patch SET status='failed' WHERE id=?", (patch_id,))
    conn.commit()
    c.post("/books/1/patches/retry-failed", follow_redirects=False)
    c.post("/books/1/video/regenerate", follow_redirects=False)
    assert store.list_jobs(conn, job_type="video")


def test_auto_build_does_not_enqueue_tts(client):
    """Building patches must not implicitly start an optional local TTS engine."""
    c, conn = client
    for i in range(4):
        conn.execute(
            "INSERT INTO chapter (book_id,chapter_index,title,text,char_count) VALUES (1,?,?,?,?)",
            (i, f"Ch{i}", "nội dung " * 20, 160),
        )
    conn.commit()
    res = c.post(
        "/books/1/patches/auto-build",
        data={"start_chapter": "0", "patch_size": "2"},
        follow_redirects=False,
    )
    assert res.status_code == 303
    jobs = store.list_jobs(conn, job_type="audiobook_tts")
    assert jobs == []


def test_regenerate_replaces_stale_queue_job(client):
    c, conn = client
    c.post("/books/1/video/regenerate", follow_redirects=False)
    first = store.list_jobs(conn, job_type="video")[0]
    c.post("/books/1/video/regenerate", follow_redirects=False)
    assert store.get(conn, first.id).status == "cancelled"


def test_reset_all_clears_and_backfills_jobs(client):
    c, conn = client
    _patch(conn, "failed")
    store.enqueue(conn, "video", dedupe_key="video:book_job=1")
    assert c.post("/queue/reset-all").json()["jobs_cleared"] >= 1
    assert all(j.status == "pending" for j in store.list_jobs(conn))
