"""Handler tests for the background_gen job type (patch-scoped)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import background_gen, db, repository
from app.config import settings
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.handlers import background_gen as background_gen_handler
from app.jobqueue.joblog import JobLogger
from app.jobqueue.models import JobFatalError
from app.video_config import get_book_video_config


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


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """fetch_image retries transient failures with a real backoff sleep; skip
    the wait so a failing-fetch test doesn't actually sleep."""
    monkeypatch.setattr(background_gen.time, "sleep", lambda seconds: None)


def _seed_book(conn) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO book (title,original_filename,epub_path,video_resolution,created_at,updated_at)
           VALUES ('Book','book.epub','book.epub','1920x1080',?,?)""",
        (now, now),
    )
    conn.commit()
    return cur.lastrowid


def _seed_patch(conn, book_id: int, patch_index: int = 0) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end,
                              chapter_no_start, chapter_no_end, name,
                              chunk_count, status, created_at, updated_at)
           VALUES (?, ?, 0, 1, 0, 1, 'P0', 1, 'pending', ?, ?)""",
        (book_id, patch_index, now, now),
    )
    conn.commit()
    return cur.lastrowid


def _run(conn, payload: dict, book_id: int | None = None, patch_id: int | None = None):
    job_id = store.enqueue(
        conn, "background_gen", payload=payload,
        book_id=book_id, patch_id=patch_id, dedupe_key=f"background_gen:patch={patch_id}",
    )
    job = store.claim(conn, "background_gen", "w")
    ctx = JobContext(job, conn, JobLogger(job_id, "background_gen"), lambda: False)
    return background_gen_handler.handle(ctx), job_id


def test_handle_rejects_a_missing_patch_id(conn):
    with pytest.raises(JobFatalError):
        _run(conn, {"book_id": 1})


def test_handle_rejects_an_unknown_patch(conn):
    with pytest.raises(JobFatalError):
        _run(conn, {"patch_id": 999, "book_id": 1}, book_id=1, patch_id=999)


def test_handle_generates_the_patch_image_and_sets_image_path(conn, monkeypatch):
    monkeypatch.setattr(background_gen.requests, "get", lambda *a, **k: FakeResponse())
    book_id = _seed_book(conn)
    patch_id = _seed_patch(conn, book_id)
    result, _ = _run(conn, {"patch_id": patch_id, "book_id": book_id}, book_id=book_id, patch_id=patch_id)
    assert result["patch_id"] == patch_id
    dest = Path(result["image_path"])
    assert dest.is_file()
    assert str(dest).startswith(str(Path(settings.data_root) / "backgrounds" / "patch_bg"))
    assert repository.get_patch(conn, patch_id).image_path == str(dest)
    # Never leaks into the shared video config.
    book = repository.get_book(conn, book_id)
    assert get_book_video_config(conn, book)["backgrounds"] == []


def test_handle_persists_the_rolled_variation_and_reports_progress(conn, monkeypatch):
    monkeypatch.setattr(background_gen.requests, "get", lambda *a, **k: FakeResponse())
    book_id = _seed_book(conn)
    patch_id = _seed_patch(conn, book_id)
    _run(conn, {"patch_id": patch_id, "book_id": book_id}, book_id=book_id, patch_id=patch_id)

    row = conn.execute(
        "SELECT payload_json, progress_current, progress_total, phase FROM job ORDER BY id DESC LIMIT 1"
    ).fetchone()
    payload = json.loads(row["payload_json"])
    variation = payload["variation"]
    assert variation["style"] in background_gen.STYLES
    assert variation["scene"] in background_gen._SCENE_DESCRIPTORS
    assert isinstance(variation["seed"], int)
    assert (row["progress_current"], row["progress_total"], row["phase"]) == (1, 1, "done")


def test_handle_retry_reuses_the_persisted_variation(conn, monkeypatch):
    """A retried job must reproduce the same draw (same prompt+seed), so the
    second attempt is a material-cache hit instead of a brand-new image."""
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(background_gen.requests, "get", fake_get)
    book_id = _seed_book(conn)
    patch_id = _seed_patch(conn, book_id)

    _, job_id = _run(conn, {"patch_id": patch_id, "book_id": book_id}, book_id=book_id, patch_id=patch_id)
    payload_after_first = json.loads(conn.execute(
        "SELECT payload_json FROM job WHERE id=?", (job_id,)
    ).fetchone()["payload_json"])
    assert calls["n"] == 1

    # Simulate the real retry cycle: fail the job, wait out the backoff, let a
    # fresh worker claim the same row, then run the handler again.
    store.fail(conn, job_id, "boom")
    conn.execute(
        "UPDATE job SET status='pending', next_retry_at=NULL, worker_id=NULL WHERE id=?",
        (job_id,),
    )
    conn.commit()
    job = store.claim(conn, "background_gen", "w2")
    assert job is not None
    ctx = JobContext(job, conn, JobLogger(job_id, "background_gen"), lambda: False)
    background_gen_handler.handle(ctx)
    assert calls["n"] == 1  # cache hit: no new network fetch
    payload_after_second = json.loads(conn.execute(
        "SELECT payload_json FROM job WHERE id=?", (job_id,)
    ).fetchone()["payload_json"])
    assert payload_after_first["variation"] == payload_after_second["variation"]


def test_handle_rolls_a_different_variation_for_a_new_patch(conn, monkeypatch):
    monkeypatch.setattr(background_gen.requests, "get", lambda *a, **k: FakeResponse())
    draws = iter([
        {"style": "anime", "scene": background_gen._SCENE_DESCRIPTORS[0], "seed": 1},
        {"style": "watercolor", "scene": background_gen._SCENE_DESCRIPTORS[3], "seed": 2},
    ])
    monkeypatch.setattr(background_gen, "roll_variation", lambda: next(draws))
    book_id = _seed_book(conn)
    patch_a = _seed_patch(conn, book_id, 0)
    patch_b = _seed_patch(conn, book_id, 1)

    _run(conn, {"patch_id": patch_a, "book_id": book_id}, book_id=book_id, patch_id=patch_a)
    _run(conn, {"patch_id": patch_b, "book_id": book_id}, book_id=book_id, patch_id=patch_b)

    rows = conn.execute(
        "SELECT patch_id, payload_json FROM job WHERE job_type='background_gen' ORDER BY id"
    ).fetchall()
    variations = {row["patch_id"]: json.loads(row["payload_json"])["variation"] for row in rows}
    assert variations[patch_a] != variations[patch_b]


def test_handle_fails_cleanly_when_the_fetch_never_succeeds(conn, monkeypatch):
    monkeypatch.setattr(
        background_gen.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(background_gen.requests.ConnectionError("boom")),
    )
    book_id = _seed_book(conn)
    patch_id = _seed_patch(conn, book_id)
    with pytest.raises(background_gen.requests.ConnectionError):
        _run(conn, {"patch_id": patch_id, "book_id": book_id}, book_id=book_id, patch_id=patch_id)
    # The variation was still persisted, so a retry reproduces the same draw.
    row = conn.execute(
        "SELECT payload_json FROM job ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert "variation" in json.loads(row["payload_json"])
    assert repository.get_patch(conn, patch_id).image_path is None