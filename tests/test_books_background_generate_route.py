"""Route tests: creating patches (rebuild/auto-build/extend) never enqueues a
background_gen job, and there is no book-level generate endpoint either."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import settings
from app.jobqueue import store
from app.main import app


def _seed_book(conn, chapter_count: int = 6, patch_size: int = 2) -> int:
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        """INSERT INTO book (title,original_filename,epub_path,patch_size,status,created_at,updated_at)
           VALUES ('Book','b.epub','b.epub',?,'ready',?,?)""",
        (patch_size, now, now),
    )
    book_id = cur.lastrowid
    for i in range(chapter_count):
        conn.execute(
            "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, ?, ?, ?, ?)",
            (book_id, i, f"Ch {i}", f"chapter content {i} " * 5, 50),
        )
    conn.commit()
    return book_id


def _background_jobs(conn):
    return store.list_jobs(conn, job_type="background_gen")


def test_rebuild_enqueues_no_background_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        response = client.post(f"/books/{book_id}/patches/rebuild", json={"ranges": [[0, 1], [3, 5]]})
        assert response.status_code == 200
        assert len(response.json()) == 2

        assert _background_jobs(conn) == []


def test_auto_build_enqueues_no_background_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        response = client.post(
            f"/books/{book_id}/patches/auto-build",
            data={"start_chapter": "0", "patch_size": "2"},
        )
        # The route 303-redirects; TestClient follows it to the book page.
        assert response.status_code == 200
        assert any(h.status_code == 303 for h in response.history)

        # 6 chapters / 2 = 3 patches were created, and still no image jobs.
        patch_count = conn.execute(
            "SELECT COUNT(*) AS n FROM patch WHERE book_id=?", (book_id,)
        ).fetchone()["n"]
        assert patch_count == 3
        assert _background_jobs(conn) == []


def test_extend_enqueues_no_background_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn, chapter_count=4, patch_size=2)

        first = client.post(f"/books/{book_id}/patches/rebuild", json={"ranges": [[0, 1]]})
        assert first.status_code == 200
        assert _background_jobs(conn) == []

        second = client.post(f"/books/{book_id}/patches/extend")
        assert second.status_code == 200
        assert second.json()["created"] == 1
        assert _background_jobs(conn) == []


def test_patch_builder_submit_enqueues_no_background_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        response = client.post(
            f"/books/{book_id}/patches/build",
            data={"range_start": ["0"], "range_end": ["2"]},
        )
        assert response.status_code == 200
        assert any(h.status_code == 303 for h in response.history)

        patch_count = conn.execute(
            "SELECT COUNT(*) AS n FROM patch WHERE book_id=?", (book_id,)
        ).fetchone()["n"]
        assert patch_count == 1
        assert _background_jobs(conn) == []


def test_book_level_generate_endpoint_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        response = client.post(f"/books/{book_id}/backgrounds/generate", json={"count": 3, "style": "anime"})
        assert response.status_code != 200
        assert _background_jobs(conn) == []
