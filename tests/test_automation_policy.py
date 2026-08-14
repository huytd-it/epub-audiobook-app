"""Automation policy (auto_create_video / auto_upload_youtube) + reconcile gate."""
import json
import sqlite3
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
from app.patch_publishing import reconcile_patch_automation, resolve_automation_policy
from app.youtube_metadata import save_book_youtube_config

NOW = datetime.now(timezone.utc).isoformat()
RATE = 100
FRAMES = 1200


def _wav_bytes(frames=FRAMES):
    buf = BytesIO()
    sf.write(buf, np.zeros(frames), RATE, format="WAV")
    return buf.getvalue()


def _timeline(frames=FRAMES) -> dict:
    return {
        "version": 1, "sample_rate": RATE, "total_frames": frames,
        "chapters": [{"chapter_index": 1, "title": "C", "start_frame": 0, "start_seconds": 0.0}],
    }


def _book(auto_create_video=True, auto_upload_youtube=True):
    return SimpleNamespace(auto_create_video=auto_create_video,
                           auto_upload_youtube=auto_upload_youtube)


def test_resolve_policy_defaults_request_override_and_upload_implies_create():
    assert resolve_automation_policy(_book()) == {
        "auto_create_video": True, "auto_upload_youtube": True}
    assert resolve_automation_policy(_book(False, False)) == {
        "auto_create_video": False, "auto_upload_youtube": False}
    assert resolve_automation_policy(_book(False, True)) == {
        "auto_create_video": True, "auto_upload_youtube": True}
    assert resolve_automation_policy(_book(True, False), request_policy={
        "auto_create_video": False}) == {"auto_create_video": False, "auto_upload_youtube": False}
    assert resolve_automation_policy(_book(False, True), request_policy={
        "auto_upload_youtube": False}) == {"auto_create_video": False, "auto_upload_youtube": False}
    assert resolve_automation_policy(_book(False, False), request_policy={
        "auto_upload_youtube": True}) == {"auto_create_video": True, "auto_upload_youtube": True}


def test_legacy_automation_config_migration(tmp_path):
    """Books created before the split flags keep their youtube.auto_upload behavior:
    strict JSON true -> both flags on; anything else -> both off."""
    path = tmp_path / "legacy.db"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE book (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, automation_config TEXT)")
    raw.executemany("INSERT INTO book (title, automation_config) VALUES (?,?)", [
        ("on", json.dumps({"youtube": {"auto_upload": True}})),
        ("off", json.dumps({"youtube": {"auto_upload": False}})),
        ("empty", "{}"),
        ("none", None),
    ])
    raw.commit()
    raw.close()

    conn = db.connect(str(path))
    db.init_schema(conn)
    got = {row["title"]: (row["auto_create_video"], row["auto_upload_youtube"])
           for row in conn.execute("SELECT * FROM book")}
    assert got["on"] == (1, 1)
    assert got["off"] == (0, 0)
    assert got["empty"] == (0, 0)
    assert got["none"] == (0, 0)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as c:
        conn = db.connect(str(tmp_path / "app.db"))
        book_id = conn.execute(
            "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
            "VALUES ('B', 'b.epub', 'b.epub', 1, 'ready', ?, ?)", (NOW, NOW),
        ).lastrowid
        conn.execute(
            "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) "
            "VALUES (?, 0, 'C', 'Hello world text.', 17)", (book_id,),
        )
        conn.execute(
            "INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
            "VALUES (41, ?, 0, 0, 0, 'pending', ?, ?)", (book_id, NOW, NOW),
        )
        conn.commit()
        conn.close()
        yield c, book_id


def _upload(name, payload):
    if isinstance(payload, dict):
        return ("files", (name, json.dumps(payload).encode("utf-8"), "application/json"))
    return ("files", (name, payload, "audio/wav"))


def test_policy_off_keeps_audio_installed_without_enqueueing(client):
    c, book_id = client
    with db.connect(settings.db_path) as conn:
        conn.execute("UPDATE book SET auto_create_video=0, auto_upload_youtube=0 WHERE id=?", (book_id,))
        conn.commit()
    body = c.post(f"/books/{book_id}/patches/upload-results",
                  files=[_upload("000 - a.wav", _wav_bytes())]).json()
    row = body["results"][0]
    assert row["status"] == "ok"
    assert row["publish_status"] == "skipped_auto_upload_disabled"
    assert body["auto_create_video"] is False
    assert body["auto_upload"] is False
    audio = Path(settings.data_root) / "books" / str(book_id) / "patches" / "41.wav"
    assert audio.is_file()
    with db.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0


def test_reconcile_skips_waiting_timeline_and_enqueues_ready_patch(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    now = NOW
    book_id = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES ('B', 'b.epub', 'b.epub', 1, 'ready', ?, ?)", (now, now),
    ).lastrowid
    audio_dir = tmp_path / "books" / str(book_id) / "patches"
    audio_dir.mkdir(parents=True)
    ready_audio = audio_dir / "a.wav"
    ready_audio.write_bytes(_wav_bytes())
    (audio_dir / "a.timeline.json").write_text(json.dumps(_timeline()), encoding="utf-8")
    waiting_audio = audio_dir / "b.wav"
    waiting_audio.write_bytes(_wav_bytes())
    conn.execute(
        "INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status, audio_path, created_at, updated_at) "
        "VALUES (1, ?, 0, 0, 0, 'done', ?, ?, ?), (2, ?, 1, 0, 0, 'done', ?, ?, ?)",
        (book_id, str(ready_audio), now, now, book_id, str(waiting_audio), now, now),
    )
    save_book_youtube_config(conn, book_id, {
        "auto_upload": True, "playlist": {"mode": "existing", "playlist_id": "PL1"},
    })
    conn.commit()
    monkeypatch.setattr("app.patch_publishing.youtube.is_configured", lambda: True)
    monkeypatch.setattr("app.patch_publishing.youtube.get_creds_from_db", lambda conn: {"id": 1})

    stats = reconcile_patch_automation(conn, book_id=book_id)
    assert stats["checked"] == 2
    assert stats["enqueued_render"] == 1
    assert stats["enqueued_upload"] == 0
    assert stats["skipped"] == 1
    jobs = conn.execute("SELECT patch_id FROM job WHERE job_type='patch_video'").fetchall()
    assert [row["patch_id"] for row in jobs] == [1]

    (audio_dir / "b.timeline.json").write_text(json.dumps(_timeline()), encoding="utf-8")
    conn.commit()
    stats = reconcile_patch_automation(conn, book_id=book_id)
    assert stats["enqueued_render"] == 1
    jobs = conn.execute("SELECT patch_id FROM job WHERE job_type='patch_video' ORDER BY id").fetchall()
    assert [row["patch_id"] for row in jobs] == [1, 2]


def test_reconcile_enqueues_missing_upload_for_ready_video(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "b.db"))
    db.init_schema(conn)
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    now = NOW
    book_id = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES ('B', 'b.epub', 'b.epub', 1, 'ready', ?, ?)", (now, now),
    ).lastrowid
    audio_dir = tmp_path / "books" / str(book_id) / "patches"
    audio_dir.mkdir(parents=True)
    audio = audio_dir / "a.wav"
    audio.write_bytes(_wav_bytes())
    (audio_dir / "a.timeline.json").write_text(json.dumps(_timeline()), encoding="utf-8")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"media")
    thumb = tmp_path / "thumb.png"
    thumb.write_bytes(b"i")
    conn.execute(
        "INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status, audio_path, created_at, updated_at) "
        "VALUES (1, ?, 0, 0, 0, 'done', ?, ?, ?)", (book_id, str(audio), now, now),
    )
    conn.execute(
        """INSERT INTO patch_pipeline (patch_id, stage, thumbnail_status, video_status,
           upload_status, playlist_status, thumbnail_path, video_path, config_snapshot,
           media_snapshot, created_at, updated_at)
           VALUES (1, 'video', 'done', 'done', 'pending', 'pending', ?, ?, '{}', '{}', ?, ?)""",
        (str(thumb), str(video), now, now),
    )
    save_book_youtube_config(conn, book_id, {
        "auto_upload": True, "playlist": {"mode": "existing", "playlist_id": "PL1"},
    })
    conn.commit()
    monkeypatch.setattr("app.patch_publishing.youtube.is_configured", lambda: True)
    monkeypatch.setattr("app.patch_publishing.youtube.get_creds_from_db", lambda conn: {"id": 1})

    created = {"id": None}

    def fake_publish_stage(conn, patch_id):
        created["id"] = conn.execute(
            """INSERT INTO youtube_uploads (video_path, youtube_video_id, status,
               render_source_type, render_source_id, created_at)
               VALUES (?, '', 'pending', 'patch', ?, ?)""",
            (str(video), patch_id, NOW),
        ).lastrowid
        conn.execute(
            "UPDATE patch_pipeline SET youtube_upload_id=? WHERE patch_id=?",
            (created["id"], patch_id),
        )
        conn.commit()
        return {"youtube_upload_id": created["id"]}

    monkeypatch.setattr("app.patch_publishing.run_patch_publish_stage", fake_publish_stage)
    stats = reconcile_patch_automation(conn, book_id=book_id)
    assert stats["enqueued_upload"] == 1
    assert stats["enqueued_render"] == 0
    jobs = conn.execute("SELECT job_type FROM job WHERE job_type='youtube_upload'").fetchall()
    assert len(jobs) == 1
    assert created["id"] is not None
    assert stats["already_live"] == 0


def test_reconcile_retries_failed_upload_without_rendering_again(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "failed.db"))
    db.init_schema(conn)
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    book_id = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES ('B', 'b.epub', 'b.epub', 1, 'ready', ?, ?)", (NOW, NOW),
    ).lastrowid
    conn.execute(
        "UPDATE book SET auto_create_video=1, auto_upload_youtube=1 WHERE id=?", (book_id,),
    )
    audio = tmp_path / "a.wav"
    audio.write_bytes(_wav_bytes())
    (tmp_path / "a.timeline.json").write_text(json.dumps(_timeline()), encoding="utf-8")
    video = tmp_path / "v.mp4"
    video.write_bytes(b"media")
    conn.execute(
        "INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status, audio_path, created_at, updated_at) "
        "VALUES (1, ?, 0, 0, 0, 'done', ?, ?, ?)", (book_id, str(audio), NOW, NOW),
    )
    upload_id = conn.execute(
        "INSERT INTO youtube_uploads (video_path, status, created_at) VALUES (?, 'failed', ?)",
        (str(video), NOW),
    ).lastrowid
    conn.execute(
        """INSERT INTO patch_pipeline
           (patch_id, stage, video_status, upload_status, video_path, youtube_upload_id,
            config_snapshot, media_snapshot, created_at, updated_at)
           VALUES (1, 'upload', 'done', 'failed', ?, ?, '{}', '{}', ?, ?)""",
        (str(video), upload_id, NOW, NOW),
    )
    save_book_youtube_config(conn, book_id, {
        "auto_upload": True, "playlist": {"mode": "existing", "playlist_id": "PL1"},
    })
    conn.commit()
    monkeypatch.setattr("app.patch_publishing.youtube.is_configured", lambda: True)
    monkeypatch.setattr("app.patch_publishing.youtube.get_creds_from_db", lambda conn: {"id": 1})
    monkeypatch.setattr("app.patch_publishing.preflight_patch", lambda *args, **kwargs: {"state": "ready"})

    stats = reconcile_patch_automation(conn, book_id=book_id)

    assert stats["enqueued_upload"] == 1
    assert stats["enqueued_render"] == 0
    assert conn.execute("SELECT COUNT(*) FROM youtube_uploads").fetchone()[0] == 1
    job = conn.execute("SELECT job_type, payload_json FROM job").fetchone()
    assert job["job_type"] == "youtube_upload"
    assert json.loads(job["payload_json"])["upload_id"] == upload_id


def test_reconcile_respects_request_policy_override(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "c.db"))
    db.init_schema(conn)
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    now = NOW
    book_id = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "auto_create_video, auto_upload_youtube, created_at, updated_at) "
        "VALUES ('B', 'b.epub', 'b.epub', 1, 'ready', 0, 0, ?, ?)", (now, now),
    ).lastrowid
    audio = tmp_path / "a.wav"
    audio.write_bytes(_wav_bytes())
    conn.execute(
        "INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status, audio_path, created_at, updated_at) "
        "VALUES (1, ?, 0, 0, 0, 'done', ?, ?, ?)", (book_id, str(audio), now, now),
    )
    conn.commit()

    stats = reconcile_patch_automation(conn, book_id=book_id,
                                       request_policy={"auto_create_video": True})
    assert stats["enqueued_render"] == 1
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 1
