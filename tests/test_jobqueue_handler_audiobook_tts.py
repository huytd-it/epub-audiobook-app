"""Handler audiobook_tts: synthesize, ghi tiến độ, resume, và nối chuỗi sang video."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest
import soundfile as sf

from app import db, repository
from app.config import settings
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.joblog import JobLogger
from app.jobqueue.handlers import audiobook_tts
from app.jobqueue.models import JobFatalError


class _FakeEngine:
    sample_rate = 24000

    def __init__(self):
        self.calls = []

    def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None):
        self.calls.append(text)
        return np.zeros(self.sample_rate // 10, dtype="float32")


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    yield


def _book_with_patch(conn, *, text="Câu một. Câu hai. Câu ba."):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size,
                              status, created_at, updated_at)
           VALUES (1, 'Sách', 'a.epub', '/tmp/a.epub', 10, 'ready', ?, ?)""", (now, now))
    conn.execute(
        """INSERT INTO chapter (book_id, chapter_index, title, text, char_count)
           VALUES (1, 0, 'Chương 1', ?, ?)""", (text, len(text)))
    cur = conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                               attempt_count, created_at, updated_at)
           VALUES (1, 0, 0, 0, 'pending', 0, ?, ?)""", (now, now))
    conn.commit()
    return cur.lastrowid


def _ctx(conn, job_type="audiobook_tts", **payload):
    job_id = store.enqueue(conn, job_type, payload=payload, book_id=1)
    job = store.claim(conn, job_type, "w")
    return JobContext(job, conn, JobLogger(job_id, job_type), lambda: False), job_id


def test_missing_patch_is_a_fatal_error(tmp_path):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    ctx, _ = _ctx(conn, patch_id=999)
    with pytest.raises(JobFatalError):
        audiobook_tts.handle(ctx)


def test_patch_with_no_speakable_text_is_fatal(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn, text="   ")
    monkeypatch.setattr(audiobook_tts, "get_engine", lambda *a, **k: _FakeEngine())
    ctx, _ = _ctx(conn, patch_id=patch_id)
    with pytest.raises(JobFatalError):
        audiobook_tts.handle(ctx)
    assert repository.get_patch(conn, patch_id).status == "failed"


def test_successful_run_writes_audio_and_marks_the_patch_done(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    engine = _FakeEngine()
    monkeypatch.setattr(audiobook_tts, "get_engine", lambda *a, **k: engine)
    ctx, job_id = _ctx(conn, patch_id=patch_id)
    result = audiobook_tts.handle(ctx)
    patch = repository.get_patch(conn, patch_id)
    assert patch.status == "done"
    assert patch.audio_path == result["audio_path"]
    assert sf.info(result["audio_path"]).frames > 0
    assert engine.calls


def test_retry_skips_tts_when_valid_audio_result_already_exists(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "existing.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    audio = tmp_path / "existing.wav"
    sf.write(audio, np.zeros(2400, dtype="float32"), 24000)
    conn.execute(
        "UPDATE patch SET status='failed', audio_path=?, error_message='worker interrupted' WHERE id=?",
        (str(audio), patch_id),
    )
    conn.commit()
    monkeypatch.setattr(
        audiobook_tts, "get_engine",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("TTS must be skipped")),
    )
    ctx, _ = _ctx(conn, patch_id=patch_id, auto_create_video=True)

    result = audiobook_tts.handle(ctx)

    assert result["skipped"] is True
    assert result["audio_path"] == str(audio)
    assert repository.get_patch(conn, patch_id).status == "done"
    jobs = store.list_jobs(conn, job_type="patch_video")
    assert len(jobs) == 1
    assert jobs[0].payload["patch_id"] == patch_id


def test_run_without_chunk_files_writes_audio_and_progress(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    engine = _FakeEngine()
    monkeypatch.setattr(audiobook_tts, "get_engine", lambda *a, **k: engine)
    monkeypatch.setattr(settings, "tts_write_chunk_files", False)
    ctx, job_id = _ctx(conn, patch_id=patch_id)

    result = audiobook_tts.handle(ctx)
    ctx.flush()

    job = store.get(conn, job_id)
    assert sf.info(result["audio_path"]).frames > 0
    assert job.progress_current == job.progress_total == result["chunks"]
    assert not (tmp_path / "books" / "1" / "patches" / f"{patch_id}_chunks").exists()


def test_progress_is_reported_per_chunk(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    monkeypatch.setattr(audiobook_tts, "get_engine", lambda *a, **k: _FakeEngine())
    ctx, job_id = _ctx(conn, patch_id=patch_id)
    audiobook_tts.handle(ctx)
    ctx.flush()
    job = store.get(conn, job_id)
    assert job.progress_total > 0
    assert job.progress_current == job.progress_total
    assert job.phase == "synthesizing"


def test_next_chunk_index_is_persisted_so_a_rerun_resumes(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    engine = _FakeEngine()
    monkeypatch.setattr(audiobook_tts, "get_engine", lambda *a, **k: engine)
    ctx, _ = _ctx(conn, patch_id=patch_id)
    audiobook_tts.handle(ctx)
    first_calls = len(engine.calls)
    repository._update_status(conn, "patch", patch_id, status="pending")
    ctx2, _ = _ctx(conn, patch_id=patch_id)
    audiobook_tts.handle(ctx2)
    assert len(engine.calls) == first_calls


def test_model_config_change_invalidates_chunks_and_restarts_at_zero(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn, text="Câu một rất dài. Câu hai rất dài. Câu ba rất dài. Câu bốn rất dài.")
    state = {"cfg": "a"}
    engine = _FakeEngine()
    engine.config_fingerprint = lambda: state["cfg"]
    monkeypatch.setattr(audiobook_tts, "get_engine", lambda *a, **k: engine)

    ctx, _ = _ctx(conn, patch_id=patch_id)
    audiobook_tts.handle(ctx)
    first_calls = len(engine.calls)

    # Same model/config + text => resume, nothing regenerated.
    repository._update_status(conn, "patch", patch_id, status="pending")
    ctx2, _ = _ctx(conn, patch_id=patch_id)
    audiobook_tts.handle(ctx2)
    assert len(engine.calls) == first_calls

    # Model/config fingerprint changed => stale chunks wiped, restart at zero.
    state["cfg"] = "b"
    repository._update_status(conn, "patch", patch_id, status="pending")
    ctx3, _ = _ctx(conn, patch_id=patch_id)
    audiobook_tts.handle(ctx3)
    assert len(engine.calls) > first_calls


def test_cancel_between_chunks_stops_the_run(tmp_path, monkeypatch):
    import asyncio
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn, text="Một. Hai. Ba. Bốn. Năm. Sáu. Bảy. Tám.")
    monkeypatch.setattr(audiobook_tts, "get_engine", lambda *a, **k: _FakeEngine())
    job_id = store.enqueue(conn, "audiobook_tts", payload={"patch_id": patch_id}, book_id=1)
    job = store.claim(conn, "audiobook_tts", "w")
    ctx = JobContext(job, conn, JobLogger(job_id, "audiobook_tts"), lambda: True)
    with pytest.raises(asyncio.CancelledError):
        audiobook_tts.handle(ctx)
    assert repository.get_patch(conn, patch_id).status != "done"


def test_finishing_the_last_patch_enqueues_a_video_job(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    conn.execute("UPDATE book SET background_image_path='/tmp/bg.jpg' WHERE id=1")
    conn.commit()
    monkeypatch.setattr(audiobook_tts, "get_engine", lambda *a, **k: _FakeEngine())
    ctx, _ = _ctx(conn, patch_id=patch_id)
    audiobook_tts.handle(ctx)
    assert repository.get_book(conn, 1).final_audio_path is not None
    video_jobs = store.list_jobs(conn, job_type="video")
    assert len(video_jobs) == 1
    assert video_jobs[0].payload["book_job_id"] == repository.get_book_job(conn, 1, "video").id


def test_no_video_job_when_the_book_has_no_image(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    monkeypatch.setattr(audiobook_tts, "get_engine", lambda *a, **k: _FakeEngine())
    ctx, _ = _ctx(conn, patch_id=patch_id)
    audiobook_tts.handle(ctx)
    assert store.list_jobs(conn, job_type="video") == []
