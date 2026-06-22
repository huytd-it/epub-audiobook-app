"""Tests for repository.reset_all_jobs() and the /queue/reset-all route."""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app import db as app_db, repository
from app.main import app


def _make_conn():
    conn = app_db.connect(":memory:")
    app_db.init_schema(conn)
    return conn


def _insert_book(conn, book_id=1):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size,
                              status, final_audio_path, created_at, updated_at)
           VALUES (?, 't', 'f.epub', '/tmp/f.epub', 10, 'processing',
                   '/tmp/final.wav', ?, ?)""",
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


def _insert_book_job(conn, *, book_id, status, error_message=None, output_path=None):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO book_job (book_id, job_type, status, attempt_count,
                                  error_message, output_path, created_at, updated_at)
           VALUES (?, 'video', ?, 0, ?, ?, ?, ?)""",
        (book_id, status, error_message, output_path, now, now),
    )
    conn.commit()
    return cur.lastrowid


# ------------------------------------------------------------------ repository


def test_reset_all_jobs_resets_all_statuses():
    conn = _make_conn()
    _insert_book(conn, book_id=1)
    _insert_book(conn, book_id=2)
    _insert_patch(conn, book_id=1, status="pending")
    _insert_patch(conn, book_id=1, status="done", audio_path="/tmp/done.wav")
    _insert_patch(conn, book_id=1, status="failed", error_message="boom")
    _insert_book_job(conn, book_id=1, status="done", output_path="/tmp/video.mp4")
    _insert_book_job(conn, book_id=2, status="failed", error_message="no gpu")

    summary = repository.reset_all_jobs(conn)
    assert summary["patches_reset"] == 3
    assert summary["book_jobs_reset"] == 2
    assert summary["books_reset"] == 2
    assert summary["files_deleted"] >= 2

    # All patches → pending, cleared.
    for row in conn.execute("SELECT * FROM patch"):
        assert row["status"] == "pending"
        assert row["audio_path"] is None
        assert row["error_message"] is None
    for row in conn.execute("SELECT * FROM book_job"):
        assert row["status"] == "pending"
        assert row["error_message"] is None
        assert row["output_path"] is None
    for row in conn.execute("SELECT * FROM book"):
        assert row["status"] == "ready"
        assert row["final_audio_path"] is None


def test_reset_all_jobs_skips_processing_rows():
    conn = _make_conn()
    _insert_book(conn)
    _insert_patch(conn, book_id=1, status="processing")

    summary = repository.reset_all_jobs(conn)
    assert summary["patches_reset"] == 0  # processing is excluded
    row = conn.execute("SELECT * FROM patch").fetchone()
    assert row["status"] == "processing"


def test_reset_all_jobs_is_noop_on_empty_db():
    conn = _make_conn()
    summary = repository.reset_all_jobs(conn)
    assert summary["patches_reset"] == 0
    assert summary["book_jobs_reset"] == 0
    assert summary["books_reset"] == 0


# ----------------------------------------------------------------- route


def test_reset_all_endpoint(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    settings_mod = __import__("app.config", fromlist=["settings"])
    monkeypatch.setattr(settings_mod.settings, "db_path", str(db_path))
    monkeypatch.setattr(settings_mod.settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings_mod.settings, "enable_worker", False)

    with TestClient(app) as c:
        resp = c.post("/queue/reset-all")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["patches_reset"] == 0
        assert payload["book_jobs_reset"] == 0


def test_reset_all_on_startup_flag(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    settings_mod = __import__("app.config", fromlist=["settings"])
    monkeypatch.setattr(settings_mod.settings, "db_path", str(db_path))
    monkeypatch.setattr(settings_mod.settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings_mod.settings, "enable_worker", False)
    monkeypatch.setattr(settings_mod.settings, "reset_all_jobs_on_startup", True)

    with TestClient(app) as c:
        # The lifespan should have run and produced no errors (empty DB is fine).
        worker = c.app.state.worker
        assert worker.state == "disabled"

        resp = c.get("/queue/stats")
        assert resp.status_code == 200
        # All counts should be zero — the reset nuked nothing because DB is empty,
        # but the startup didn't crash.
        stats = resp.json()
        for k in ("pending", "processing", "done", "failed"):
            assert stats["patch"][k] == 0
            assert stats["book_job"][k] == 0
