"""Advanced filtering/search on the upload queue (youtube.list_uploads) and the
generic per-row field editor (youtube.update_upload_fields), plus the routes that
expose them: GET /youtube/uploads and PATCH /youtube/uploads/{id}.
"""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app import youtube as youtube_module
from app.routes import youtube as youtube_routes


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "uploads.db"))
    db.init_schema(conn)
    return conn


def _seed(conn, **overrides) -> int:
    values = {
        "video_path": "D:/videos/a.mp4",
        "title": "Tập 1",
        "description": "",
        "tags": [],
        "privacy_status": "private",
        "playlist_id": "",
        "not_for_kids": True,
        "ai_labels_enabled": False,
    }
    values.update({k: v for k, v in overrides.items() if k in values})
    return youtube_module.enqueue_upload(conn, **values)


# --------------------------------------------------------------------------- defaults


def test_enqueue_upload_defaults_not_for_kids_true_and_ai_labels_false(tmp_path):
    conn = _conn(tmp_path)
    upload_id = youtube_module.enqueue_upload(conn, "D:/v.mp4", "Title")
    row = conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert row["not_for_kids"] == 1
    assert row["ai_labels_enabled"] == 0


# --------------------------------------------------------------------------- list_uploads filters


def test_list_uploads_search_filters_title_and_description(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, title="Chuyện ma", description="kinh dị")
    _seed(conn, title="Tình yêu", description="lãng mạn")

    assert [r["title"] for r in youtube_module.list_uploads(conn, search="ma")] == ["Chuyện ma"]
    assert [r["title"] for r in youtube_module.list_uploads(conn, search="lãng mạn")] == ["Tình yêu"]


def test_list_uploads_filters_status_and_privacy(tmp_path):
    conn = _conn(tmp_path)
    a = _seed(conn, title="A", privacy_status="public")
    b = _seed(conn, title="B", privacy_status="private")
    conn.execute("UPDATE youtube_uploads SET status='failed' WHERE id=?", (b,))
    conn.commit()

    assert [r["id"] for r in youtube_module.list_uploads(conn, privacy_status="public")] == [a]
    assert [r["id"] for r in youtube_module.list_uploads(conn, status="failed")] == [b]


def test_list_uploads_filters_not_for_kids_and_ai_labels(tmp_path):
    conn = _conn(tmp_path)
    kids_ok = _seed(conn, title="Kids", not_for_kids=False)
    not_kids = _seed(conn, title="NotKids", not_for_kids=True)
    ai_on = _seed(conn, title="AI", ai_labels_enabled=True)

    assert [r["id"] for r in youtube_module.list_uploads(conn, not_for_kids="0")] == [kids_ok]
    assert set(r["id"] for r in youtube_module.list_uploads(conn, not_for_kids="1")) == {not_kids, ai_on}
    assert [r["id"] for r in youtube_module.list_uploads(conn, ai_labels_enabled="1")] == [ai_on]


def test_list_uploads_date_range(tmp_path):
    conn = _conn(tmp_path)
    old = _seed(conn, title="Old")
    conn.execute("UPDATE youtube_uploads SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?", (old,))
    new = _seed(conn, title="New")
    conn.execute("UPDATE youtube_uploads SET created_at='2030-01-01T00:00:00+00:00' WHERE id=?", (new,))
    conn.commit()

    assert [r["id"] for r in youtube_module.list_uploads(conn, date_from="2025-01-01")] == [new]
    assert [r["id"] for r in youtube_module.list_uploads(conn, date_to="2025-01-01")] == [old]


def test_list_uploads_has_playlist_checks_column_and_snapshot(tmp_path):
    conn = _conn(tmp_path)
    no_playlist = _seed(conn, title="None")
    via_column = _seed(conn, title="Published")
    conn.execute("UPDATE youtube_uploads SET playlist_id='PLxyz' WHERE id=?", (via_column,))
    via_snapshot = _seed(conn, title="Queued", playlist_id="PLabc")
    conn.commit()

    has_playlist_ids = {r["id"] for r in youtube_module.list_uploads(conn, has_playlist="yes")}
    no_playlist_ids = {r["id"] for r in youtube_module.list_uploads(conn, has_playlist="no")}
    assert has_playlist_ids == {via_column, via_snapshot}
    assert no_playlist_ids == {no_playlist}


def test_list_uploads_sort_and_order(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, title="B")
    _seed(conn, title="A")
    _seed(conn, title="C")

    titles = [r["title"] for r in youtube_module.list_uploads(conn, sort="title", order="asc")]
    assert titles == ["A", "B", "C"]


# --------------------------------------------------------------------------- update_upload_fields


def test_update_upload_fields_writes_only_supplied_fields(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn, title="Cũ", description="Mô tả cũ")

    updated = youtube_module.update_upload_fields(conn, upload_id, title="Mới")

    assert updated["title"] == "Mới"
    assert updated["description"] == "Mô tả cũ"


def test_update_upload_fields_coerces_booleans_and_tags(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn)

    updated = youtube_module.update_upload_fields(
        conn, upload_id, not_for_kids=False, ai_labels_enabled=True, tags=["a", "b"]
    )

    assert updated["not_for_kids"] == 0
    assert updated["ai_labels_enabled"] == 1
    assert json.loads(updated["tags"]) == ["a", "b"]


def test_update_upload_fields_ignores_unknown_fields(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn)

    updated = youtube_module.update_upload_fields(conn, upload_id, status="done", youtube_video_id="hacked")

    assert updated["status"] == "pending"
    assert updated["youtube_video_id"] is None


def test_update_upload_fields_returns_none_for_missing_id(tmp_path):
    conn = _conn(tmp_path)
    assert youtube_module.update_upload_fields(conn, 999, title="x") is None


def test_set_upload_ai_labels_marks_enabled(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn)

    youtube_module.set_upload_ai_labels(conn, upload_id, ["sach noi", "audiobook"])

    row = conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert row["ai_labels_enabled"] == 1
    assert json.loads(row["ai_labels"]) == ["sach noi", "audiobook"]


# --------------------------------------------------------------------------- routes


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_module, "is_configured", lambda: True)
    conn = _conn(tmp_path)
    app = FastAPI()
    app.include_router(youtube_routes.router)
    app.state.conn = conn
    app.state.db_lock = threading.Lock()
    app.state.job_queue = SimpleNamespace()
    yield SimpleNamespace(conn=conn, client=TestClient(app))
    conn.close()


def test_get_uploads_route_applies_filters(client):
    _seed(client.conn, title="Chuyện ma")
    _seed(client.conn, title="Tình yêu")

    res = client.client.get("/youtube/uploads?search=ma")
    assert res.status_code == 200
    titles = [u["title"] for u in res.json()["uploads"]]
    assert titles == ["Chuyện ma"]


def test_get_uploads_route_has_playlist_filter(client):
    no_playlist = _seed(client.conn, title="None")
    with_playlist = _seed(client.conn, title="Has", playlist_id="PLabc")

    res = client.client.get("/youtube/uploads?has_playlist=no")
    assert [u["id"] for u in res.json()["uploads"]] == [no_playlist]


def test_patch_upload_updates_title(client):
    upload_id = _seed(client.conn, title="Cũ")

    res = client.client.patch(f"/youtube/uploads/{upload_id}", json={"title": "Mới"})

    assert res.status_code == 200
    assert res.json()["title"] == "Mới"


def test_patch_upload_toggles_kids_and_ai_flags(client):
    upload_id = _seed(client.conn)

    res = client.client.patch(
        f"/youtube/uploads/{upload_id}", json={"not_for_kids": False, "ai_labels_enabled": True}
    )

    assert res.status_code == 200
    body = res.json()
    assert body["not_for_kids"] == 0
    assert body["ai_labels_enabled"] == 1


def test_patch_upload_rejects_empty_title(client):
    upload_id = _seed(client.conn)
    res = client.client.patch(f"/youtube/uploads/{upload_id}", json={"title": "  "})
    assert res.status_code == 400


def test_patch_upload_rejects_bad_privacy_status(client):
    upload_id = _seed(client.conn)
    res = client.client.patch(f"/youtube/uploads/{upload_id}", json={"privacy_status": "secret"})
    assert res.status_code == 400


def test_patch_upload_404_for_unknown_id(client):
    res = client.client.patch("/youtube/uploads/9999", json={"title": "x"})
    assert res.status_code == 404
