import json
import pytest
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.handlers import patch_video
from app.jobqueue.joblog import JobLogger
from app.main import app
from app.routes import patches
from app.video_integrity import ValidationFacts, ValidationResult


@pytest.fixture(autouse=True)
def _valid_generated_video(monkeypatch):
    valid = lambda p, **kw: ValidationResult(True, None, "", (), ValidationFacts(), 0)
    monkeypatch.setattr(patches, "validate_video", valid)
    monkeypatch.setattr(patch_video, "validate_video", valid)


def _run_queued_patch_video(conn, response):
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    job = store.claim(conn, "patch_video", "test")
    return patch_video.handle(JobContext(job, conn, JobLogger(job_id, "patch_video"), lambda: False))


def _seed_book_and_patch(conn, book_id=7, patch_id=11, audio_path="audio.wav"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status, created_at, updated_at)
           VALUES (?, 'Book', 'book.epub', 'book.epub', 10, 'done', ?, ?)""",
        (book_id, now, now),
    )
    conn.execute(
        """INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status,
                                audio_path, image_path, created_at, updated_at)
           VALUES (?, ?, 0, 1, 1, 'done', ?, 'image.jpg', ?, ?)""",
        (patch_id, book_id, audio_path, now, now),
    )
    conn.commit()


def test_upload_patch_video_saves_where_preview_reads(tmp_path, monkeypatch):
    # db_path must be redirected too: without it this test writes its videos row into the
    # real data/app.db and leaves a junk row behind on every run.
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        _seed_book_and_patch(client.app.state.conn)
        response = client.post(
            "/books/7/patches/11/video",
            files={"video": ("patch.mp4", b"video-data", "video/mp4")},
            follow_redirects=False,
        )
        library = client.get("/video/api/videos")
    assert response.status_code == 303
    assert (tmp_path / "books" / "7" / "patch_videos" / "11.mp4").read_bytes() == b"video-data"
    assert any(v["filename"] == "patch_7_11.mp4" for v in library.json()["videos"])


def test_uploaded_patch_video_is_linked_to_its_patch(tmp_path, monkeypatch):
    """A patch's MP4 must be findable from the DB, not only by globbing the disk."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        _seed_book_and_patch(conn)
        client.post(
            "/books/7/patches/11/video",
            files={"video": ("patch.mp4", b"video-data", "video/mp4")},
            follow_redirects=False,
        )
        row = conn.execute("SELECT book_id, patch_id FROM videos").fetchone()
    assert (row["book_id"], row["patch_id"]) == (7, 11)


def test_generated_patch_video_is_linked_to_its_patch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    audio = tmp_path / "narration.wav"; audio.write_bytes(b"audio")
    image = tmp_path / "background.jpg"; image.write_bytes(b"image")
    monkeypatch.setattr(patch_video.video_gen, "generate_segment",
                        lambda *args, **kwargs: Path(args[2]).write_bytes(b"video"))
    monkeypatch.setattr(patch_video.image_overlay, "ensure_patch_overlay",
                        lambda *args, **kwargs: str(image))
    with TestClient(app) as client:
        conn = client.app.state.conn
        _seed_book_and_patch(conn, book_id=1, patch_id=1, audio_path=str(audio))
        response = client.post("/books/1/patches/1/generate-video?ajax=1")
        _run_queued_patch_video(conn, response)
        row = conn.execute("SELECT book_id, patch_id FROM videos").fetchone()
    assert response.status_code == 202
    assert (row["book_id"], row["patch_id"]) == (1, 1)


def test_registering_then_publishing_keeps_one_row_per_patch(tmp_path, monkeypatch):
    """Two writers, one MP4: the route and the publish stage must not both insert.

    The UNIQUE index is on patch_id, so a row left with patch_id NULL cannot be deduped
    against and the same file ends up in the library twice.
    """
    from app.video_repository import upsert_patch_video

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        _seed_book_and_patch(conn)
        client.post(
            "/books/7/patches/11/video",
            files={"video": ("patch.mp4", b"video-data", "video/mp4")},
            follow_redirects=False,
        )
        video_path = tmp_path / "books" / "7" / "patch_videos" / "11.mp4"
        upsert_patch_video(conn, book_id=7, patch_id=11,
                           file_path=str(video_path), resolution="1920x1080")
        rows = conn.execute("SELECT id, patch_id FROM videos").fetchall()
    assert len(rows) == 1, f"duplicate library rows for one MP4: {[dict(r) for r in rows]}"
    assert rows[0]["patch_id"] == 11


def test_upload_patch_audio_marks_patch_done(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    now = datetime.now(timezone.utc).isoformat()
    with TestClient(app) as client:
        conn = client.app.state.conn
        conn.execute(
            """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status, created_at, updated_at)
               VALUES (1, 'Book', 'book.epub', 'book.epub', 10, 'ready', ?, ?)""",
            (now, now),
        )
        conn.execute(
            """INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at)
               VALUES (1, 1, 0, 1, 1, 'pending', ?, ?)""",
            (now, now),
        )
        conn.commit()
        response = client.post(
            "/books/1/patches/1/upload-audio",
            files={"audio": ("result.wav", b"audio-data", "audio/wav")},
        )
        row = conn.execute("SELECT status, audio_path FROM patch WHERE id = 1").fetchone()

    assert response.status_code == 200
    assert row["status"] == "done"
    assert Path(row["audio_path"]).read_bytes() == b"audio-data"


def test_generate_patch_video_mixes_book_background_music(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    now = datetime.now(timezone.utc).isoformat()
    audio = tmp_path / "narration.wav"
    image = tmp_path / "background.jpg"
    music = tmp_path / "music.mp3"
    audio.write_bytes(b"audio")
    image.write_bytes(b"image")
    music.write_bytes(b"music")
    captured = {}

    def render(*args, **kwargs):
        captured.update(kwargs)
        Path(args[2]).write_bytes(b"video")

    monkeypatch.setattr(patch_video.video_gen, "generate_segment", render)
    monkeypatch.setattr(patch_video.image_overlay, "ensure_patch_overlay", lambda *args, **kwargs: str(image))

    with TestClient(app) as client:
        conn = client.app.state.conn
        conn.execute(
            "INSERT INTO music (id, name, file_path, created_at) VALUES (1, 'Music', ?, ?)",
            (str(music), now),
        )
        conn.execute(
            """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                                   music_id, music_volume, created_at, updated_at)
               VALUES (1, 'Book', 'book.epub', 'book.epub', 10, 'done', 1, 0.3, ?, ?)""",
            (now, now),
        )
        conn.execute(
            """INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status,
                                    audio_path, image_path, created_at, updated_at)
               VALUES (1, 1, 0, 1, 1, 'done', ?, ?, ?, ?)""",
            (str(audio), str(image), now, now),
        )
        conn.commit()
        response = client.post("/books/1/patches/1/generate-video?ajax=1")
        _run_queued_patch_video(conn, response)

    assert response.status_code == 202
    assert captured["music_path"] == str(music)
    assert captured["music_volume"] == 0.3


def test_generate_patch_video_appends_intro_and_outro(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    now = datetime.now(timezone.utc).isoformat()
    audio = tmp_path / "narration.wav"
    image = tmp_path / "background.jpg"
    audio.write_bytes(b"audio")
    image.write_bytes(b"image")
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    intro = voices_dir / "intro.mp3"
    outro = voices_dir / "outro.mp3"
    intro.write_bytes(b"intro")
    outro.write_bytes(b"outro")
    segment_calls = []
    concat_calls = []

    def render(*args, **kwargs):
        segment_calls.append((args[0], args[1], args[2]))
        Path(args[2]).write_bytes(b"video")

    def concat(segments, out_path, **kwargs):
        concat_calls.append((list(segments), out_path))
        Path(out_path).write_bytes(b"final")

    monkeypatch.setattr(patch_video.video_gen, "generate_segment", render)
    monkeypatch.setattr(patch_video.video_gen, "concat_segments", concat)
    monkeypatch.setattr(patch_video.image_overlay, "ensure_patch_overlay", lambda *args, **kwargs: str(image))

    with TestClient(app) as client:
        conn = client.app.state.conn
        conn.execute(
            """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                                   automation_config, created_at, updated_at)
               VALUES (1, 'Book', 'book.epub', 'book.epub', 10, 'done', ?, ?, ?)""",
            (json.dumps({"video": {"intro_voice": "intro.mp3", "outro_voice": "outro.mp3"}}), now, now),
        )
        conn.execute(
            """INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status,
                                    audio_path, image_path, created_at, updated_at)
               VALUES (1, 1, 0, 1, 1, 'done', ?, ?, ?, ?)""",
            (str(audio), str(image), now, now),
        )
        conn.commit()
        response = client.post("/books/1/patches/1/generate-video?ajax=1")
        _run_queued_patch_video(conn, response)

    assert response.status_code == 202
    audios = [call[1] for call in segment_calls]
    assert audios == [str(intro), str(audio), str(outro)]
    assert len(concat_calls) == 1
    segments, out_path = concat_calls[0]
    assert len(segments) == 3
    final = tmp_path / "books" / "1" / "patch_videos" / "1.mp4"
    assert Path(out_path) != final
    assert final.is_file()
    assert final.read_bytes() == b"final"


def _seed_book_with_patch_video(conn, tmp_path) -> Path:
    """Insert a book + done patch and put an MP4 where the patch video routes read."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status, created_at, updated_at)
           VALUES (1, 'Book', 'book.epub', 'book.epub', 10, 'done', ?, ?)""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status,
                                audio_path, created_at, updated_at)
           VALUES (1, 1, 0, 1, 1, 'done', 'narration.wav', ?, ?)""",
        (now, now),
    )
    video_path = tmp_path / "books" / "1" / "patch_videos" / "1.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"video")
    conn.execute(
        """INSERT INTO videos (id, filename, file_path, file_size_bytes, created_at, updated_at)
           VALUES (1, 'patch_1_1.mp4', ?, 5, ?, ?)""",
        (str(video_path), now, now),
    )
    conn.commit()
    return video_path


def test_delete_patch_video_removes_file_and_library_row(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)

    with TestClient(app) as client:
        conn = client.app.state.conn
        video_path = _seed_book_with_patch_video(conn, tmp_path)
        response = client.post("/books/1/patches/1/video/delete?ajax=1")
        remaining = conn.execute("SELECT COUNT(*) AS n FROM videos").fetchone()["n"]

    assert response.status_code == 200
    assert response.json()["status"] == "deleted"
    assert not video_path.exists()
    assert remaining == 0


def test_delete_patch_video_resets_publish_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    now = datetime.now(timezone.utc).isoformat()

    with TestClient(app) as client:
        conn = client.app.state.conn
        video_path = _seed_book_with_patch_video(conn, tmp_path)
        conn.execute(
            """INSERT INTO patch_pipeline (patch_id, stage, video_status, upload_status,
                                             video_id, video_path, config_snapshot, media_snapshot,
                                             created_at, updated_at)
               VALUES (1, 'upload', 'done', 'pending', 1, ?, '{}', '{}', ?, ?)""",
            (str(video_path), now, now),
        )
        conn.commit()
        client.post("/books/1/patches/1/video/delete?ajax=1")
        row = conn.execute("SELECT * FROM patch_pipeline WHERE patch_id = 1").fetchone()

    assert row["video_status"] == "pending"
    assert row["stage"] == "video"
    assert row["video_id"] is None
    assert row["video_path"] is None


def test_delete_patch_video_keeps_stage_when_already_uploaded(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    now = datetime.now(timezone.utc).isoformat()

    with TestClient(app) as client:
        conn = client.app.state.conn
        video_path = _seed_book_with_patch_video(conn, tmp_path)
        conn.execute(
            """INSERT INTO patch_pipeline (patch_id, stage, video_status, upload_status,
                                             video_id, video_path, config_snapshot, media_snapshot,
                                             created_at, updated_at)
               VALUES (1, 'playlist', 'done', 'done', 1, ?, '{}', '{}', ?, ?)""",
            (str(video_path), now, now),
        )
        conn.commit()
        client.post("/books/1/patches/1/video/delete?ajax=1")
        row = conn.execute("SELECT * FROM patch_pipeline WHERE patch_id = 1").fetchone()

    assert row["stage"] == "playlist"
    assert row["upload_status"] == "done"
    assert row["video_path"] is None


def test_delete_patch_video_rejects_patch_from_another_book(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)

    with TestClient(app) as client:
        conn = client.app.state.conn
        video_path = _seed_book_with_patch_video(conn, tmp_path)
        response = client.post("/books/99/patches/1/video/delete?ajax=1")

    assert response.status_code == 404
    assert video_path.exists()


def test_init_schema_backfills_patch_id_for_existing_patch_videos(tmp_path, monkeypatch):
    """Rows written before patch_id was set must be adopted, not left orphaned.

    Patch videos live at books/{book_id}/patch_videos/{patch_id}.mp4, so the link can be
    recovered from the path.
    """
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn = db.connect(str(tmp_path / "legacy.db"))
    db.init_schema(conn)
    _seed_book_and_patch(conn, book_id=7, patch_id=11)
    now = datetime.now(timezone.utc).isoformat()
    legacy_path = tmp_path / "books" / "7" / "patch_videos" / "11.mp4"
    legacy_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_path.write_bytes(b"video")
    conn.execute(
        """INSERT INTO videos (filename, file_path, file_size_bytes, created_at, updated_at)
           VALUES ('patch_7_11.mp4', ?, 5, ?, ?)""",
        (str(legacy_path), now, now),
    )
    # An unrelated library video must be left alone.
    unrelated = tmp_path / "studio" / "clip.mp4"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    conn.execute(
        """INSERT INTO videos (filename, file_path, file_size_bytes, created_at, updated_at)
           VALUES ('clip.mp4', ?, 5, ?, ?)""",
        (str(unrelated), now, now),
    )
    conn.commit()

    db.init_schema(conn)

    linked = conn.execute(
        "SELECT book_id, patch_id FROM videos WHERE file_path = ?", (str(legacy_path),)
    ).fetchone()
    assert (linked["book_id"], linked["patch_id"]) == (7, 11)
    untouched = conn.execute(
        "SELECT book_id, patch_id FROM videos WHERE file_path = ?", (str(unrelated),)
    ).fetchone()
    assert (untouched["book_id"], untouched["patch_id"]) == (None, None)
    conn.close()


def test_backfill_skips_rows_whose_patch_is_gone(tmp_path, monkeypatch):
    """The path names a patch id, but that patch may have been deleted since."""
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn = db.connect(str(tmp_path / "legacy.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    orphan = tmp_path / "books" / "7" / "patch_videos" / "999.mp4"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    conn.execute(
        """INSERT INTO videos (filename, file_path, file_size_bytes, created_at, updated_at)
           VALUES ('patch_7_999.mp4', ?, 5, ?, ?)""",
        (str(orphan), now, now),
    )
    conn.commit()

    db.init_schema(conn)   # must not raise a foreign-key error

    row = conn.execute("SELECT patch_id FROM videos WHERE file_path = ?", (str(orphan),)).fetchone()
    assert row["patch_id"] is None
    conn.close()


def test_generate_patch_video_uses_saved_shared_video_config(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    now = datetime.now(timezone.utc).isoformat()
    audio = tmp_path / "narration.wav"
    fallback = tmp_path / "fallback.jpg"
    bg1 = tmp_path / "bg1.jpg"
    bg2 = tmp_path / "bg2.jpg"
    for path in (audio, fallback, bg1, bg2):
        path.write_bytes(b"x")
    captured = {}

    def render_sequence(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        Path(args[2]).write_bytes(b"video")

    monkeypatch.setattr(patch_video.video_gen, "generate_background_sequence", render_sequence)
    monkeypatch.setattr(patch_video.video_gen, "generate_segment", lambda *args, **kwargs: Path(args[2]).write_bytes(b"video"))
    monkeypatch.setattr(patch_video.image_overlay, "ensure_patch_overlay", lambda *args, **kwargs: str(fallback))

    with TestClient(app) as client:
        conn = client.app.state.conn
        conn.execute(
            """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                                   background_image_path, automation_config, created_at, updated_at)
               VALUES (1, 'Book', 'book.epub', 'book.epub', 10, 'done', ?, ?, ?, ?)""",
            (str(fallback), json.dumps({"video": {
                "backgrounds": [str(bg1), str(bg2)],
                "background_mode": "random",
                "image_duration_seconds": 7,
                "crossfade_enabled": True,
                "crossfade_seconds": 1.5,
                "ken_burns_enabled": True,
                "progress_bar_enabled": True,
            }}), now, now),
        )
        conn.execute(
            """INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status,
                                    audio_path, created_at, updated_at)
               VALUES (1, 1, 0, 1, 1, 'done', ?, ?, ?)""",
            (str(audio), now, now),
        )
        conn.commit()
        response = client.post("/books/1/patches/1/generate-video?ajax=1")
        _run_queued_patch_video(conn, response)

    assert response.status_code == 202
    assert captured["args"][:2] == ([str(bg1), str(bg2)], str(audio))
    assert captured["image_duration"] == 7
    assert captured["mode"] == "random"
    assert captured["crossfade"] is True
    assert captured["crossfade_seconds"] == 1.5
    assert captured["ken_burns"] is True
    assert captured["progress_bar"] is True
