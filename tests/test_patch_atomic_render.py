from datetime import datetime, timezone
import logging
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.jobqueue import store
from app.main import app
from app.patch_publishing import enqueue_patch_publish, run_patch_publish_stage
from app.routes import patches
from app.video_integrity import ValidationFacts, ValidationResult


VALID = ValidationResult(True, None, "", (), ValidationFacts(), 0)


def _seed(conn, audio):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,video_resolution,created_at,updated_at) VALUES (1,'B','b','b',1,'done','1280x720',?,?)", (now, now))
    conn.execute("INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,audio_path,created_at,updated_at) VALUES (1,1,0,0,0,'done',?,?,?)", (str(audio), now, now)); conn.commit()


def test_generate_patch_route_enqueues_without_rendering(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db")); monkeypatch.setattr(settings, "data_root", str(tmp_path)); monkeypatch.setattr(settings, "enable_worker", False)
    audio = tmp_path / "a.wav"; audio.write_bytes(b"a"); image = tmp_path / "i.jpg"; image.write_bytes(b"i"); seen = {}
    monkeypatch.setattr(patches.image_overlay, "ensure_patch_overlay", lambda *a, **k: str(image))
    monkeypatch.setattr(patches.video_gen, "generate_segment", lambda *a, **k: seen.update(render=True))
    with TestClient(app) as client:
        _seed(client.app.state.conn, audio)
        response = client.post("/books/1/patches/1/generate-video?ajax=1")
        job = store.get(client.app.state.conn, response.json()["job_id"])
    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["deduplicated"] is False
    assert job.job_type == "patch_video"
    assert job.book_id == 1
    assert job.payload == {"patch_id": 1, "upload_youtube": False, "privacy": ""}
    assert "render" not in seen


def test_generate_patch_route_deduplicates_live_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db")); monkeypatch.setattr(settings, "data_root", str(tmp_path)); monkeypatch.setattr(settings, "enable_worker", False)
    audio = tmp_path / "a.wav"; audio.write_bytes(b"a"); image = tmp_path / "i.jpg"; image.write_bytes(b"i")
    monkeypatch.setattr(patches.image_overlay, "ensure_patch_overlay", lambda *a, **k: str(image))
    with TestClient(app) as client:
        _seed(client.app.state.conn, audio)
        first = client.post("/books/1/patches/1/generate-video?ajax=1")
        second = client.post("/books/1/patches/1/generate-video?ajax=1")
    assert second.status_code == 202
    assert second.json() == {"status": "queued", "job_id": first.json()["job_id"], "deduplicated": True}


def test_generate_patch_route_requeues_finished_video_without_deleting_current_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db")); monkeypatch.setattr(settings, "data_root", str(tmp_path)); monkeypatch.setattr(settings, "enable_worker", False)
    audio = tmp_path / "a.wav"; audio.write_bytes(b"a")
    image = tmp_path / "i.jpg"; image.write_bytes(b"i")
    video = tmp_path / "books" / "1" / "videos" / "1_001.mp4"
    video.parent.mkdir(parents=True); video.write_bytes(b"current-video")
    now = datetime.now(timezone.utc).isoformat()
    with TestClient(app) as client:
        conn = client.app.state.conn
        _seed(conn, audio)
        conn.execute("UPDATE patch SET image_path=? WHERE id=1", (str(image),))
        conn.execute(
            """INSERT INTO patch_pipeline
                   (patch_id,stage,thumbnail_status,video_status,upload_status,playlist_status,
                    thumbnail_path,video_path,render_attempts,last_error,config_snapshot,
                    media_snapshot,created_at,updated_at)
                 VALUES (1,'upload','done','done','pending','pending',?,?,3,'render failed',
                         '{}','{}',?,?)""",
            (str(image), str(video), now, now),
        )
        conn.commit()

        response = client.post(
            "/books/1/patches/1/generate-video?ajax=1",
            data={"requeue": "true"},
        )
        pipeline = conn.execute(
            "SELECT stage,video_status,render_attempts,last_error,video_path FROM patch_pipeline WHERE patch_id=1"
        ).fetchone()

    assert response.status_code == 202
    assert response.json()["deduplicated"] is False
    assert dict(pipeline) == {
        "stage": "video",
        "video_status": "pending",
        "render_attempts": 0,
        "last_error": None,
        "video_path": str(video),
    }
    assert video.read_bytes() == b"current-video"


def test_generate_patch_route_requeue_keeps_existing_live_job(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db")); monkeypatch.setattr(settings, "data_root", str(tmp_path)); monkeypatch.setattr(settings, "enable_worker", False)
    audio = tmp_path / "a.wav"; audio.write_bytes(b"a")
    image = tmp_path / "i.jpg"; image.write_bytes(b"i")
    monkeypatch.setattr(patches.image_overlay, "ensure_patch_overlay", lambda *a, **k: str(image))
    with TestClient(app) as client:
        _seed(client.app.state.conn, audio)
        first = client.post("/books/1/patches/1/generate-video?ajax=1")
        second = client.post(
            "/books/1/patches/1/generate-video?ajax=1",
            data={"requeue": "true"},
        )

    assert second.status_code == 202
    assert second.json() == {"status": "queued", "job_id": first.json()["job_id"], "deduplicated": True}


def test_generate_patch_route_requeue_waits_for_active_youtube_postprocessing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db")); monkeypatch.setattr(settings, "data_root", str(tmp_path)); monkeypatch.setattr(settings, "enable_worker", False)
    audio = tmp_path / "a.wav"; audio.write_bytes(b"a")
    image = tmp_path / "i.jpg"; image.write_bytes(b"i")
    now = datetime.now(timezone.utc).isoformat()
    with TestClient(app) as client:
        conn = client.app.state.conn
        _seed(conn, audio)
        conn.execute("UPDATE patch SET image_path=? WHERE id=1", (str(image),))
        conn.execute(
            """INSERT INTO patch_pipeline
                   (patch_id,stage,thumbnail_status,video_status,upload_status,playlist_status,
                    config_snapshot,media_snapshot,created_at,updated_at)
                 VALUES (1,'playlist','done','done','done','pending','{}','{}',?,?)""",
            (now, now),
        )
        conn.commit()
        response = client.post(
            "/books/1/patches/1/generate-video?ajax=1",
            data={"requeue": "true"},
        )

    assert response.status_code == 409
    assert "đang upload YouTube" in response.json()["detail"]


def test_publish_stage_publishes_only_validated_temp(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db")); db.init_schema(conn)
    audio = tmp_path / "a.wav"; audio.write_bytes(b"a"); thumb = tmp_path / "t.png"; thumb.write_bytes(b"t"); final = tmp_path / "v.mp4"; final.write_bytes(b"old")
    _seed(conn, audio)
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *a, **k: str(thumb))
    enqueue_patch_publish(conn, 1)
    conn.execute("UPDATE patch_pipeline SET thumbnail_status='done',thumbnail_path=?,video_status='pending',video_path=? WHERE patch_id=1", (str(thumb), str(final))); conn.commit(); seen = {}
    monkeypatch.setattr("app.patch_publishing.video_gen.generate_segment", lambda *a, **k: (seen.update(render=Path(a[2])), Path(a[2]).write_bytes(b"new")))
    monkeypatch.setattr("app.patch_publishing.validate_video", lambda p: seen.update(validated=Path(p)) or VALID, raising=False)
    run_patch_publish_stage(conn, 1)
    assert seen["render"] == seen["validated"] != final
    assert final.read_bytes() == b"new"
