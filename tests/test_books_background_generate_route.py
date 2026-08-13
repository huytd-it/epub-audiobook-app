"""Route tests: creating patches (rebuild/auto-build/extend) auto-enqueues a
patch-scoped background_gen job per new patch, with no book-level generate
endpoint left behind."""
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


def test_rebuild_enqueues_one_background_job_per_patch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        response = client.post(f"/books/{book_id}/patches/rebuild", json={"ranges": [[0, 1], [3, 5]]})
        assert response.status_code == 200
        assert len(response.json()) == 2

        jobs = _background_jobs(conn)
        assert len(jobs) == 2
        patch_ids = [row["id"] for row in conn.execute(
            "SELECT id FROM patch WHERE book_id=?", (book_id,)
        ).fetchall()]
        assert {job.patch_id for job in jobs} == set(patch_ids)
        assert all(job.book_id == book_id for job in jobs)
        assert {job.dedupe_key for job in jobs} == {f"background_gen:patch={pid}" for pid in patch_ids}
        for job in jobs:
            assert job.payload == {"patch_id": job.patch_id, "book_id": book_id}


def test_auto_build_enqueues_one_background_job_per_patch(tmp_path, monkeypatch):
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

        jobs = _background_jobs(conn)
        patch_ids = {row["id"] for row in conn.execute(
            "SELECT id FROM patch WHERE book_id=?", (book_id,)
        ).fetchall()}
        assert {job.patch_id for job in jobs} == patch_ids
        assert len(jobs) == 3  # 6 chapters / 2 = 3 patches


def test_extend_enqueues_only_for_newly_created_patches(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn, chapter_count=4, patch_size=2)

        first = client.post(f"/books/{book_id}/patches/rebuild", json={"ranges": [[0, 1]]})
        assert first.status_code == 200
        first_ids = {row["id"] for row in conn.execute(
            "SELECT id FROM patch WHERE book_id=?", (book_id,)
        ).fetchall()}
        assert {job.patch_id for job in _background_jobs(conn)} == first_ids

        second = client.post(f"/books/{book_id}/patches/extend")
        assert second.status_code == 200
        assert second.json()["created"] == 1
        second_ids = {row["id"] for row in conn.execute(
            "SELECT id FROM patch WHERE book_id=?", (book_id,)
        ).fetchall()}
        jobs = _background_jobs(conn)
        assert {job.patch_id for job in jobs} == second_ids
        assert len(jobs) == 2

        # No more uncovered chapters: extend creates nothing, enqueues nothing.
        again = client.post(f"/books/{book_id}/patches/extend")
        assert again.json()["created"] == 0
        assert len(_background_jobs(conn)) == 2


def test_rebuild_again_enqueues_for_the_fresh_patch_ids(tmp_path, monkeypatch):
    """A rebuild replaces rows: the old patch's jobs are cascade-deleted with
    it (job.patch_id -> patch ON DELETE CASCADE), and the fresh patch ids get
    their own jobs even though the previous ones finished."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        client.post(f"/books/{book_id}/patches/rebuild", json={"ranges": [[0, 2]]})
        first_jobs = _background_jobs(conn)
        assert len(first_jobs) == 1
        for job in first_jobs:
            store.finish(conn, job.id, None)

        client.post(f"/books/{book_id}/patches/rebuild", json={"ranges": [[0, 2]]})
        second_ids = {row["id"] for row in conn.execute(
            "SELECT id FROM patch WHERE book_id=?", (book_id,)
        ).fetchall()}
        jobs = _background_jobs(conn)
        assert {job.patch_id for job in jobs} == second_ids
        # Only the live job for the current patch remains: the finished one was
        # cascade-deleted with its (now replaced) patch row.
        assert len(jobs) == 1
        assert jobs[0].status == "pending"


def test_book_level_generate_endpoint_is_gone(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        response = client.post(f"/books/{book_id}/backgrounds/generate", json={"count": 3, "style": "anime"})
        assert response.status_code != 200
        assert _background_jobs(conn) == []