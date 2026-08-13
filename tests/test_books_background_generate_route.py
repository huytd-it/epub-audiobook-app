"""Route test for POST /books/{book_id}/backgrounds/generate."""
from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def _seed_book(conn) -> int:
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO book (title,original_filename,epub_path,created_at,updated_at) VALUES ('Book','b.epub','b.epub',?,?)",
        (now, now),
    )
    conn.commit()
    return cur.lastrowid


def test_trigger_enqueues_a_background_gen_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        response = client.post(f"/books/{book_id}/backgrounds/generate", json={"count": 3, "style": "anime"})
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "queued"
        assert body["job_id"] is not None

        row = conn.execute(
            "SELECT job_type, book_id, payload_json FROM job WHERE id = ?", (body["job_id"],)
        ).fetchone()
        assert row["job_type"] == "background_gen"
        assert row["book_id"] == book_id
        assert json.loads(row["payload_json"]) == {"book_id": book_id, "count": 3, "style": "anime"}


def test_trigger_is_deduped_while_a_run_is_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        first = client.post(f"/books/{book_id}/backgrounds/generate", json={"count": 2, "style": "realistic"})
        second = client.post(f"/books/{book_id}/backgrounds/generate", json={"count": 2, "style": "realistic"})
        assert first.json()["status"] == "queued"
        assert second.json() == {"status": "already_queued", "job_id": None}


def test_trigger_rejects_an_unknown_book(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        response = client.post("/books/999/backgrounds/generate", json={"count": 2, "style": "realistic"})
        assert response.status_code == 404


def test_trigger_rejects_an_invalid_count(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        response = client.post(f"/books/{book_id}/backgrounds/generate", json={"count": 50, "style": "realistic"})
        assert response.status_code == 400


def test_trigger_rejects_an_invalid_style(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        book_id = _seed_book(conn)
        response = client.post(f"/books/{book_id}/backgrounds/generate", json={"count": 2, "style": "vaporwave"})
        assert response.status_code == 400
