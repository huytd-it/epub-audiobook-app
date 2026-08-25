import json
from pathlib import Path
import pytest
import soundfile as sf

from app import db
from app import youtube
from app.patch_publishing import enqueue_patch_publish, retry_patch_publish, sync_pipeline_from_upload


def _seed(conn):
    now = "2026-01-01T00:00:00"
    conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("Book", "book.epub", "/tmp/book.epub", now, now),
    )
    book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, audio_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (book_id, 0, 1, 2, "/tmp/audio.wav", now, now),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_enqueue_snapshots_metadata_and_is_idempotent(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "test.db"))
    db.init_schema(conn)
    patch_id = _seed(conn)
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *args, **kwargs: "/tmp/thumb.png")

    first = enqueue_patch_publish(conn, patch_id)
    second = enqueue_patch_publish(conn, patch_id)

    assert first["patch_id"] == patch_id
    assert json.loads(first["config_snapshot"])["title"].startswith("Book - Tập 1")
    assert json.loads(first["config_snapshot"])["automation"]["youtube"]["mode"] == "none"
    assert first["id"] == second["id"]
    assert conn.execute("SELECT COUNT(*) FROM patch_pipeline").fetchone()[0] == 1


def test_enqueue_freezes_complete_patch_render_configuration(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "snapshot.db")); db.init_schema(conn); patch_id = _seed(conn)
    thumb = tmp_path / "thumb.png"; thumb.write_bytes(b"x")
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *a, **k: str(thumb))
    row = enqueue_patch_publish(conn, patch_id)
    config = json.loads(row["media_snapshot"])["render_config"]
    assert config["resolution"] == "1920x1080"
    assert config["fps"] == 30
    assert config["codec"] == "libx264"
    assert config["crf"] == 23
    assert config["audio_bitrate"] == "320k"
    assert {"music_path", "music_volume", "intro_audio", "outro_audio"} <= config.keys()
    # Gap music is frozen with everything else, so changing the setting later
    # cannot alter a patch already queued for render.
    assert config["music_gap_only"] is True
    assert config["music_gap_min_ms"] == 1500
    assert config["music_gap_fade_ms"] == 400


def test_enqueue_snapshot_contains_timeline_once(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    audio = tmp_path / "audio.wav"
    sf.write(audio, [0.0] * 300, 10)
    Path(audio.with_suffix(".timeline.json")).write_text(json.dumps({
        "version": 1, "sample_rate": 10, "total_frames": 300,
        "chapters": [
                    {"chapter_index": 1, "start_frame": 0, "start_seconds": 0, "title": "Intro"},
                    {"chapter_index": 2, "start_frame": 100, "start_seconds": 10, "title": "One"},
                    {"chapter_index": 3, "start_frame": 200, "start_seconds": 20, "title": "Two"},
        ],
    }))
    patch_id = _seed(conn)
    conn.execute("UPDATE patch SET audio_path=? WHERE id=?", (str(audio), patch_id)); conn.commit()
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *args, **kwargs: "/tmp/thumb.png")
    row = enqueue_patch_publish(conn, patch_id)
    description = json.loads(row["config_snapshot"])["description"]
    assert description.count("00:00 Intro") == 1
    assert "00:00 Intro\n00:10 One\n00:20 Two" in description
    assert json.loads(enqueue_patch_publish(conn, patch_id)["config_snapshot"])["description"].count("00:00 Intro") == 1


def test_enqueue_snapshot_lists_chapter_names_and_music_credit(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    patch_id = _seed(conn)
    book_id = conn.execute("SELECT book_id FROM patch WHERE id=?", (patch_id,)).fetchone()[0]
    for index, title in ((1, "Chương 1: Mưa"), (2, "Chương 2: Nắng")):
        conn.execute(
            "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, ?, ?, ?, ?)",
            (book_id, index, title, "noi dung", 8),
        )
    conn.execute(
        "INSERT INTO music (name, file_path, duration_sec, created_at, description, license) VALUES (?, ?, ?, ?, ?, ?)",
        ("Incredulity", "/tmp/music.mp3", 12.0, "2026-01-01T00:00:00", "", "CC BY 4.0 - Scott Buckley"),
    )
    conn.execute("UPDATE book SET music_id = (SELECT id FROM music) WHERE id = ?", (book_id,))
    conn.commit()
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *args, **kwargs: "/tmp/thumb.png")

    description = json.loads(enqueue_patch_publish(conn, patch_id)["config_snapshot"])["description"]

    assert "Chương 1: Mưa" in description
    assert "Chương 2: Nắng" in description
    assert "Incredulity" in description
    assert "CC BY 4.0 - Scott Buckley" in description


def test_upload_creation_re_resolves_stale_snapshot(tmp_path, monkeypatch):
    """A pipeline enqueued before a metadata fix must not upload the stale title."""
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    patch_id = _seed(conn)
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *args, **kwargs: "/tmp/thumb.png")
    enqueue_patch_publish(conn, patch_id)
    conn.execute("UPDATE patch_pipeline SET video_status='done', video_path='/tmp/video.mp4' WHERE patch_id=?", (patch_id,))
    stale ={"title": "Book - Tap 1 - Chuong 0-9: Old", "description": "", "tags": [],
             "privacy_status": "private", "automation": {"youtube": {"mode": "none"}},
             "playlist_template_values": {}}
    conn.execute("UPDATE patch_pipeline SET config_snapshot=? WHERE patch_id=?",
                 (json.dumps(stale), patch_id))
    conn.commit()
    from app.patch_publishing import _create_upload_atomically
    row = dict(conn.execute("SELECT * FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone())
    upload_id = _create_upload_atomically(conn, row)
    upload = conn.execute("SELECT title, metadata_snapshot FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert upload["title"].startswith("Book - Tập 1")
    assert json.loads(upload["metadata_snapshot"])["title"].startswith("Book - Tập 1")
    refreshed = conn.execute("SELECT config_snapshot FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone()
    assert json.loads(refreshed["config_snapshot"])["title"].startswith("Book - Tập 1")


def test_thumbnail_path_must_exist_before_enqueue_is_complete(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    patch_id = _seed(conn)
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *args, **kwargs: "/missing/thumb.png")
    assert enqueue_patch_publish(conn, patch_id)["thumbnail_status"] == "pending"


def test_publish_thumbnail_uses_default_font(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    patch_id = _seed(conn)
    seen = []
    monkeypatch.setattr("app.config.settings.default_font_path", "default.ttf")
    monkeypatch.setattr(
        "app.patch_publishing.ensure_patch_overlay",
        lambda book, patch, font, **kw: seen.append(font) or "/missing/thumb.png",
    )

    enqueue_patch_publish(conn, patch_id)

    assert seen == ["default.ttf"]


def test_migration_enforces_unique_book_channel_playlist_map(tmp_path):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    indexes = conn.execute("PRAGMA index_list(youtube_playlist_map)").fetchall()
    assert any(row[1] == "idx_youtube_playlist_map_book_channel" for row in indexes)


def test_batch_publish_keeps_thumbnail_bound_to_patch_id(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    book_id = _seed(conn)
    now = "2026-01-01T00:00:00"
    conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, audio_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (book_id, 1, 3, 4, "/tmp/audio-2.wav", now, now),
    )
    patch_a = conn.execute("SELECT id FROM patch WHERE patch_index=0").fetchone()[0]
    patch_b = conn.execute("SELECT id FROM patch WHERE patch_index=1").fetchone()[0]
    conn.commit()
    monkeypatch.setattr(
        "app.patch_publishing.ensure_patch_overlay",
        lambda book, patch, _font, **kw: f"/tmp/thumb-{patch.id}.png",
    )

    a = enqueue_patch_publish(conn, patch_a)
    b = enqueue_patch_publish(conn, patch_b)

    assert json.loads(a["media_snapshot"])["patch_id"] == patch_a
    assert json.loads(b["media_snapshot"])["patch_id"] == patch_b
    assert a["thumbnail_path"] != b["thumbnail_path"]


def test_retry_done_upload_postprocesses_without_reenqueue(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    patch_id = _seed(conn)
    thumb = tmp_path / "thumb.png"; thumb.write_bytes(b"x")
    video = tmp_path / "v.mp4"; video.write_bytes(b"x")
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *args, **kwargs: str(thumb))
    row = enqueue_patch_publish(conn, patch_id)
    conn.execute("UPDATE patch_pipeline SET video_status='done', video_path=? WHERE patch_id=?", (str(video), patch_id))
    upload_id = youtube.enqueue_upload(conn, str(video), "Title")
    conn.execute("UPDATE youtube_uploads SET status='done', youtube_video_id='yt1', thumbnail_status='done', playlist_status='done' WHERE id=?", (upload_id,))
    conn.execute("UPDATE patch_pipeline SET upload_status='done', youtube_upload_id=? WHERE patch_id=?", (upload_id, patch_id)); conn.commit()
    enqueue = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("re-enqueued"))
    monkeypatch.setattr(youtube, "enqueue_upload", enqueue)
    published = lambda conn, upload_id: {"status": "published", "youtube_video_id": "yt1"}
    monkeypatch.setattr(youtube, "publish_completed_upload", published)
    result = retry_patch_publish(conn, patch_id)
    assert result["stage"] == "published"
    assert result["youtube_upload_id"] == upload_id


def test_retry_failed_upload_requeues_same_upload(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    patch_id = _seed(conn)
    thumb = tmp_path / "thumb.png"; thumb.write_bytes(b"x")
    video = tmp_path / "v.mp4"; video.write_bytes(b"x")
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *args, **kwargs: str(thumb))
    enqueue_patch_publish(conn, patch_id)
    conn.execute("UPDATE patch_pipeline SET video_status='done', video_path=? WHERE patch_id=?", (str(video), patch_id)); conn.commit()
    retry_patch_publish(conn, patch_id)
    upload_id = conn.execute("SELECT youtube_upload_id FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone()[0]
    conn.execute("UPDATE youtube_uploads SET status='failed' WHERE id=?", (upload_id,)); conn.commit()
    retry_patch_publish(conn, patch_id)
    assert conn.execute("SELECT COUNT(*) FROM youtube_uploads").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()[0] == "pending"


def test_force_new_preserves_media_and_clears_active_upload(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    patch_id = _seed(conn)
    thumb = tmp_path / "thumb.png"; thumb.write_bytes(b"x")
    video = tmp_path / "v.mp4"; video.write_bytes(b"x")
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *args, **kwargs: str(thumb))
    old = enqueue_patch_publish(conn, patch_id)
    upload_id = youtube.enqueue_upload(conn, str(video), "Title")
    conn.execute("UPDATE patch_pipeline SET video_status='done', video_path=?, upload_status='done', playlist_status='done', youtube_upload_id=? WHERE patch_id=?", (str(video), upload_id, patch_id)); conn.commit()
    fresh = enqueue_patch_publish(conn, patch_id, force_new=True)
    assert fresh["thumbnail_status"] == "done"
    assert fresh["video_status"] == "done"
    assert fresh["upload_status"] == "pending"
    assert fresh["playlist_status"] == "pending"
    assert fresh["youtube_upload_id"] is None
    assert fresh["video_path"] == str(video)


def test_force_new_does_not_reuse_done_thumbnail_when_new_path_missing(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    patch_id = _seed(conn)
    old = tmp_path / "old.png"; old.write_bytes(b"x")
    enqueue_patch_publish(conn, patch_id)
    conn.execute("UPDATE patch_pipeline SET thumbnail_status='done', thumbnail_path=? WHERE patch_id=?", (str(old), patch_id)); conn.commit()
    monkeypatch.setattr("app.patch_publishing.ensure_patch_overlay", lambda *args, **kwargs: "/missing/new.png")
    assert enqueue_patch_publish(conn, patch_id, force_new=True)["thumbnail_status"] == "pending"


def test_sync_pipeline_from_upload_copies_postprocess_status(tmp_path):
    conn = db.connect(str(tmp_path / "test.db")); db.init_schema(conn)
    patch_id = _seed(conn)
    now = "2026-01-01T00:00:00"
    conn.execute("INSERT INTO patch_pipeline (patch_id, config_snapshot, media_snapshot, created_at, updated_at) VALUES (?, '{}', '{}', ?, ?)", (patch_id, now, now))
    upload_id = youtube.enqueue_upload(conn, "/tmp/v.mp4", "Title")
    conn.execute("UPDATE youtube_uploads SET status='done', youtube_video_id='yt1', thumbnail_status='done', playlist_status='done' WHERE id=?", (upload_id,))
    conn.execute("UPDATE patch_pipeline SET youtube_upload_id=? WHERE patch_id=?", (upload_id, patch_id)); conn.commit()
    row = sync_pipeline_from_upload(conn, upload_id)
    assert row["stage"] == "published"
    assert row["thumbnail_status"] == "done"
    assert row["playlist_status"] == "done"
