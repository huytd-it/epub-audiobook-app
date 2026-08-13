"""Handler tests for the background_gen job type."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app import background_gen, db
from app.config import settings
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.handlers import background_gen as background_gen_handler
from app.jobqueue.joblog import JobLogger
from app.jobqueue.models import JobFatalError


class FakeResponse:
    def __init__(self, content: bytes = b"jpeg-bytes", content_type: str = "image/jpeg"):
        self.content = content
        self.headers = {"content-type": content_type}

    def raise_for_status(self):
        pass


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def _seed_book(conn) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO book (title,original_filename,epub_path,video_resolution,created_at,updated_at)
           VALUES ('Book','book.epub','book.epub','1920x1080',?,?)""",
        (now, now),
    )
    conn.commit()
    return cur.lastrowid


def _run(conn, payload: dict, book_id: int | None = None):
    job_id = store.enqueue(conn, "background_gen", payload=payload, book_id=book_id)
    job = store.claim(conn, "background_gen", "w")
    ctx = JobContext(job, conn, JobLogger(job_id, "background_gen"), lambda: False)
    return background_gen_handler.handle(ctx)


def test_handle_rejects_a_missing_book_id(conn):
    with pytest.raises(JobFatalError):
        _run(conn, {"count": 2, "style": "realistic"})


def test_handle_rejects_an_unknown_book(conn):
    with pytest.raises(JobFatalError):
        _run(conn, {"book_id": 999, "count": 2, "style": "realistic"})


def test_handle_rejects_an_invalid_count(conn):
    book_id = _seed_book(conn)
    with pytest.raises(JobFatalError):
        _run(conn, {"book_id": book_id, "count": 0, "style": "realistic"})


def test_handle_rejects_an_invalid_style(conn):
    book_id = _seed_book(conn)
    with pytest.raises(JobFatalError):
        _run(conn, {"book_id": book_id, "count": 2, "style": "not-a-style"}, book_id=book_id)


def test_handle_generates_backgrounds_and_reports_progress(conn, monkeypatch):
    monkeypatch.setattr(background_gen.requests, "get", lambda *a, **k: FakeResponse())
    book_id = _seed_book(conn)
    result = _run(conn, {"book_id": book_id, "count": 3, "style": "realistic"}, book_id=book_id)
    assert len(result["generated"]) == 3
    job = conn.execute("SELECT progress_current, progress_total, phase FROM job WHERE id = (SELECT MAX(id) FROM job)").fetchone()
    assert (job["progress_current"], job["progress_total"], job["phase"]) == (3, 3, "done")


def test_handle_falls_back_to_defaults_when_count_and_style_are_omitted(conn, monkeypatch):
    monkeypatch.setattr(background_gen.requests, "get", lambda *a, **k: FakeResponse())
    book_id = _seed_book(conn)
    result = _run(conn, {"book_id": book_id}, book_id=book_id)
    assert len(result["generated"]) == 4  # background_gen's default count
