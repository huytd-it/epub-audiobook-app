"""Giọng đọc riêng từng patch: migration, lưu/trả, ưu tiên khi enqueue, credit."""
import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, image_overlay, repository
from app.config import settings
from app.jobqueue import store
from app.jobqueue.backfill import enqueue_pending_patch_jobs
from app.main import app


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size,
                              status, created_at, updated_at)
           VALUES (1, 'Book', 'a.epub', '/tmp/a.epub', 10, 'ready', ?, ?)""", (now, now))
    conn.commit()
    return conn


def _patch(conn, index=0):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                               attempt_count, created_at, updated_at)
           VALUES (1, ?, 0, 0, 'pending', 0, ?, ?)""", (index, now, now))
    conn.commit()
    return cur.lastrowid


def test_migration_adds_patch_voice_columns(tmp_path):
    conn = _conn(tmp_path)
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(patch)")}
    assert {"tts_model", "tts_voice_id"} <= cols
    patch_id = _patch(conn)
    patch = repository.get_patch(conn, patch_id)
    assert patch.tts_model is None and patch.tts_voice_id is None


def test_set_and_clear_patch_audio_settings(tmp_path):
    conn = _conn(tmp_path)
    patch_id = _patch(conn)
    assert repository.set_patch_audio_settings(conn, patch_id, "zerotts", "maichi") is True
    # Ghi trùng không chạm DB vô ích.
    assert repository.set_patch_audio_settings(conn, patch_id, "zerotts", "maichi") is False
    patch = repository.get_patch(conn, patch_id)
    assert (patch.tts_model, patch.tts_voice_id) == ("zerotts", "maichi")
    assert repository.clear_patch_audio_settings(conn, [patch_id, 9999]) == 1
    patch = repository.get_patch(conn, patch_id)
    assert patch.tts_model is None and patch.tts_voice_id is None
    assert repository.clear_patch_audio_settings(conn, [patch_id]) == 0
    assert repository.set_patch_audio_settings(conn, 9999, "x", "y") is False


def test_enqueue_patch_override_wins_over_explicit_request(tmp_path):
    conn = _conn(tmp_path)
    patch_id = _patch(conn)
    repository.set_patch_audio_settings(conn, patch_id, "gtts", "vi")
    assert enqueue_pending_patch_jobs(conn, tts_engine="voxcpm2", voice="clip.wav") == 1
    job = store.list_jobs(conn, job_type="audiobook_tts_api")[0]
    assert job.payload["patch_id"] == patch_id
    assert job.payload["tts_engine"] == "gtts"
    assert job.payload["voice"] == "vi"


def test_enqueue_patch_partial_override_per_field(tmp_path):
    """Chỉ gán model ở patch: model thắng, voice vẫn lấy request."""
    conn = _conn(tmp_path)
    patch_id = _patch(conn)
    repository.set_patch_audio_settings(conn, patch_id, "zerotts", None)
    assert enqueue_pending_patch_jobs(conn, tts_engine="voxcpm2", voice="clip.wav") == 1
    job = store.list_jobs(conn, job_type="audiobook_tts")[0]
    assert job.payload["tts_engine"] == "zerotts"
    assert job.payload["voice"] == "clip.wav"


def test_enqueue_inherit_uses_request_and_book_config(tmp_path):
    conn = _conn(tmp_path)
    patch_id = _patch(conn)
    assert enqueue_pending_patch_jobs(conn, tts_engine="voxcpm2", voice="clip.wav") == 1
    job = store.list_jobs(conn, job_type="audiobook_tts")[0]
    assert (job.payload["tts_engine"], job.payload["voice"]) == ("voxcpm2", "clip.wav")


def test_credit_prefers_patch_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "tts_api_providers", "")
    from types import SimpleNamespace

    book = SimpleNamespace(tts_model="edge-tts", tts_voice_id="vi-VN-HoaiMyNeural")
    patch = SimpleNamespace(tts_model="zerotts", tts_voice_id="maichi")
    credit = image_overlay.resolve_narrator_credit(book, patch)
    assert "ZeroTTS" in credit and "maichi" in credit
    # Patch không gán riêng -> credit sách.
    plain = SimpleNamespace(tts_model=None, tts_voice_id=None)
    assert image_overlay.resolve_narrator_credit(book, plain) == \
        "Giọng đọc: vi-VN-HoaiMyNeural · Edge TTS"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    with TestClient(app) as c:
        yield c


def _seed_client_book(client, tmp_path):
    from app.config import settings as s
    import sqlite3

    conn = sqlite3.connect(s.db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size,
                              status, created_at, updated_at)
           VALUES (1, 'Book', 'a.epub', '/tmp/a.epub', 10, 'ready', ?, ?)""", (now, now))
    cur = conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                               attempt_count, created_at, updated_at)
           VALUES (1, 0, 0, 0, 'pending', 0, ?, ?)""", (now, now))
    patch_id = cur.lastrowid
    conn.commit()
    conn.close()
    return patch_id


def test_generate_validates_patch_override_needs_book_clip(client, tmp_path):
    from app.config import settings as s
    import sqlite3

    patch_id = _seed_client_book(client, tmp_path)
    conn = sqlite3.connect(s.db_path)
    conn.execute("UPDATE patch SET tts_model='voxcpm2', tts_voice_id=NULL WHERE id=?", (patch_id,))
    conn.commit()
    conn.close()
    resp = client.post("/books/1/tts/generate", json={"patch_ids": [patch_id]})
    assert resp.status_code == 400


def test_generate_uses_patch_preset_voice_without_clip(client, tmp_path):
    from app.config import settings as s
    import sqlite3

    patch_id = _seed_client_book(client, tmp_path)
    conn = sqlite3.connect(s.db_path)
    conn.execute("UPDATE patch SET tts_voice_id='preset:zerotts:maichi' WHERE id=?", (patch_id,))
    conn.commit()
    conn.close()
    resp = client.post("/books/1/tts/generate", json={"patch_ids": [patch_id]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["queued"] == 1


def test_patch_audio_settings_routes(client, tmp_path):
    patch_id = _seed_client_book(client, tmp_path)
    resp = client.post(f"/books/1/patches/{patch_id}/audio-settings",
                       json={"tts_model": "zerotts", "tts_voice_id": "maichi"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["tts_model"] == "zerotts"
    resp = client.post(f"/books/1/patches/{patch_id}/audio-settings",
                       json={"tts_model": "nope", "tts_voice_id": ""})
    assert resp.status_code == 400
    resp = client.post("/books/1/patches/reset-voices", json={"patch_ids": [patch_id, 9999]})
    assert resp.status_code == 200 and resp.json()["reset"] == 1
    resp = client.post("/books/1/patches/9999/audio-settings", json={"tts_voice_id": "x"})
    assert resp.status_code == 404


def _snapshot(tmp_path, book_id, dirname, engine, voice):
    chunk_dir = tmp_path / "books" / str(book_id) / dirname
    chunk_dir.mkdir(parents=True, exist_ok=True)
    (chunk_dir / ".tts_request.json").write_text(
        json.dumps({"tts_engine": engine, "voice": voice}), encoding="utf-8")


def test_backfill_patch_voices_from_new_layout_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn = _conn(tmp_path)
    patch_id = _patch(conn)
    _snapshot(tmp_path, 1, "audio/1_001_chunks", "zerotts", "kimoanh")
    assert db._backfill_patch_voices_from_snapshots(conn) == 1
    conn.commit()
    patch = repository.get_patch(conn, patch_id)
    assert (patch.tts_model, patch.tts_voice_id) == ("zerotts", "kimoanh")


def test_backfill_patch_voices_from_legacy_layout_and_skips_set_or_bad(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn = _conn(tmp_path)
    legacy_id = _patch(conn)
    _snapshot(tmp_path, 1, f"patches/{legacy_id}_chunks", "vieneu-fast", "Hải Đăng")
    pinned_id = _patch(conn, index=1)
    repository.set_patch_audio_settings(conn, pinned_id, "gtts", "vi")
    bad_id = _patch(conn, index=2)
    _snapshot(tmp_path, 1, "audio/1_003_chunks", "nope-engine", "x")
    assert db._backfill_patch_voices_from_snapshots(conn) == 1
    conn.commit()
    assert repository.get_patch(conn, legacy_id).tts_model == "vieneu-fast"
    assert repository.get_patch(conn, legacy_id).tts_voice_id == "Hải Đăng"
    # Đã gán tay thì giữ nguyên; engine lạ thì bỏ qua.
    assert repository.get_patch(conn, pinned_id).tts_voice_id == "vi"
    assert repository.get_patch(conn, bad_id).tts_model is None
