import json
import sqlite3
import threading

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import app
from app.config import settings
from app.patch_publishing import enqueue_patch_publish, retry_patch_publish, seed_patch_video
from app.routes import books, patches
from app.youtube_metadata import DEFAULT_BOOK_YOUTUBE_CONFIG, validate_book_youtube_config
from app import patch_publishing, youtube
from app import upload_worker


def _client(tmp_path):
    conn = db.connect(str(tmp_path / "routes.db"))
    db.init_schema(conn)
    app.state.conn = conn
    app.state.db_lock = threading.Lock()
    app.state.worker = None
    app.state.job_queue = object()
    return conn, TestClient(app)


def _seed(conn, *, auto_upload=False, audio_path="/tmp/audio.wav"):
    now = "2026-01-01T00:00:00"
    conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at, automation_config) VALUES (?, ?, ?, ?, ?, ?)", ("Book", "book.epub", "/tmp/book.epub", now, now, json.dumps({"youtube": {"auto_upload": auto_upload}})))
    book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, audio_path, created_at, updated_at) VALUES (?, ?, ?, ?, 'done', ?, ?, ?)", (book_id, 0, 1, 1, audio_path, now, now))
    patch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    return book_id, patch_id


def test_default_youtube_config_has_disabled_boolean_auto_upload():
    assert DEFAULT_BOOK_YOUTUBE_CONFIG["auto_upload"] is False
    assert validate_book_youtube_config({})["auto_upload"] is False


def test_auto_upload_must_be_boolean():
    with pytest.raises(ValueError, match="auto_upload"):
        validate_book_youtube_config({"auto_upload": "true"})


def test_seeded_video_is_resumed_not_only_enqueued(monkeypatch):
    calls = []
    monkeypatch.setattr(patch_publishing, "run_patch_publish_stage", lambda conn, patch_id: calls.append(patch_id) or {"stage": "upload"})
    monkeypatch.setattr(patch_publishing, "_row", lambda conn, patch_id: {"patch_id": patch_id})
    result = patch_publishing.retry_patch_publish(object(), 7)
    assert result == {"stage": "upload"}
    assert calls == [7]


def test_manual_audio_upload_auto_enqueues_only_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(patch_publishing, "ensure_patch_overlay", lambda *args, **kwargs: "/tmp/thumb.png")
    conn, client = _client(tmp_path)
    book_id, patch_id = _seed(conn, auto_upload=True)
    response = client.post(f"/books/{book_id}/patches/{patch_id}/upload-audio", files={"audio": ("a.wav", b"RIFF", "audio/wav")})
    assert response.status_code == 200
    assert conn.execute("SELECT stage FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone()[0] == "thumbnail"
    conn.close()

    conn, client = _client(tmp_path)
    book_id, patch_id = _seed(conn, auto_upload=False)
    client.post(f"/books/{book_id}/patches/{patch_id}/upload-audio", files={"audio": ("a.wav", b"RIFF", "audio/wav")})
    assert conn.execute("SELECT COUNT(*) FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone()[0] == 0


@pytest.mark.parametrize("configured, credentials, status", [(False, True, 400), (True, False, 400)])
def test_settings_rejects_disconnected_auto_upload(tmp_path, monkeypatch, configured, credentials, status):
    conn, client = _client(tmp_path)
    book_id, _ = _seed(conn)
    monkeypatch.setattr(books.youtube, "is_configured", lambda: configured)
    monkeypatch.setattr(books.youtube, "get_creds_from_db", lambda conn: {"id": 1} if credentials else None)
    response = client.post(f"/books/{book_id}/youtube-settings", json={"auto_upload": True, "playlist": {"mode": "create", "title_template": "{book_title}"}})
    assert response.status_code == status


def test_settings_rejects_inaccessible_existing_playlist(tmp_path, monkeypatch):
    conn, client = _client(tmp_path)
    book_id, _ = _seed(conn)
    monkeypatch.setattr(books.youtube, "is_configured", lambda: True)
    monkeypatch.setattr(books.youtube, "get_creds_from_db", lambda conn: {"id": 1})
    monkeypatch.setattr(books.youtube, "list_playlists", lambda conn: [{"id": "other"}])
    response = client.post(f"/books/{book_id}/youtube-settings", json={"auto_upload": True, "playlist": {"mode": "existing", "playlist_id": "missing"}})
    assert response.status_code == 400


def test_settings_rejects_auto_create_playlist(tmp_path, monkeypatch):
    conn, client = _client(tmp_path)
    book_id, _ = _seed(conn)
    monkeypatch.setattr(books.youtube, "is_configured", lambda: True)
    monkeypatch.setattr(books.youtube, "get_creds_from_db", lambda conn: {"id": 1})
    calls = []
    monkeypatch.setattr(books.youtube, "list_playlists", lambda conn: calls.append(True) or [])
    response = client.post(f"/books/{book_id}/youtube-settings", json={"auto_upload": True, "playlist": {"mode": "create", "title_template": "{book_title}"}})
    assert response.status_code == 400
    assert "no longer supported" in response.json()["detail"]
    assert calls == []




def test_metadata_get_returns_effective_metadata_and_override(tmp_path):
    conn, client = _client(tmp_path)
    book_id, patch_id = _seed(conn)
    conn.execute("UPDATE patch SET youtube_override=? WHERE id=?", (json.dumps({"title": "Custom"}), patch_id)); conn.commit()
    response = client.get(f"/books/{book_id}/patches/{patch_id}/youtube-metadata")
    assert response.status_code == 200
    assert response.json()["metadata"]["title"] == "Custom"
    assert response.json()["override"] == {"title": "Custom"}


def test_publish_saves_override_before_enqueue(tmp_path, monkeypatch):
    conn, client = _client(tmp_path)
    book_id, patch_id = _seed(conn)
    monkeypatch.setattr(patches.youtube, "is_configured", lambda: True)
    monkeypatch.setattr(patches.youtube, "get_creds_from_db", lambda conn: {"id": 1})
    upload_worker.upload_worker = type("Worker", (), {"get_status": lambda self: {"running": True}})()
    seen = []
    # The pipeline resolves metadata from the patch row, so the override has to be
    # persisted before the stage runs - spy on the stage, not on the reset helper.
    monkeypatch.setattr(patches, "run_patch_publish_stage", lambda c, p, **kw: seen.append(c.execute("SELECT youtube_override FROM patch WHERE id=?", (p,)).fetchone()[0]) or {"patch_id": p})
    response = client.post(f"/books/{book_id}/patches/{patch_id}/publish", json={"title": "Saved first"})
    assert response.status_code == 200
    assert json.loads(seen[0])["title"] == "Saved first"


def test_publish_creates_the_upload_it_enqueues(tmp_path, monkeypatch):
    """enqueue_patch_publish never returns a youtube_upload_id - it nulls the column.

    So the publish route has to advance the pipeline before it can enqueue anything,
    otherwise 'Save & Upload' leaves a stage='upload' row that nothing ever picks up.
    """
    conn, client = _client(tmp_path)
    book_id, patch_id = _seed(conn)
    video_path = tmp_path / "v.mp4"; video_path.write_bytes(b"video")
    thumb_path = tmp_path / "thumb.png"; thumb_path.write_bytes(b"thumb")
    video_id = conn.execute("INSERT INTO videos (filename, original_name, file_path, file_size_bytes, created_at, updated_at) VALUES ('v.mp4', 'v.mp4', '/tmp/v.mp4', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)").lastrowid
    conn.commit()
    monkeypatch.setattr(patch_publishing, "ensure_patch_overlay", lambda *args, **kwargs: str(thumb_path))
    seed_patch_video(conn, patch_id, video_id, str(video_path))
    monkeypatch.setattr(patches.youtube, "is_configured", lambda: True)
    monkeypatch.setattr(patches.youtube, "get_creds_from_db", lambda conn: {"id": 1})

    response = client.post(f"/books/{book_id}/patches/{patch_id}/publish", json={})

    assert response.status_code == 200
    upload_id = conn.execute(
        "SELECT youtube_upload_id FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone()[0]
    assert upload_id is not None, "publish did not create a youtube_uploads row"
    job = conn.execute("SELECT payload_json FROM job WHERE job_type='youtube_upload'").fetchone()
    assert job is not None, "publish created an upload but never queued the job for it"
    assert json.loads(job["payload_json"])["upload_id"] == upload_id


def test_publish_force_new_alone_preserves_saved_override(tmp_path, monkeypatch):
    conn, client = _client(tmp_path)
    book_id, patch_id = _seed(conn)
    monkeypatch.setattr(patches.youtube, "is_configured", lambda: True)
    monkeypatch.setattr(patches.youtube, "get_creds_from_db", lambda conn: {"id": 1})
    upload_worker.upload_worker = type("Worker", (), {"get_status": lambda self: {"running": True}})()
    conn.execute("UPDATE patch SET youtube_override=? WHERE id=?", (json.dumps({"title": "Keep me"}), patch_id)); conn.commit()
    monkeypatch.setattr(patches, "run_patch_publish_stage", lambda c, p, **kw: {"patch_id": p})
    response = client.post(f"/books/{book_id}/patches/{patch_id}/publish", json={"force_new": True})
    assert response.status_code == 200
    assert json.loads(conn.execute("SELECT youtube_override FROM patch WHERE id=?", (patch_id,)).fetchone()[0]) == {"title": "Keep me"}


def test_seed_video_retry_creates_one_upload_and_force_new_preserves_video(tmp_path, monkeypatch):
    conn, _ = _client(tmp_path)
    book_id, patch_id = _seed(conn)
    video_path = tmp_path / "v.mp4"; video_path.write_bytes(b"video")
    thumb_path = tmp_path / "thumb.png"; thumb_path.write_bytes(b"thumb")
    video_id = conn.execute("INSERT INTO videos (filename, original_name, file_path, file_size_bytes, created_at, updated_at) VALUES ('v.mp4', 'v.mp4', '/tmp/v.mp4', 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)").lastrowid
    conn.commit()
    monkeypatch.setattr(patch_publishing, "ensure_patch_overlay", lambda *args, **kwargs: str(thumb_path))
    seed_patch_video(conn, patch_id, video_id, str(video_path))
    retry_patch_publish(conn, patch_id)
    assert conn.execute("SELECT COUNT(*) FROM youtube_uploads").fetchone()[0] == 1
    upload_id = conn.execute("SELECT youtube_upload_id FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone()[0]
    assert upload_id is not None
    conn.execute("UPDATE patch_pipeline SET video_status='done', video_id=?, video_path=?, thumbnail_status='done', thumbnail_path=? WHERE patch_id=?", (video_id, str(video_path), str(thumb_path), patch_id)); conn.commit()
    response = _client(tmp_path)[1].post(f"/books/{book_id}/patches/{patch_id}/publish/retry?force_new=true")
    assert response.status_code == 200
    row = conn.execute("SELECT video_id, video_path, stage, youtube_upload_id FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone()
    # force_new keeps the rendered video but must re-upload it as a *new* YouTube video,
    # so the row points at a second upload rather than being left with none at all.
    assert tuple(row)[:3] == (video_id, str(video_path), "upload")
    assert row["youtube_upload_id"] not in (None, upload_id)
    assert conn.execute("SELECT COUNT(*) FROM youtube_uploads").fetchone()[0] == 2
