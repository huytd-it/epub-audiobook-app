"""Job `youtube_metadata_gen`: sinh mô tả + thẻ rồi ghi vào cấu hình YouTube
của sách, và route xếp job đó."""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import ai_content, db, repository
from app.config import settings
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.handlers import youtube_metadata as handler
from app.jobqueue.joblog import JobLogger
from app.jobqueue.models import JobFatalError
from app.production_defaults import get_effective_youtube_config


@pytest.fixture(autouse=True)
def _gemini_configured(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "ai_content_provider", "gemini")
    monkeypatch.setattr(settings, "ai_content_api_key", "test-key")
    monkeypatch.setattr(settings, "ai_content_model", "")
    monkeypatch.setattr(settings, "ai_content_base_url", "")


def _seed_book(conn, title: str = "Đại Vệ Chí Dị") -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO book (title,original_filename,epub_path,patch_size,status,created_at,updated_at)
           VALUES (?, 'b.epub', 'b.epub', 10, 'ready', ?, ?)""",
        (title, now, now),
    )
    book_id = cur.lastrowid
    for index in range(2):
        conn.execute(
            "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, ?, ?, 'x', 1)",
            (book_id, index, f"Chương {index + 1}"),
        )
    conn.commit()
    return book_id


def _run(conn, payload: dict, book_id: int | None = None):
    job_id = store.enqueue(conn, "youtube_metadata_gen", payload=payload, book_id=book_id)
    job = store.claim(conn, "youtube_metadata_gen", "w")
    ctx = JobContext(job, conn, JobLogger(job_id, "youtube_metadata_gen"), lambda: False)
    return handler.handle(ctx)


def test_missing_book_id_is_fatal():
    conn = _memory()
    with pytest.raises(JobFatalError):
        _run(conn, {})


def test_unknown_book_is_fatal():
    conn = _memory()
    with pytest.raises(JobFatalError):
        _run(conn, {"book_id": 999})


def test_provider_misconfiguration_is_fatal(monkeypatch):
    conn = _memory()
    book_id = _seed_book(conn)
    monkeypatch.setattr(settings, "ai_content_api_key", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(JobFatalError) as excinfo:
        _run(conn, {"book_id": book_id}, book_id=book_id)
    assert "GEMINI_API_KEY" in str(excinfo.value)


def test_handler_writes_only_description_and_genre_tags(monkeypatch):
    conn = _memory()
    book_id = _seed_book(conn)
    book = repository.get_book(conn, book_id)
    # Người dùng đã chỉnh sẵn phần này thì phải được giữ, không bị job xoá.
    from app.youtube_metadata import save_book_youtube_config

    config = get_effective_youtube_config(conn, book)
    save_book_youtube_config(
        conn, book_id, {**config, "description": "cũ", "genre_tags": "cũ", "privacy_status": "public"}
    )

    monkeypatch.setattr(ai_content, "complete", lambda prompt, config=None: json.dumps(
        {"description": "Mô tả mới", "tags": ["sách nói", "tiểu thuyết"]}
    ))
    result = _run(conn, {"book_id": book_id}, book_id=book_id)

    assert result["description"] == "Mô tả mới"
    assert result["genre_tags"] == "sách nói, tiểu thuyết"
    saved = get_effective_youtube_config(conn, repository.get_book(conn, book_id))
    assert saved["description"] == "Mô tả mới"
    assert saved["genre_tags"] == "sách nói, tiểu thuyết"
    assert saved["privacy_status"] == "public"


def test_handler_reports_progress_and_result(monkeypatch):
    conn = _memory()
    book_id = _seed_book(conn)
    monkeypatch.setattr(ai_content, "complete", lambda prompt, config=None: json.dumps(
        {"description": "Mô tả", "tags": ["sách nói"]}
    ))
    _run(conn, {"book_id": book_id}, book_id=book_id)
    job = store.list_jobs(conn, job_type="youtube_metadata_gen")[0]
    assert job.progress_total == 1
    assert job.progress_current == 1


def _memory():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


# ------------------------------------------------------------------- routes

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    from app.main import app
    conn = db.connect(str(tmp_path / "app.db"))
    db.init_schema(conn)
    app.state.conn, app.state.db_lock = conn, threading.Lock()
    app.state.worker = app.state.job_queue = None
    with TestClient(app) as test_client:
        app.state.conn = conn
        app.state.db_lock = threading.Lock()
        yield test_client, conn


def test_route_rejects_when_provider_is_missing(client, monkeypatch):
    test_client, conn = client
    monkeypatch.setattr(settings, "ai_content_api_key", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    book_id = _seed_book(conn)
    response = test_client.post(f"/books/{book_id}/youtube-metadata-generate")
    assert response.status_code == 400
    assert "GEMINI_API_KEY" in response.json()["detail"]
    assert store.list_jobs(conn, job_type="youtube_metadata_gen") == []


def test_route_enqueues_one_job_per_book(client):
    test_client, conn = client
    book_id = _seed_book(conn)
    response = test_client.post(f"/books/{book_id}/youtube-metadata-generate")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "queued"
    jobs = store.list_jobs(conn, job_type="youtube_metadata_gen")
    assert [job.book_id for job in jobs] == [book_id]
    assert jobs[0].payload["book_id"] == book_id

    # Bấm lần hai khi job còn sống => 409, không tạo job trùng.
    assert test_client.post(f"/books/{book_id}/youtube-metadata-generate").status_code == 409
    assert len(store.list_jobs(conn, job_type="youtube_metadata_gen")) == 1


def test_route_404_for_unknown_book(client):
    test_client, _ = client
    assert test_client.post("/books/999/youtube-metadata-generate").status_code == 404


def test_youtube_settings_reports_provider_status(client, monkeypatch):
    test_client, conn = client
    book_id = _seed_book(conn)
    status = test_client.get(f"/books/{book_id}/youtube-settings").json()["ai_content"]
    assert status["configured"] is True
    assert status["provider"] == "gemini"
    assert status["model"] == "gemini-2.5-flash"

    monkeypatch.setattr(settings, "ai_content_api_key", "")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    status = test_client.get(f"/books/{book_id}/youtube-settings").json()["ai_content"]
    assert status["configured"] is False
    assert "GEMINI_API_KEY" in status["detail"]
