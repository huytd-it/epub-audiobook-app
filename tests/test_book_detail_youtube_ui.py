from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded_book(tmp_path):
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,
           final_audio_path,created_at,updated_at)
           VALUES (1,'Book','book.epub','/tmp/book.epub',10,'done','/tmp/book.wav',?,?)""",
        (now, now),
    )
    conn.commit()
    conn.close()
    return type("BookRef", (), {"id": 1})()



def test_ui_routes_serve_the_react_spa(client, seeded_book):
    for path in ("/books", "/books/upload", "/books/1", "/queue", "/youtube"):
        response = client.get(path)
        assert response.status_code == 200
        assert '<div id="root"></div>' in response.text
        assert "text/html" in response.headers["content-type"]


def test_legacy_template_directories_are_removed():
    from pathlib import Path

    assert not Path("app/templates").exists()
    assert not Path("app/static").exists()


def test_patch_metadata_endpoint_includes_pipeline_payload(client, seeded_book):
    conn = db.connect(settings.db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,created_at,updated_at) VALUES (1,1,0,0,1,'done',?,?)", (now, now))
    conn.execute("""INSERT INTO patch_pipeline (patch_id,stage,last_error,thumbnail_path,video_path,thumbnail_status,video_status,upload_status,playlist_status,config_snapshot,media_snapshot,created_at,updated_at)
                    VALUES (1,'upload','oops','/thumb.jpg','/video.mp4','done','done','processing','pending','{}','{}',?,?)""", (now, now))
    conn.commit(); conn.close()
    payload = client.get("/books/1/patches/1/youtube-metadata").json()
    assert payload["pipeline"] == {"stage": "upload", "last_error": "oops", "thumbnail_path": "/thumb.jpg", "video_path": "/video.mp4", "thumbnail_status": "done", "video_status": "done", "upload_status": "processing", "playlist_status": "pending"}
