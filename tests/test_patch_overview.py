"""Trang Quản lý Patch: phân loại patch của mọi sách + đẩy tiếp vào queue."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db, repository
from app.config import settings
from app.jobqueue import store
from app.patch_overview import (continue_publish_targets, list_patch_overview,
                                split_continue_targets)

NOW = datetime.now(timezone.utc).isoformat()


def _book(conn, title, *, automation=True):
    flag = 1 if automation else 0
    return conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "auto_create_video, auto_upload_youtube, created_at, updated_at) "
        "VALUES (?, 'b.epub', 'b.epub', 1, 'ready', ?, ?, ?, ?)",
        (title, flag, flag, NOW, NOW),
    ).lastrowid


def _patch(conn, book_id, index, status="pending", *, next_chunk=0, audio=None, name=""):
    return conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, name, "
        "chunk_count, next_chunk_index, audio_path, created_at, updated_at) "
        "VALUES (?, ?, 0, 0, ?, ?, 10, ?, ?, ?, ?)",
        (book_id, index, status, name, next_chunk, audio, NOW, NOW),
    ).lastrowid


def _pipeline(conn, patch_id, *, video_status="pending", upload_status="pending",
              upload_id=None, video_path=None, stage="video"):
    conn.execute(
        """INSERT INTO patch_pipeline (patch_id, stage, video_status, upload_status,
           video_path, youtube_upload_id, config_snapshot, media_snapshot, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, '{}', '{}', ?, ?)""",
        (patch_id, stage, video_status, upload_status, video_path, upload_id, NOW, NOW),
    )


def _upload(conn, status, video_id=""):
    return conn.execute(
        "INSERT INTO youtube_uploads (video_path, youtube_video_id, status, created_at) "
        "VALUES ('v.mp4', ?, ?, ?)", (video_id, status, NOW),
    ).lastrowid


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    c = db.connect(str(tmp_path / "a.db"))
    db.init_schema(c)
    yield c
    c.close()


def _seed(conn):
    a = _book(conn, "Alpha")
    b = _book(conn, "Beta", automation=False)
    ids = {
        "not_started": _patch(conn, a, 0),
        "tts_partial": _patch(conn, a, 1, next_chunk=3, name="Tập dở"),
        "tts_failed": _patch(conn, a, 2, "failed"),
        "publish_pending": _patch(conn, a, 3, "done", audio="x.wav"),
        "publish_failed": _patch(conn, a, 4, "done", audio="x.wav"),
        "complete": _patch(conn, a, 5, "done", audio="x.wav"),
        "queued": _patch(conn, a, 6),
        "published_not_uploaded": _patch(conn, a, 7, "done", audio="x.wav"),
        "complete_no_automation": _patch(conn, b, 0, "done", audio="x.wav"),
    }
    _pipeline(conn, ids["publish_failed"], video_status="failed")
    _pipeline(conn, ids["complete"], video_status="done", upload_status="done",
              upload_id=_upload(conn, "done", "YT1"), stage="published")
    # stage='published' nhưng không có upload done -> vẫn là dang dở.
    _pipeline(conn, ids["published_not_uploaded"], video_status="done", stage="published")
    store.enqueue(conn, "audiobook_tts", payload={"patch_id": ids["queued"]}, book_id=a,
                  patch_id=ids["queued"], dedupe_key=f"audiobook_tts:patch={ids['queued']}")
    conn.commit()
    return a, b, ids


def test_every_patch_gets_the_phase_of_its_missing_step(conn):
    _, _, ids = _seed(conn)
    data = list_patch_overview(conn, group="all", per_page=200)
    phases = {item["patch_id"]: item["phase"] for item in data["items"]}
    assert phases[ids["not_started"]] == "not_started"
    assert phases[ids["tts_partial"]] == "tts_partial"
    assert phases[ids["tts_failed"]] == "tts_failed"
    assert phases[ids["publish_pending"]] == "publish_pending"
    assert phases[ids["publish_failed"]] == "publish_failed"
    assert phases[ids["complete"]] == "complete"
    assert phases[ids["queued"]] == "queued"
    assert phases[ids["published_not_uploaded"]] == "publish_pending"
    assert phases[ids["complete_no_automation"]] == "complete"
    assert data["counts"] == {"unfinished": 5, "not_started": 1, "queued": 1,
                              "complete": 2, "all": 9}
    queued = next(i for i in data["items"] if i["patch_id"] == ids["queued"])
    assert queued["live_job"]["job_type"] == "audiobook_tts"


def test_groups_filters_and_pagination(conn):
    a, b, ids = _seed(conn)
    unfinished = list_patch_overview(conn)
    assert {i["phase"] for i in unfinished["items"]} <= {
        "tts_partial", "tts_failed", "publish_pending", "publish_failed"}
    assert unfinished["total"] == 5
    assert [x["title"] for x in unfinished["books"]] == ["Alpha", "Beta"]

    only_b = list_patch_overview(conn, group="all", book_id=b)
    assert [i["patch_id"] for i in only_b["items"]] == [ids["complete_no_automation"]]

    by_name = list_patch_overview(conn, group="all", q="tập DỞ")
    assert [i["patch_id"] for i in by_name["items"]] == [ids["tts_partial"]]

    page2 = list_patch_overview(conn, group="all", book_id=a, page=2, per_page=3)
    assert page2["total_pages"] == 3
    assert [i["patch_index"] for i in page2["items"]] == [3, 4, 5]

    with pytest.raises(ValueError):
        list_patch_overview(conn, group="nope")


def test_split_routes_each_patch_to_its_next_step(conn):
    a, _, ids = _seed(conn)
    targets = split_continue_targets(conn, [
        ids["tts_partial"], ids["tts_failed"], ids["publish_failed"],
        ids["queued"], ids["complete"], 99999,
    ])
    assert targets["tts"] == {a: [ids["tts_partial"], ids["tts_failed"]]}
    assert [t["patch_id"] for t in targets["publish"]] == [ids["publish_failed"]]
    assert {s["patch_id"]: s["reason"] for s in targets["skipped"]} == {
        ids["queued"]: "already_queued", ids["complete"]: "complete", 99999: "not_found"}


def test_publish_continue_reports_why_a_patch_was_skipped(conn, tmp_path):
    b = _book(conn, "Off", automation=False)
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    patch_id = _patch(conn, b, 0, "done", audio=str(audio))
    conn.commit()
    result = continue_publish_targets(conn, [{"patch_id": patch_id, "book_id": b,
                                              "audio_path": str(audio)}])
    assert result["skipped"] == [{"patch_id": patch_id, "reason": "no_automation", "error": None}]
    assert conn.execute("SELECT COUNT(*) FROM job").fetchone()[0] == 0


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app_module()) as c:
        yield c


def app_module():
    from app.main import app
    return app


def test_continue_route_resumes_tts_without_wiping_existing_chunks(client):
    with db.connect(settings.db_path) as conn:
        a, _, ids = _seed(conn)
    chunk_dir = repository.get_patch_chunk_dir(a, 1)
    chunk_dir.mkdir(parents=True)
    kept = chunk_dir / "chunk_000.wav"
    kept.write_bytes(b"RIFF")

    resp = client.post("/api/ui/patches/continue",
                       json={"patch_ids": [ids["tts_partial"], ids["queued"]]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tts_queued"] == 1
    assert {s.get("patch_id"): s["reason"] for s in body["skipped"]} == {
        ids["queued"]: "already_queued"}
    assert kept.is_file()
    with db.connect(settings.db_path) as conn:
        job = conn.execute(
            "SELECT job_type, payload_json FROM job WHERE patch_id=?", (ids["tts_partial"],),
        ).fetchone()
        patch = repository.get_patch(conn, ids["tts_partial"])
    assert job["job_type"] in {"audiobook_tts", "audiobook_tts_api"}
    assert json.loads(job["payload_json"])["patch_id"] == ids["tts_partial"]
    assert patch.next_chunk_index == 3  # không bị reset về 0

    listing = client.get("/api/ui/patches", params={"group": "queued"}).json()
    assert ids["tts_partial"] in {i["patch_id"] for i in listing["items"]}


def test_continue_route_validates_input(client):
    assert client.post("/api/ui/patches/continue", json={"patch_ids": []}).status_code == 400
    assert client.get("/api/ui/patches", params={"group": "bad"}).status_code == 400
