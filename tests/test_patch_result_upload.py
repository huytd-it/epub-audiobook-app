"""Bulk upload of a batch's result/ folder: WAVs and timelines in one drop."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import db, repository
from app.config import settings
from app.main import app
from app.youtube_metadata import save_book_youtube_config

NOW = datetime.now(timezone.utc).isoformat()
RATE = 100
FRAMES = 1200


def _wav_bytes(frames=FRAMES, rate=RATE) -> bytes:
    buf = BytesIO()
    sf.write(buf, np.zeros(frames), rate, format="WAV")
    return buf.getvalue()


def _timeline(frames=FRAMES, rate=RATE, title="Chương 1") -> dict:
    return {
        "version": 1,
        "sample_rate": rate,
        "total_frames": frames,
        "chapters": [{"chapter_index": 1, "title": title, "start_frame": 0, "start_seconds": 0.0}],
    }


def _upload(name: str, payload) -> tuple[str, tuple[str, bytes, str]]:
    if isinstance(payload, dict):
        return ("files", (name, json.dumps(payload).encode("utf-8"), "application/json"))
    return ("files", (name, payload, "audio/wav"))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A book with two patches whose ids deliberately differ from their indexes -
    result filenames carry the index, so a route matching on id would pass otherwise."""
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
        for patch_id, index in ((41, 0), (42, 1)):
            conn.execute(
                "INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, 0, 'pending', ?, ?)", (patch_id, book_id, index, NOW, NOW),
            )
        conn.commit()
        conn.close()
        yield c, book_id


def _patch_paths(book_id, patch_id):
    base = Path(settings.data_root) / "books" / str(book_id) / "patches"
    return base / f"{patch_id}.wav", base / f"{patch_id}.timeline.json"


def _enable_auto_upload(book_id):
    with db.connect(settings.db_path) as conn:
        conn.execute(
            "UPDATE book SET auto_create_video=1, auto_upload_youtube=1 WHERE id=?",
            (book_id,),
        )
        save_book_youtube_config(conn, book_id, {
            "auto_upload": True,
            "playlist": {"mode": "existing", "playlist_id": "PL1"},
        })
        conn.commit()


def _connected_youtube(monkeypatch):
    monkeypatch.setattr("app.routes.patches.youtube.is_configured", lambda: True)
    monkeypatch.setattr("app.routes.patches.youtube.get_creds_from_db", lambda conn: {"id": 1})


def test_bulk_upload_installs_wav_and_timeline_per_patch(client):
    c, book_id = client
    response = c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - Tập 1.wav", _wav_bytes()),
        _upload("000 - Tập 1.timeline.json", _timeline(title="Một")),
        _upload("001 - Tập 2.wav", _wav_bytes()),
        _upload("001 - Tập 2.timeline.json", _timeline(title="Hai")),
    ])
    assert response.status_code == 200
    body = response.json()
    assert body["installed"] == 2
    assert [(r["patch_index"], r["patch_id"], r["status"], r["audio"], r["timeline"]) for r in body["results"]] == [
        (0, 41, "ok", True, "installed"),
        (1, 42, "ok", True, "installed"),
    ]

    for patch_id, title in ((41, "Một"), (42, "Hai")):
        audio, sidecar = _patch_paths(book_id, patch_id)
        assert sf.info(str(audio)).frames == FRAMES
        assert json.loads(sidecar.read_text(encoding="utf-8"))["chapters"][0]["title"] == title

    with db.connect(str(Path(settings.data_root) / "app.db")) as conn:
        assert [repository.get_patch(conn, pid).status for pid in (41, 42)] == ["done", "done"]


def test_bulk_upload_keeps_a_timeline_that_does_not_describe_its_wav(client):
    """Same rule as the Drive import: a mismatched sidecar is reported, not installed."""
    c, book_id = client
    response = c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - Tập 1.wav", _wav_bytes()),
        _upload("000 - Tập 1.timeline.json", _timeline(frames=FRAMES * 2)),
    ])
    row = response.json()["results"][0]
    assert (row["status"], row["audio"], row["timeline"]) == ("ok", True, "rejected")
    audio, sidecar = _patch_paths(book_id, 41)
    assert audio.is_file() and not sidecar.exists()


def test_uploading_a_wav_alone_clears_a_stale_sidecar(client):
    c, book_id = client
    files = [_upload("000 - a.wav", _wav_bytes()), _upload("000 - a.timeline.json", _timeline())]
    assert c.post(f"/books/{book_id}/patches/upload-results", files=files).status_code == 200
    _, sidecar = _patch_paths(book_id, 41)
    assert sidecar.is_file()

    response = c.post(f"/books/{book_id}/patches/upload-results",
                      files=[_upload("000 - a.wav", _wav_bytes(frames=FRAMES * 3))])
    assert response.json()["results"][0]["timeline"] == "none"
    assert not sidecar.exists()


def test_timeline_alone_backfills_audio_already_installed(client):
    c, book_id = client
    assert c.post(f"/books/{book_id}/patches/upload-results",
                  files=[_upload("000 - a.wav", _wav_bytes())]).status_code == 200

    response = c.post(f"/books/{book_id}/patches/upload-results",
                      files=[_upload("000 - a.timeline.json", _timeline(title="Sau"))])
    row = response.json()["results"][0]
    assert (row["status"], row["audio"], row["timeline"]) == ("ok", False, "installed")
    assert response.json()["installed"] == 0
    _, sidecar = _patch_paths(book_id, 41)
    assert json.loads(sidecar.read_text(encoding="utf-8"))["chapters"][0]["title"] == "Sau"


def test_timeline_alone_without_audio_reports_the_patch_not_the_request(client):
    c, book_id = client
    row = c.post(f"/books/{book_id}/patches/upload-results",
                 files=[_upload("001 - b.timeline.json", _timeline())]).json()["results"][0]
    assert row["status"] == "error"
    assert "chưa có audio" in row["detail"]


def test_bad_files_are_reported_without_blocking_the_rest(client):
    c, book_id = client
    body = c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - ok.wav", _wav_bytes()),
        _upload("001 - broken.wav", b"not a wav at all"),
        _upload("099 - missing patch.wav", _wav_bytes()),
        _upload("notes.txt", b"hello"),
    ]).json()
    by_status = {r["status"]: r for r in body["results"]}
    assert body["installed"] == 1
    assert by_status["ok"]["patch_id"] == 41
    assert by_status["error"]["patch_id"] == 42
    assert "WAV không hợp lệ" in by_status["error"]["detail"]
    skipped = sorted(r["filename"] for r in body["results"] if r["status"] == "skipped")
    assert skipped == ["099 - missing patch.wav", "notes.txt"]
    assert _patch_paths(book_id, 41)[0].is_file()
    assert not _patch_paths(book_id, 42)[0].exists()


def test_duplicate_files_for_one_patch_are_skipped_not_silently_overwritten(client):
    c, book_id = client
    body = c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - first.wav", _wav_bytes()),
        _upload("000 - second.wav", _wav_bytes(frames=FRAMES * 2)),
    ]).json()
    assert body["installed"] == 1
    assert [r["status"] for r in body["results"]] == ["ok", "skipped"]
    assert sf.info(str(_patch_paths(book_id, 41)[0])).frames == FRAMES


def test_a_processing_patch_is_left_to_the_worker(client):
    c, book_id = client
    with db.connect(str(Path(settings.data_root) / "app.db")) as conn:
        conn.execute("UPDATE patch SET status = 'processing' WHERE id = 41")
        conn.commit()

    body = c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - đang chạy.wav", _wav_bytes()),
        _upload("001 - rảnh.wav", _wav_bytes()),
    ]).json()
    assert body["installed"] == 1
    assert [(r["patch_id"], r["status"], r.get("detail")) for r in body["results"]] == [
        (41, "error", "patch đang xử lý"),
        (42, "ok", None),
    ]
    assert not _patch_paths(book_id, 41)[0].exists()


def test_upload_results_rejects_an_unknown_book(client):
    c, _ = client
    response = c.post("/books/9999/patches/upload-results",
                      files=[_upload("000 - a.wav", _wav_bytes())])
    assert response.status_code == 404


def test_auto_upload_preflight_failure_keeps_audio_without_rendering(client, monkeypatch):
    c, book_id = client
    _enable_auto_upload(book_id)
    monkeypatch.setattr("app.routes.patches.youtube.is_configured", lambda: False)

    body = c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - a.wav", _wav_bytes()),
    ]).json()

    row = body["results"][0]
    assert body["publish_ready"] is False
    assert "YouTube chưa" in body["publish_warning"]
    assert row["status"] == "ok"
    assert row["publish_status"] == "waiting_config"
    assert _patch_paths(book_id, 41)[0].is_file()
    with db.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0


def test_auto_upload_partial_success_only_enqueues_valid_patch(client, monkeypatch):
    c, book_id = client
    _enable_auto_upload(book_id)
    _connected_youtube(monkeypatch)
    monkeypatch.setattr("app.routes.patches._warm_thumbnail", lambda request, patch_id: None)

    body = c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - ok.wav", _wav_bytes()),
        _upload("000 - ok.timeline.json", _timeline()),
        _upload("001 - broken.wav", b"not wav"),
    ]).json()

    rows = {row["patch_id"]: row for row in body["results"]}
    assert body["installed"] == 1
    assert rows[41]["publish_status"] == "queued"
    assert rows[42]["status"] == "error"
    with db.connect(settings.db_path) as conn:
        jobs = conn.execute("SELECT job_type, patch_id FROM job").fetchall()
        assert [(row["job_type"], row["patch_id"]) for row in jobs] == [("patch_video", 41)]


def test_live_patch_video_job_blocks_only_that_patch(client, monkeypatch):
    c, book_id = client
    _enable_auto_upload(book_id)
    _connected_youtube(monkeypatch)

    with db.connect(settings.db_path) as conn:
        conn.execute(
            """INSERT INTO job (job_type,status,book_id,payload_json,dedupe_key,patch_id,created_at,updated_at)
               VALUES ('patch_video','running',?,'{}','patch_video:patch=41',41,?,?)""",
            (book_id, NOW, NOW),
        )
        conn.commit()

    body = c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - busy.wav", _wav_bytes()),
        _upload("001 - free.wav", _wav_bytes()),
    ]).json()

    rows = {row["patch_id"]: row for row in body["results"]}
    assert rows[41]["publish_status"] == "blocked_active_pipeline"
    assert "đang tạo hoặc upload" in rows[41]["detail"]
    assert rows[42]["status"] == "ok"
    assert not _patch_paths(book_id, 41)[0].exists()
    assert _patch_paths(book_id, 42)[0].is_file()


def test_published_patch_with_new_audio_waits_for_confirmation(client, monkeypatch):
    c, book_id = client
    _enable_auto_upload(book_id)
    _connected_youtube(monkeypatch)
    monkeypatch.setattr("app.routes.patches._warm_thumbnail", lambda request, patch_id: None)
    with db.connect(settings.db_path) as conn:
        upload_id = conn.execute(
            """INSERT INTO youtube_uploads
               (video_path,youtube_video_id,status,render_source_type,render_source_id,created_at)
               VALUES ('old.mp4','yt-existing','done','patch',41,?)""", (NOW,),
        ).lastrowid
        conn.execute(
            """INSERT INTO patch_pipeline
               (patch_id,stage,thumbnail_status,video_status,upload_status,playlist_status,
                youtube_upload_id,config_snapshot,media_snapshot,created_at,updated_at)
               VALUES (41,'published','done','done','done','done',?,'{}','{}',?,?)""",
            (upload_id, NOW, NOW),
        )
        conn.commit()

    body = c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - replacement.wav", _wav_bytes(frames=FRAMES * 2)),
    ]).json()
    existing_audio = _patch_paths(book_id, 41)[0]

    row = body["results"][0]
    assert row["status"] == "ok"
    assert row["publish_status"] == "awaiting_republish_confirmation"
    assert "cần xác nhận publish lại" in row["publish_error"]
    assert body["publish_ready"] is False
    assert sf.info(str(existing_audio)).frames == FRAMES * 2
    with db.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0
        pipeline = conn.execute(
            "SELECT stage,youtube_upload_id,republish_confirmed_for FROM patch_pipeline WHERE patch_id=41"
        ).fetchone()
        assert (pipeline["stage"], pipeline["youtube_upload_id"], pipeline["republish_confirmed_for"]) == (
            "published", upload_id, None,
        )


def test_confirming_republish_enqueues_fresh_video_keeping_the_upload_history(client, monkeypatch):
    c, book_id = client
    _enable_auto_upload(book_id)
    _connected_youtube(monkeypatch)
    monkeypatch.setattr("app.routes.patches._warm_thumbnail", lambda request, patch_id: None)
    with db.connect(settings.db_path) as conn:
        upload_id = conn.execute(
            """INSERT INTO youtube_uploads
               (video_path,youtube_video_id,status,render_source_type,render_source_id,created_at)
               VALUES ('old.mp4','yt-existing','done','patch',41,?)""", (NOW,),
        ).lastrowid
        conn.execute(
            """INSERT INTO patch_pipeline
               (patch_id,stage,thumbnail_status,video_status,upload_status,playlist_status,
                youtube_upload_id,config_snapshot,media_snapshot,created_at,updated_at)
               VALUES (41,'published','done','done','done','done',?,'{}','{}',?,?)""",
            (upload_id, NOW, NOW),
        )
        conn.commit()

    first = c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - replacement.wav", _wav_bytes(frames=FRAMES * 2)),
        _upload("000 - replacement.timeline.json", _timeline(frames=FRAMES * 2, title="Sau")),
    ]).json()
    assert first["results"][0]["publish_status"] == "awaiting_republish_confirmation"
    with db.connect(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0

    confirm = c.post(f"/books/{book_id}/patches/{41}/publish/confirm")
    assert confirm.status_code == 200
    body = confirm.json()
    assert body["state"] == "queued"
    assert body["job_id"] is not None
    assert body["stage"] == "upload"

    with db.connect(settings.db_path) as conn:
        jobs = conn.execute(
            "SELECT payload_json FROM job WHERE job_type='patch_video' AND patch_id=41"
        ).fetchall()
        assert len(jobs) == 1
        payload = json.loads(jobs[0]["payload_json"])
        assert payload["upload_youtube"] is True
        assert payload["snapshot"]["audio_path"] == str(_patch_paths(book_id, 41)[0])
        assert payload["snapshot"]["schema_version"] == 1
        pipeline = conn.execute(
            "SELECT stage, youtube_upload_id, video_status FROM patch_pipeline WHERE patch_id=41"
        ).fetchone()
        assert (pipeline["stage"], pipeline["video_status"], pipeline["youtube_upload_id"]) == (
            "upload", "pending", None,
        )
        history = conn.execute(
            "SELECT youtube_video_id, status FROM youtube_uploads WHERE render_source_id=41"
        ).fetchall()
        assert [row["youtube_video_id"] for row in history] == ["yt-existing"]
        assert history[0]["status"] == "done"


def test_confirming_republish_twice_is_deduplicated(client, monkeypatch):
    c, book_id = client
    _enable_auto_upload(book_id)
    _connected_youtube(monkeypatch)
    monkeypatch.setattr("app.routes.patches._warm_thumbnail", lambda request, patch_id: None)
    with db.connect(settings.db_path) as conn:
        upload_id = conn.execute(
            """INSERT INTO youtube_uploads
               (video_path,youtube_video_id,status,render_source_type,render_source_id,created_at)
               VALUES ('old.mp4','yt-existing','done','patch',41,?)""", (NOW,),
        ).lastrowid
        conn.execute(
            """INSERT INTO patch_pipeline
               (patch_id,stage,thumbnail_status,video_status,upload_status,playlist_status,
                youtube_upload_id,config_snapshot,media_snapshot,created_at,updated_at)
               VALUES (41,'published','done','done','done','done',?,'{}','{}',?,?)""",
            (upload_id, NOW, NOW),
        )
        conn.commit()
    c.post(f"/books/{book_id}/patches/upload-results", files=[
        _upload("000 - replacement.wav", _wav_bytes(frames=FRAMES * 2)),
        _upload("000 - replacement.timeline.json", _timeline(frames=FRAMES * 2, title="Sau")),
    ])

    first = c.post(f"/books/{book_id}/patches/{41}/publish/confirm").json()
    second = c.post(f"/books/{book_id}/patches/{41}/publish/confirm").json()
    assert (first["state"], second["state"]) == ("queued", "queued")
    assert second["deduplicated"] is True
    assert first["job_id"] == second["job_id"]
    with db.connect(settings.db_path) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM job WHERE job_type='patch_video' AND patch_id=41"
        ).fetchone()[0] == 1
