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


def test_book_detail_has_youtube_settings_and_no_whole_book_audio(client, seeded_book):
    html = client.get(f"/books/{seeded_book.id}").text
    assert 'data-open-dialog="youtube-settings-modal"' in html
    assert 'id="patch-youtube-modal"' in html
    assert f'href="/books/{seeded_book.id}/download/audio"' not in html


def test_missing_book_detail_returns_404(client):
    response = client.get("/books/999999")
    assert response.status_code == 404


def test_patch_row_exposes_pipeline_stage(client, seeded_book):
    conn = db.connect(settings.db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,
           audio_path,created_at,updated_at) VALUES (1,1,0,0,1,'done','/tmp/a.wav',?,?)""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO patch_pipeline (patch_id,stage,config_snapshot,media_snapshot,created_at,updated_at)
           VALUES (1,'published','{}','{}',?,?)""",
        (now, now),
    )
    conn.commit()
    conn.close()
    html = client.get(f"/books/{seeded_book.id}").text
    assert "Published" in html


def test_patch_media_modal_has_only_audio_and_video_uploads(client, seeded_book):
    conn = db.connect(settings.db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,created_at,updated_at)
           VALUES (1,1,0,0,1,'pending',?,?)""",
        (now, now),
    )
    conn.commit()
    conn.close()

    html = client.get(f"/books/{seeded_book.id}").text

    assert ">Media</button>" in html
    assert ">More</button>" not in html
    assert 'id="pm-audio-file"' in html
    assert 'id="pm-video-file"' in html
    assert 'id="pm-bg-select"' not in html
    assert "patch-bg-select" not in html
    assert "patch-bg-save-btn" not in html


def test_book_detail_youtube_controls_use_exact_settings_shape(client, seeded_book):
    html = client.get(f"/books/{seeded_book.id}").text
    for field in ("privacy_status", "auto_upload", "genre_tags", "title_template", "description_template"):
        assert f'name="{field}"' in html
    assert 'name="playlist_id"' in html
    assert 'id="youtube-connection-state"' in html
    assert 'id="youtube-preview"' in html


def test_video_config_uses_media_library_background_checkboxes(client, seeded_book):
    media_dir = __import__("pathlib").Path(settings.data_root) / "backgrounds"
    media_dir.mkdir(parents=True, exist_ok=True)
    (media_dir / "a.png").write_bytes(b"image")
    (media_dir / "b.mp4").write_bytes(b"video")
    html = client.get(f"/books/{seeded_book.id}").text
    assert html.count('class="vc-background-check"') >= 2
    assert "a.png" in html and "b.mp4" in html
    assert '<video src="/video/backgrounds/preview?' in html
    assert '<img src="/video/backgrounds/preview?' in html
    assert "selectedBackgroundOrder.slice()" in html
    assert "selectedBackgroundOrder.push(e.target.value)" in html
    assert '<textarea id="vc-backgrounds"' not in html


def test_patch_youtube_modal_renders_override_controls_and_metadata_flow(client, seeded_book):
    html = client.get(f"/books/{seeded_book.id}").text
    for field in ("title", "description", "genre_tags", "privacy_status"):
        assert f'id="patch-{field}"' in html
    assert '<textarea id="patch-description"' in html
    assert "Use book default" in html
    assert "/youtube-metadata" in html
    assert "response.ok" in html
    assert "force_new" in html
    assert "join(', ')" in html
    assert "PATCH_INHERITED = new Set" in html
    assert "delete fields.playlist" in html
    assert "settingsForm.privacy_status.value" in html
    assert "youtube-metadata`" in html
    assert "action === 'retry'" in html
    assert "action.disabled = true" in html
    assert "loadPatchMetadata(button.dataset.patchYoutubeId).then" in html
    assert "pipeline-status" in html or "patch-pipeline-" in html
    assert "if (!statusResponse.ok)" in html
    assert "Patch queued; status refresh failed" in html
    assert "return true" in html
    assert "catch (error)" in html
    assert "if (loaded) actions.forEach(action => action.disabled = false)" in html
    assert "refreshYoutubeSettings" in html
    assert "youtube-settings-error" in html
    assert "pv-pipeline-state" in html
    assert "video.textContent" not in html
    assert "if (action === 'save')" in html
    assert "showToast('Metadata saved', 'success')" in html
    assert "return;" in html
    assert "try {" in html
    assert "Metadata load failed" in html
    assert "finally" in html
    assert "clickedAction.disabled = false" in html
    assert "yts-preview-error" in html
    assert "Preview failed" in html
    assert "/books/${BOOK_ID}/patches/${patchId}/overlay-image" in html
    assert "/books/${BOOK_ID}/patches/${PATCH_ID}/" not in html


def test_save_branch_precedes_publish_url_selection(client, seeded_book):
    html = client.get(f"/books/{seeded_book.id}").text
    save_pos = html.index("if (action === 'save')")
    publish_url_pos = html.index("const url = action === 'retry'")
    assert save_pos < publish_url_pos


def test_patch_metadata_endpoint_includes_pipeline_payload(client, seeded_book):
    conn = db.connect(settings.db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,created_at,updated_at) VALUES (1,1,0,0,1,'done',?,?)", (now, now))
    conn.execute("""INSERT INTO patch_pipeline (patch_id,stage,last_error,thumbnail_path,video_path,thumbnail_status,video_status,upload_status,playlist_status,config_snapshot,media_snapshot,created_at,updated_at)
                    VALUES (1,'upload','oops','/thumb.jpg','/video.mp4','done','done','processing','pending','{}','{}',?,?)""", (now, now))
    conn.commit(); conn.close()
    payload = client.get("/books/1/patches/1/youtube-metadata").json()
    assert payload["pipeline"] == {"stage": "upload", "last_error": "oops", "thumbnail_path": "/thumb.jpg", "video_path": "/video.mp4", "thumbnail_status": "done", "video_status": "done", "upload_status": "processing", "playlist_status": "pending"}
