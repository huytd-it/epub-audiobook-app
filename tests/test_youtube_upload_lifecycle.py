import json
import os
import threading
import sqlite3
import time
from pathlib import Path

import pytest

from app import db, youtube




def test_set_thumbnail_compresses_images_over_youtube_limit(tmp_path, monkeypatch):
    from PIL import Image

    source = tmp_path / "large.png"
    Image.frombytes("RGB", (1200, 1200), os.urandom(1200 * 1200 * 3)).save(source)
    uploaded = {}

    class Service:
        def thumbnails(self): return self
        def set(self, **kwargs): return self
        def execute(self): return {}

    def media_io(fd, mimetype):
        uploaded["size"] = len(fd.read())
        uploaded["mimetype"] = mimetype
        return object()

    def media_file(path, mimetype):
        raise AssertionError("compressed temp file must not go through MediaFileUpload")

    monkeypatch.setattr(youtube, "get_youtube_service", lambda conn: Service())
    monkeypatch.setattr(youtube, "MediaIoBaseUpload", media_io)
    monkeypatch.setattr(youtube, "MediaFileUpload", media_file)

    youtube.set_thumbnail(None, "video", str(source))

    assert source.stat().st_size > 2 * 1024 * 1024
    assert uploaded["size"] <= 2 * 1024 * 1024
    assert uploaded["mimetype"] == "image/jpeg"


def test_set_thumbnail_temp_file_deletable_despite_open_media_handle(tmp_path, monkeypatch):
    # googleapiclient's MediaFileUpload opens the file in its constructor and never
    # closes it; on Windows the finally-block unlink of the compressed temp jpg then
    # fails with WinError 32. set_thumbnail must not hand the temp file to it.
    from PIL import Image

    source = tmp_path / "large.png"
    Image.frombytes("RGB", (1200, 1200), os.urandom(1200 * 1200 * 3)).save(source)

    class Service:
        def thumbnails(self): return self
        def set(self, **kwargs): return self
        def execute(self): return {}

    held = []

    class HoldingMediaFileUpload:
        def __init__(self, path, mimetype):
            held.append(open(path, "rb"))

    monkeypatch.setattr(youtube, "get_youtube_service", lambda conn: Service())
    monkeypatch.setattr(youtube, "MediaFileUpload", HoldingMediaFileUpload)

    try:
        youtube.set_thumbnail(None, "video", str(source))
    finally:
        for handle in held:
            handle.close()




@pytest.fixture
def db_conn(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    db.init_schema(conn)
    return conn


class FakeYouTubeService:
    def videos(self):
        return self

    def insert(self, *args, **kwargs):
        return self

    def next_chunk(self):
        return None, {"id": "youtube_video_id"}


@pytest.fixture(autouse=True)
def fake_service(monkeypatch):
    monkeypatch.setattr(
        "app.youtube.get_youtube_service",
        lambda conn: FakeYouTubeService(),
    )


def test_pending_upload_updates_same_row(db_conn, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    upload_id = youtube.enqueue_upload(db_conn, str(video), "Title")
    result = youtube.process_upload(db_conn, upload_id)
    rows = db_conn.execute("SELECT * FROM youtube_uploads").fetchall()
    assert len(rows) == 1
    assert rows[0]["id"] == upload_id
    assert rows[0]["youtube_video_id"] == result["youtube_video_id"]


def test_playlist_stale_claim_is_recovered(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db_conn.commit()
    db_conn.execute("INSERT INTO youtube_playlist_map (book_id, channel_id, playlist_id, mode, created_at, updated_at) VALUES (?, 'c', '__creating__', 'auto-create', '2020', '2020')", (book_id,))
    db_conn.commit()
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [])
    monkeypatch.setattr(youtube, "create_playlist", lambda *args: {"id": "new"})
    assert youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}) == "new"


def test_stale_claim_reuses_remote_marker_without_creating(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db_conn.commit()
    db_conn.execute("INSERT INTO youtube_playlist_map (book_id, channel_id, playlist_id, mode, created_at, updated_at) VALUES (?, 'c', '__creating__', 'auto-create', '2020', '2020')", (book_id,))
    db_conn.commit()
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [{"id": "remote", "snippet": {"description": f"old\n[epub-audiobook-app book:{book_id}]"}}])
    monkeypatch.setattr(youtube, "create_playlist", lambda *args: pytest.fail("duplicate playlist creation"))
    assert youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}) == "remote"


def test_marker_must_be_standalone_line(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db_conn.commit()
    db_conn.execute("INSERT INTO youtube_playlist_map (book_id, channel_id, playlist_id, mode, created_at, updated_at) VALUES (?, 'c', '__creating__', 'auto-create', '2020', '2020')", (book_id,)); db_conn.commit()
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [{"id": "embedded", "snippet": {"description": f"not-{youtube._playlist_marker(book_id)}-text"}}])
    monkeypatch.setattr(youtube, "create_playlist", lambda *args: {"id": "new"})
    assert youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}) == "new"
    db_conn.execute("UPDATE youtube_playlist_map SET playlist_id='__creating__', updated_at='2020' WHERE book_id=?", (book_id,)); db_conn.commit()
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [{"id": "standalone", "snippet": {"description": f"old\r\n{youtube._playlist_marker(book_id)}\r\n"}}])
    monkeypatch.setattr(youtube, "create_playlist", lambda *args: pytest.fail("duplicate playlist creation"))
    assert youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}) == "standalone"


def test_stale_claim_finds_marker_on_second_playlist_page(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db_conn.execute("INSERT INTO youtube_playlist_map (book_id, channel_id, playlist_id, mode, created_at, updated_at) VALUES (?, 'c', '__creating__', 'auto-create', '2020', '2020')", (book_id,)); db_conn.commit()
    class Service:
        def playlists(self): return self
        def list(self, **kwargs): self.kwargs = kwargs; return self
        def execute(self):
            if getattr(self, "kwargs", {}).get("pageToken"):
                return {"items": [{"id": "page2", "snippet": {"description": youtube._playlist_marker(book_id)}}]}
            return {"items": [], "nextPageToken": "next"}
    monkeypatch.setattr(youtube, "get_youtube_service", lambda conn: Service())
    monkeypatch.setattr(youtube, "create_playlist", lambda *args: pytest.fail("duplicate playlist creation"))
    assert youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}) == "page2"


def test_invalid_metadata_cleanup_keeps_replacement_owner(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]; db_conn.commit()
    db_conn.execute("INSERT INTO youtube_playlist_map (book_id, channel_id, playlist_id, mode, created_at, updated_at) VALUES (?, 'c', '__creating__', 'auto-create:owner-a', '2020', '2020')", (book_id,)); db_conn.commit()
    class Template(str):
        def format(self, **values):
            db_conn.execute("UPDATE youtube_playlist_map SET mode='auto-create:owner-b' WHERE book_id=?", (book_id,)); db_conn.commit()
            return ""
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [])
    with pytest.raises(ValueError):
        youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book", "_playlist_title_template": Template("x"), "_playlist_description_template": ""})
    assert db_conn.execute("SELECT mode FROM youtube_playlist_map WHERE book_id=?", (book_id,)).fetchone()[0] == "auto-create:owner-b"


def test_marker_adoption_does_not_overwrite_changed_claim(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]; db_conn.commit()
    db_conn.execute("INSERT INTO youtube_playlist_map (book_id, channel_id, playlist_id, mode, created_at, updated_at) VALUES (?, 'c', '__creating__', 'auto-create:owner-a', '2020', '2020')", (book_id,)); db_conn.commit()
    def listing(conn):
        conn.execute("UPDATE youtube_playlist_map SET mode='auto-create:owner-b', playlist_id='winner' WHERE book_id=?", (book_id,)); conn.commit()
        return [{"id": "remote", "snippet": {"description": youtube._playlist_marker(book_id)}}]
    monkeypatch.setattr(youtube, "list_playlists", listing)
    assert youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}) == "winner"
    assert db_conn.execute("SELECT mode, playlist_id FROM youtube_playlist_map WHERE book_id=?", (book_id,)).fetchone()[0] == "auto-create:owner-b"


def test_active_playlist_lease_is_not_reclaimed(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]; db_conn.commit()
    db_conn.execute("INSERT INTO youtube_playlist_map (book_id, channel_id, playlist_id, mode, created_at, updated_at) VALUES (?, 'c', '__creating__', 'auto-create:owner', '2020', datetime('now'))", (book_id,)); db_conn.commit()
    monkeypatch.setattr(youtube, "create_playlist", lambda *args: pytest.fail("active claim reclaimed"))
    with pytest.raises(RuntimeError, match="timed out"):
        youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"})


def test_heartbeat_keeps_long_create_claim_alive(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]; db_conn.commit()
    monkeypatch.setattr(youtube, "PLAYLIST_LEASE_SECONDS", 0.2)
    monkeypatch.setattr(youtube, "PLAYLIST_HEARTBEAT_SECONDS", 0.01)
    started = threading.Event(); release = threading.Event(); calls = []
    def create(conn, *args):
        started.set(); release.wait(1); return {"id": "created"}
    monkeypatch.setattr(youtube, "create_playlist", create)
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [])
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [])
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [])
    first = threading.Thread(target=lambda: youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}))
    first.start(); started.wait(1)
    monkeypatch.setattr(youtube, "create_playlist", lambda *args: calls.append(True) or {"id": "duplicate"})
    second = threading.Thread(target=lambda: youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}))
    second.start(); second.join(0.1)
    assert calls == []
    release.set(); first.join(1); second.join(1)
    assert calls == []


def test_stale_claim_without_heartbeat_still_recovers(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]; db_conn.commit()
    db_conn.execute("INSERT INTO youtube_playlist_map (book_id, channel_id, playlist_id, mode, created_at, updated_at) VALUES (?, 'c', '__creating__', 'auto-create:crash', '2020', '2020-01-01T00:00:00+00:00')", (book_id,)); db_conn.commit()
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [{"id": "recovered", "snippet": {"description": youtube._playlist_marker(book_id)}}])
    assert youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}) == "recovered"


def test_stale_marker_scan_cleans_up_heartbeat(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]; db_conn.commit()
    db_conn.execute("INSERT INTO youtube_playlist_map (book_id, channel_id, playlist_id, mode, created_at, updated_at) VALUES (?, 'c', '__creating__', 'auto-create:crash', '2020', '2020-01-01T00:00:00+00:00')", (book_id,)); db_conn.commit()
    events = []
    class Heartbeat:
        def stop(self): events.append("stop")
        def join(self): events.append("join")
        def close(self): events.append("close")
    monkeypatch.setattr(youtube, "_start_playlist_heartbeat", lambda *args: Heartbeat())
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [{"id": "recovered", "snippet": {"description": youtube._playlist_marker(book_id)}}])
    assert youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}) == "recovered"
    assert events == ["stop", "join", "close"]


def test_memory_heartbeat_does_not_open_connection_or_thread(monkeypatch):
    class Conn:
        def execute(self, sql):
            return type("Result", (), {"fetchone": lambda self: (0, "main", ":memory:")})()
    conn = Conn()
    monkeypatch.setattr(youtube.db, "connect", lambda *args: pytest.fail("secondary connection"))
    heartbeat = youtube._start_playlist_heartbeat(conn, 1, "c", "owner")
    assert heartbeat._thread is None and heartbeat._conn is None
    heartbeat.stop(); heartbeat.join(); heartbeat.close()


def test_failed_heartbeat_invalidates_create_claim(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]; db_conn.commit()
    class BrokenHeartbeat:
        error = sqlite3.OperationalError("heartbeat failed")
        def stop(self): pass
        def join(self): pass
        def close(self): pass
    monkeypatch.setattr(youtube, "_start_playlist_heartbeat", lambda *args: BrokenHeartbeat())
    monkeypatch.setattr(youtube, "create_playlist", lambda *args: {"id": "created"})
    with pytest.raises(RuntimeError, match="heartbeat"):
        youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"})
    assert db_conn.execute("SELECT playlist_id FROM youtube_playlist_map WHERE book_id=?", (book_id,)).fetchone()[0] == "__creating__"


def test_memory_resolver_guard_serializes_live_create(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]; db_conn.commit()
    started = threading.Event(); release = threading.Event(); calls = []
    def create(conn, *args):
        calls.append(True); started.set(); release.wait(1); return {"id": "created"}
    monkeypatch.setattr(youtube, "create_playlist", create)
    monkeypatch.setattr(youtube, "list_playlists", lambda conn: [])
    first = threading.Thread(target=lambda: youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}))
    first.start(); started.wait(1)
    second = threading.Thread(target=lambda: youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}))
    second.start(); time.sleep(0.05)
    assert len(calls) == 1
    release.set(); first.join(1); second.join(1)


def test_creator_losing_lease_does_not_overwrite_winner(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]; db_conn.commit()
    def create(conn, *args):
        conn.execute("UPDATE youtube_playlist_map SET playlist_id='winner', mode='auto-create:winner' WHERE book_id=?", (book_id,)); conn.commit()
        return {"id": "loser"}
    monkeypatch.setattr(youtube, "create_playlist", create)
    assert youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"}) == "winner"


def test_losing_creator_failure_does_not_delete_winner_claim(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]; db_conn.commit()
    def create(conn, *args):
        conn.execute("UPDATE youtube_playlist_map SET mode='auto-create:owner-b', updated_at=? WHERE book_id=?", (youtube._now_iso(), book_id)); conn.commit()
        raise RuntimeError("api")
    monkeypatch.setattr(youtube, "create_playlist", create)
    with pytest.raises(RuntimeError):
        youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"})
    row = db_conn.execute("SELECT playlist_id, mode FROM youtube_playlist_map WHERE book_id=?", (book_id,)).fetchone()
    assert row["playlist_id"] == "__creating__" and row["mode"] == "auto-create:owner-b"


def test_postprocess_uses_frozen_playlist_template_values(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Current', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db_conn.execute("INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, name, created_at, updated_at) VALUES (?, 0, 99, 100, 'Current Patch', '2020', '2020')", (book_id,))
    patch_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    frozen = {"book_title": "Old", "episode_number": 2, "chapter_start": 3, "chapter_end": 4, "patch_name": "Old Patch", "genre_tags": "mystery,classic"}
    config = {"privacy_status": "private", "automation": {"youtube": {"playlist_mode": "auto-create", "title_template": "{book_title} {episode_number} {patch_name} {genre_tags}", "description_template": "{chapter_start}-{chapter_end}"}}, "playlist_template_values": frozen}
    db_conn.execute("INSERT INTO patch_pipeline (patch_id, config_snapshot, media_snapshot, created_at, updated_at) VALUES (?, ?, '{}', '2020', '2020')", (patch_id, json.dumps(config)))
    db_conn.execute("INSERT INTO youtube_uploads (video_path, title, status, youtube_video_id, thumbnail_status, playlist_status, metadata_snapshot, created_at) VALUES ('v', 'v', 'done', 'yt', 'done', 'pending', ?, '2020')", (json.dumps({"automation": config["automation"], "privacy_status": "private"}),))
    upload_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db_conn.execute("UPDATE patch_pipeline SET youtube_upload_id=? WHERE patch_id=?", (upload_id, patch_id)); db_conn.commit()
    seen = []
    monkeypatch.setattr(youtube, "get_creds_from_db", lambda conn: {"channel_id": "c"})
    monkeypatch.setattr(youtube, "create_playlist", lambda conn, title, desc, privacy: seen.append((title, desc)) or {"id": "p"})
    monkeypatch.setattr(youtube, "playlist_contains_video", lambda *args: True)
    db_conn.execute("UPDATE book SET title='Mutated' WHERE id=?", (book_id,)); db_conn.execute("UPDATE patch SET chapter_start=8, chapter_end=9, name='Mutated Patch' WHERE id=?", (patch_id,)); db_conn.commit()
    youtube.postprocess_upload(db_conn, upload_id)
    assert seen == [("Old 2 Old Patch mystery,classic", "3-4\n[epub-audiobook-app book:%s]" % book_id)]


def test_playlist_description_renders_all_fields_and_marker(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db_conn.commit()
    seen = []
    monkeypatch.setattr(youtube, "create_playlist", lambda conn, title, desc, privacy: seen.append((title, desc)) or {"id": "p"})
    youtube.resolve_book_playlist(db_conn, book_id, "c", {
        "book_title": "Book", "episode_number": 3, "chapter_start": 10, "chapter_end": 12,
        "patch_name": "Intro", "genre_tags": "fantasy", "_playlist_title_template": "{book_title} ep {episode_number}: {patch_name} [{genre_tags}]",
        "_playlist_description_template": "chapters {chapter_start}-{chapter_end}",
    })
    assert seen == [("Book ep 3: Intro [fantasy]", "chapters 10-12\n[epub-audiobook-app book:%s]" % book_id)]


def test_playlist_create_failure_releases_claim(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db_conn.commit()
    monkeypatch.setattr(youtube, "create_playlist", lambda *args: (_ for _ in ()).throw(RuntimeError("api")))
    with pytest.raises(RuntimeError):
        youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Book"})
    assert db_conn.execute("SELECT COUNT(*) FROM youtube_playlist_map WHERE book_id=?", (book_id,)).fetchone()[0] == 0


def test_create_playlist_uses_book_and_episode_template_values(db_conn, monkeypatch):
    db_conn.execute("INSERT INTO book (title, original_filename, epub_path, created_at, updated_at) VALUES ('Real Book', 'b', 'b', '2020', '2020')")
    book_id = db_conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    db_conn.commit()
    seen = []
    monkeypatch.setattr(youtube, "create_playlist", lambda conn, title, desc, privacy: seen.append((title, desc)) or {"id": "p"})
    youtube.resolve_book_playlist(db_conn, book_id, "c", {"book_title": "Real Book", "episode_number": 3, "patch_name": "Intro", "_playlist_title_template": "{book_title} {episode_number} {patch_name}", "_playlist_description_template": "{book_title} {episode_number}"})
    assert seen == [("Real Book 3 Intro", "Real Book 3\n[epub-audiobook-app book:%s]" % book_id)]


def test_quota_403_is_failed(db_conn, monkeypatch, tmp_path):
    class Error(Exception):
        resp = type("Resp", (), {"status": 403})()
        content = b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'
    monkeypatch.setattr(youtube, "set_thumbnail", lambda *args: (_ for _ in ()).throw(Error("quota")))
    thumb = tmp_path / "thumb.png"; thumb.write_bytes(b"x")
    upload_id = youtube.enqueue_upload(db_conn, "/tmp/missing.mp4", "Title")
    db_conn.execute("UPDATE youtube_uploads SET status='done', youtube_video_id='v', thumbnail_status='pending', metadata_snapshot=? WHERE id=?", (json.dumps({"background_fallback": str(thumb)}), upload_id)); db_conn.commit()
    assert youtube.postprocess_upload(db_conn, upload_id)["status"] == "failed"
