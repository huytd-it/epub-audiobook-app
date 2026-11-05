"""Giọng hội thoại: payload, chọn engine theo kind, và lưu cấu hình sách."""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import db, repository
from app.config import settings
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.joblog import JobLogger
from app.jobqueue.handlers import audiobook_tts
from app.main import app
from app.tts_engine import normalize_tt_payload


class _RecordingEngine:
    """Engine giả nhớ voice của mình và mọi câu đã đọc."""

    sample_rate = 24000

    def __init__(self, voice):
        self.voice = voice
        self.calls = []

    def config_fingerprint(self) -> str:
        return f"fake:{self.voice}"

    def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None):
        self.calls.append(text)
        return np.zeros(self.sample_rate // 10, dtype="float32")


class _EngineRegistry:
    def __init__(self):
        self.engines: dict[str | None, _RecordingEngine] = {}

    def get(self, engine_id=None, voice=None, tts_options=None):
        return self.engines.setdefault(voice, _RecordingEngine(voice))


class _Ctx:
    """Context tối thiểu cho synthesize_patch (bỏ qua broker, xem test reuse)."""

    def __init__(self, conn):
        self.conn = conn
        self.logs = []

    def log(self, message, level=None):
        self.logs.append(message)

    def progress(self, *args, **kwargs):
        pass

    def should_cancel(self):
        return False


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    yield


TEXT = 'Anh ta dừng lại. "Cậu là ai?" Rồi im lặng.'


def _book_with_patch(conn, *, text=TEXT):
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


def _ctx(conn, **payload):
    job_id = store.enqueue(conn, "audiobook_tts", payload=payload, book_id=1)
    job = store.claim(conn, "audiobook_tts", "w")
    return JobContext(job, conn, JobLogger(job_id, "audiobook_tts"), lambda: False), job_id


def test_normalize_tt_payload_keeps_dialogue_voice():
    payload = normalize_tt_payload({"patch_id": 1, "voice": "narr", "dialogue_voice": "dlg"})
    assert payload["voice"] == "narr"
    assert payload["dialogue_voice"] == "dlg"
    assert normalize_tt_payload({"patch_id": 1, "voice": "narr"})["dialogue_voice"] is None


def test_dialogue_chunks_use_the_second_engine(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    registry = _EngineRegistry()
    monkeypatch.setattr(audiobook_tts, "get_engine", registry.get)
    monkeypatch.setattr(settings, "tts_write_chunk_files", False)

    ctx, _ = _ctx(conn, patch_id=patch_id, voice="narr", dialogue_voice="dlg")
    audiobook_tts.handle(ctx)

    narration = registry.engines["narr"].calls
    assert len(narration) == 2
    assert "Anh ta dừng lại." in narration[0]
    assert narration[1] == "Rồi im lặng."
    assert registry.engines["dlg"].calls == ['"Cậu là ai?"']


def test_without_dialogue_voice_a_single_engine_reads_everything(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    registry = _EngineRegistry()
    monkeypatch.setattr(audiobook_tts, "get_engine", registry.get)
    monkeypatch.setattr(settings, "tts_write_chunk_files", False)

    ctx, _ = _ctx(conn, patch_id=patch_id, voice="narr")
    audiobook_tts.handle(ctx)

    assert list(registry.engines) == ["narr"]
    assert len(registry.engines["narr"].calls) == 3
    assert registry.engines["narr"].calls[1] == '"Cậu là ai?"'


def test_changing_dialogue_voice_only_regenerates_dialogue_chunks(tmp_path, monkeypatch):
    from pathlib import Path

    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    monkeypatch.setattr(settings, "tts_write_chunk_files", True)
    patch = repository.get_patch(conn, patch_id)
    ctx = _Ctx(conn)

    def run(narration, dialogue):
        audiobook_tts.synthesize_patch(
            ctx, patch, narration, Path(settings.data_root),
            engine_id="fake", effective_max_chars=None, with_effects=False,
            dialogue_engine=dialogue,
        )

    narration = _RecordingEngine("narr")
    dialogue = _RecordingEngine("dlg")
    run(narration, dialogue)
    narration_calls = len(narration.calls)
    assert narration_calls == 2
    assert dialogue.calls == ['"Cậu là ai?"']

    # Cùng cấu hình: mọi chunk được dùng lại, không engine nào chạy thêm.
    run(narration, dialogue)
    assert len(narration.calls) == narration_calls
    assert len(dialogue.calls) == 1

    # Đổi giọng hội thoại: chỉ chunk thoại phải tổng hợp lại.
    dialogue2 = _RecordingEngine("dlg2")
    run(narration, dialogue2)
    assert len(narration.calls) == narration_calls
    assert dialogue2.calls == ['"Cậu là ai?"']


def test_audio_settings_route_stores_and_returns_dialogue_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO book (id, title, original_filename, epub_path, patch_size,
                                  status, created_at, updated_at)
               VALUES (1, 'Book', 'b.epub', 'b.epub', 10, 'ready', ?, ?)""", (now, now))
        conn.commit()

        saved = client.post("/books/1/audio-settings", json={
            "model_id": "edge-tts", "voice_id": "vi-VN-HoaiMyNeural",
            "dialogue_voice_id": "vi-VN-NamMinhNeural", "max_chars": 400,
            "with_effects": False,
        })
        got = client.get("/books/1/audio-settings")

    assert saved.status_code == 200
    assert saved.json()["dialogue_voice_id"] == "vi-VN-NamMinhNeural"
    assert got.json()["dialogue_voice_id"] == "vi-VN-NamMinhNeural"


def test_enqueue_pending_patch_jobs_passes_dialogue_voice(tmp_path, monkeypatch):
    from app.jobqueue.backfill import enqueue_pending_patch_jobs
    from app.production_defaults import save_book_audio_section

    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    patch_id = _book_with_patch(conn)
    monkeypatch.setattr(settings, "tts_engine", "edge-tts")
    save_book_audio_section(conn, 1, dialogue_voice_id="vi-VN-NamMinhNeural")
    conn.commit()

    assert enqueue_pending_patch_jobs(conn, 1) == 1
    jobs = store.list_jobs(conn)
    assert len(jobs) == 1
    assert jobs[0].payload["dialogue_voice"] == "vi-VN-NamMinhNeural"
    assert jobs[0].payload["patch_id"] == patch_id
