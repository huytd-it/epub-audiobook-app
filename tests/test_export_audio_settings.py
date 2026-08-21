from datetime import datetime, timezone

from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routes.patches import _save_export_audio_settings


def _insert_book(conn):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (
               id, title, original_filename, epub_path, patch_size, status,
               tts_model, tts_voice_id, tts_max_chars, tts_with_effects,
               created_at, updated_at
           ) VALUES (1, 'Book', 'book.epub', 'book.epub', 10, 'ready',
                     'edge-tts', 'vi-VN-HoaiMyNeural', 300, 1, ?, ?)""",
        (now, now),
    )
    conn.commit()


def test_export_audio_settings_use_remote_defaults_instead_of_local_config(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        _insert_book(client.app.state.conn)
        response = client.get("/books/1/export-audio-settings")

    assert response.status_code == 200
    assert response.json() == {
        "model_id": "omnivoice",
        "voice_id": "",
        "max_chars": 1200,
        "with_effects": False,
    }


def test_saved_export_audio_settings_do_not_change_local_config(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        _insert_book(conn)
        _save_export_audio_settings(
            conn, 1, model_id="gtts", voice_id="vi", max_chars=450, with_effects=0
        )
        conn.commit()

        export_response = client.get("/books/1/export-audio-settings")
        local_response = client.get("/books/1/audio-settings")

    assert export_response.json() == {
        "model_id": "gtts",
        "voice_id": "vi",
        "max_chars": 450,
        "with_effects": False,
    }
    assert local_response.json() == {
        "model_id": "edge-tts",
        "voice_id": "vi-VN-HoaiMyNeural",
        "max_chars": 300,
        "with_effects": True,
        "chunk_pause_ms": 300,
        "chapter_pause_ms": 1500,
    }


def test_export_audio_settings_can_be_saved_without_exporting(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        _insert_book(client.app.state.conn)
        response = client.post(
            "/books/1/export-audio-settings",
            json={
                "model_id": "omnivoice",
                "voice_id": "reference.wav",
                "max_chars": 1200,
                "with_effects": True,
            },
        )
        saved = client.get("/books/1/export-audio-settings")
        local = client.get("/books/1/audio-settings")

    assert response.status_code == 200
    assert saved.json() == response.json()
    assert local.json()["model_id"] == "edge-tts"
    assert local.json()["max_chars"] == 300
