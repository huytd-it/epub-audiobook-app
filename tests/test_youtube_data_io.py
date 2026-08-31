"""Export/import of the YouTube upload queue (app/youtube_io.py + its routes).

Covers the round trip that the feature rests on: export -> edit -> import, plus the
guards that keep a bad sheet from corrupting the queue (unknown ids, invalid privacy,
over-long titles, rows the worker is currently uploading).
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db
from app import youtube as youtube_module
from app import youtube_io
from app.routes import youtube as youtube_routes

_NOW = datetime.now(timezone.utc).isoformat()


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "io.db"))
    db.init_schema(conn)
    return conn


def _seed(conn, **overrides) -> int:
    values = {
        "video_path": "D:/videos/a.mp4",
        "title": "Tập 1 - Chương 1",
        "description": "Mô tả gốc",
        "tags": ["sach noi"],
        "privacy_status": "private",
        "playlist_id": "",
    }
    values.update({k: v for k, v in overrides.items() if k in values})
    upload_id = youtube_module.enqueue_upload(conn, **values)
    if "status" in overrides:
        conn.execute("UPDATE youtube_uploads SET status=? WHERE id=?", (overrides["status"], upload_id))
        conn.commit()
    if "youtube_video_id" in overrides:
        conn.execute("UPDATE youtube_uploads SET youtube_video_id=? WHERE id=?",
                     (overrides["youtube_video_id"], upload_id))
        conn.commit()
    return upload_id


# --------------------------------------------------------------------------- export


def test_export_records_shape_and_playlist_from_snapshot(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn, playlist_id="PL123", tags=["a", "b"])

    records = youtube_io.export_records(conn)

    assert len(records) == 1
    record = records[0]
    assert list(record) == youtube_io.EXPORT_COLUMNS
    assert record["id"] == upload_id
    assert record["tags"] == "a, b"
    assert record["playlist_id"] == "PL123"
    assert record["status"] == "pending"


def test_export_records_includes_kids_and_ai_label_flags(tmp_path):
    conn = _conn(tmp_path)
    default_id = _seed(conn)
    kid_friendly = youtube_module.enqueue_upload(conn, "D:/videos/b.mp4", "B", not_for_kids=False, ai_labels_enabled=True)

    by_id = {r["id"]: r for r in youtube_io.export_records(conn)}
    assert by_id[default_id]["not_for_kids"] == "true"
    assert by_id[default_id]["ai_labels_enabled"] == "false"
    assert by_id[kid_friendly]["not_for_kids"] == "false"
    assert by_id[kid_friendly]["ai_labels_enabled"] == "true"


def test_export_records_filters_by_id_and_status(tmp_path):
    conn = _conn(tmp_path)
    first = _seed(conn, title="A")
    second = _seed(conn, title="B", status="failed")

    assert [r["id"] for r in youtube_io.export_records(conn, ids=[second])] == [second]
    assert [r["id"] for r in youtube_io.export_records(conn, statuses=["pending"])] == [first]
    # newest first
    assert [r["id"] for r in youtube_io.export_records(conn)] == [second, first]


def test_csv_round_trip_preserves_every_column(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn, title='Tập 1 - "Mở đầu", phần A', description="Dòng 1\nDòng 2", tags=["x, y", "z"])

    records = youtube_io.export_records(conn)
    parsed = youtube_io.parse_records(youtube_io.records_to_csv(records), "csv")

    assert len(parsed) == 1
    for column in youtube_io.EXPORT_COLUMNS:
        assert str(parsed[0][column]) == str(records[0][column])


def test_json_export_wraps_records_and_parses_back(tmp_path):
    conn = _conn(tmp_path)
    _seed(conn)
    records = youtube_io.export_records(conn)

    payload = json.loads(youtube_io.records_to_json(records))
    assert payload["kind"] == "youtube_uploads"
    assert youtube_io.parse_records(json.dumps(payload), "json") == records


def test_parse_records_accepts_bare_list_and_rejects_junk():
    assert youtube_io.parse_records('[{"id": 1}]', "json") == [{"id": 1}]
    with pytest.raises(ValueError):
        youtube_io.parse_records("{}", "json")
    with pytest.raises(ValueError):
        youtube_io.parse_records("[1, 2]", "json")
    with pytest.raises(ValueError):
        youtube_io.parse_records("not json", "json")
    with pytest.raises(ValueError):
        youtube_io.parse_records("a,b\n1,2\n", "csv")


def test_csv_import_normalizes_crlf_in_multiline_description():
    # Excel writes \r\n line endings, including inside a quoted multi-line cell (e.g.
    # a description with a chapter list). Without normalizing, those \r\n pairs would
    # land in the stored description verbatim and drift the text on every round-trip.
    raw = 'id,title,description\r\n1,"T","dong 1\r\ndong 2"\r\n'.encode("utf-8-sig")
    records = youtube_io.parse_records(raw, "csv")
    assert records[0]["description"] == "dong 1\ndong 2"
    assert "\r" not in records[0]["description"]


def test_csv_import_preserves_emoji_and_vietnamese_through_a_round_trip():
    desc = "Sách nói - Chương 1 \U0001f3a7 (\u201cĐêm khuya\u201d) #audiobook \U0001f1fb\U0001f1f3"
    record = {"id": 1, "title": "Tập 1, có dấu phẩy", "description": desc, "tags": "a, b",
              "privacy_status": "private", "playlist_id": "", "video_path": "d:/a.mp4",
              "not_for_kids": "true", "ai_labels_enabled": "false", "status": "done",
              "youtube_video_id": "x", "created_at": "2026"}
    csv_text = youtube_io.records_to_csv([record])
    back = youtube_io.parse_records(csv_text.encode("utf-8-sig"), "csv")
    assert back[0]["description"] == desc
    assert back[0]["title"] == record["title"]


def test_csv_import_rejects_non_utf8_bytes_with_a_helpful_message():
    # A common failure mode: Excel's "CSV (Comma delimited)" save option uses the
    # system codepage instead of UTF-8, silently mangling emoji/Vietnamese text.
    bad = "title\nX\xe9\n".encode("cp1252")
    with pytest.raises(ValueError, match="UTF-8"):
        youtube_io.parse_records(bad, "csv")


# --------------------------------------------------------------------------- import


def test_import_updates_editable_fields_only(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn)

    summary = youtube_io.apply_import(conn, [{
        "id": upload_id,
        "title": "Tiêu đề mới",
        "description": "Mô tả mới",
        "tags": "a, b , ,c",
        "privacy_status": "public",
        "playlist_id": "PLnew",
        "status": "done",              # read-only column: must be ignored
        "youtube_video_id": "hacked",  # read-only column: must be ignored
    }])

    assert summary["counts"]["updated"] == 1
    row = conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert row["title"] == "Tiêu đề mới"
    assert row["description"] == "Mô tả mới"
    assert json.loads(row["tags"]) == ["a", "b", "c"]
    assert row["privacy_status"] == "public"
    assert row["status"] == "pending"
    assert row["youtube_video_id"] is None
    snapshot = json.loads(row["metadata_snapshot"])
    assert snapshot["automation"]["youtube"]["playlist_id"] == "PLnew"


@pytest.mark.parametrize("raw,expected", [("false", 0), ("0", 0), ("no", 0), ("true", 1), ("1", 1), ("yes", 1)])
def test_import_accepts_various_boolean_spellings_for_not_for_kids(tmp_path, raw, expected):
    conn = _conn(tmp_path)
    # newly enqueued rows default to not_for_kids=1 (true), so flip it first to make
    # every spelling in the matrix a real, observable change.
    upload_id = youtube_module.enqueue_upload(conn, "D:/videos/a.mp4", "Title", not_for_kids=False)

    summary = youtube_io.apply_import(conn, [{"id": upload_id, "not_for_kids": raw}])

    assert summary["counts"][("updated" if expected == 1 else "unchanged")] == 1
    row = conn.execute("SELECT not_for_kids FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert row["not_for_kids"] == expected


def test_import_rejects_invalid_boolean_value(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn)

    summary = youtube_io.apply_import(conn, [{"id": upload_id, "ai_labels_enabled": "maybe"}])

    assert summary["counts"]["error"] == 1


def test_import_reports_unchanged_rows_and_omitted_columns(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn, title="Giữ nguyên")

    summary = youtube_io.apply_import(conn, [{"id": upload_id, "title": "Giữ nguyên"}])

    assert summary["counts"]["unchanged"] == 1
    assert summary["results"][0]["changes"] == {}
    # description was not in the sheet at all -> left alone
    assert conn.execute("SELECT description FROM youtube_uploads WHERE id=?",
                        (upload_id,)).fetchone()["description"] == "Mô tả gốc"


def test_dry_run_reports_changes_without_writing(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn)

    summary = youtube_io.apply_import(conn, [{"id": upload_id, "title": "Xem trước"}], dry_run=True)

    assert summary["dry_run"] is True
    assert summary["results"][0]["changes"] == {"title": "Xem trước"}
    assert conn.execute("SELECT title FROM youtube_uploads WHERE id=?",
                        (upload_id,)).fetchone()["title"] == "Tập 1 - Chương 1"


def test_import_rejects_bad_values_and_unknown_ids(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn)

    summary = youtube_io.apply_import(conn, [
        {"id": upload_id, "privacy_status": "secret"},
        {"id": upload_id, "title": ""},
        {"id": upload_id, "title": "x" * 101},
        {"id": 9999, "title": "Không tồn tại"},
        {"id": "abc", "title": "id hỏng"},
        {"title": "Thiếu id"},
    ])

    assert summary["counts"]["error"] == 6
    assert conn.execute("SELECT privacy_status, title FROM youtube_uploads WHERE id=?",
                        (upload_id,)).fetchone()["title"] == "Tập 1 - Chương 1"


def test_import_skips_rows_the_worker_is_uploading(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn, status="uploading")

    summary = youtube_io.apply_import(conn, [{"id": upload_id, "title": "Sửa giữa chừng"}])

    assert summary["counts"]["skipped"] == 1
    assert conn.execute("SELECT title FROM youtube_uploads WHERE id=?",
                        (upload_id,)).fetchone()["title"] == "Tập 1 - Chương 1"


def test_import_warns_when_editing_an_already_published_row(tmp_path):
    conn = _conn(tmp_path)
    upload_id = _seed(conn, status="done", youtube_video_id="abc123")

    summary = youtube_io.apply_import(conn, [{"id": upload_id, "title": "Đổi tên"}])

    assert summary["counts"]["updated"] == 1
    assert summary["results"][0]["warning"]


def test_upsert_creates_rows_without_id(tmp_path):
    conn = _conn(tmp_path)
    created = []

    def create(c, payload):
        created.append(payload)
        return youtube_module.enqueue_upload(
            c, video_path=payload["video_path"], title=payload["title"],
            description=payload["description"], tags=payload["tags"],
            privacy_status=payload["privacy_status"], playlist_id=payload["playlist_id"],
        )

    summary = youtube_io.apply_import(conn, [
        {"id": "", "title": "Mới", "video_path": "D:/videos/new.mp4", "tags": "a,b"},
        {"id": "", "title": "Thiếu đường dẫn"},
    ], mode="upsert", create=create)

    assert summary["counts"]["created"] == 1
    assert summary["counts"]["error"] == 1
    assert created[0]["tags"] == ["a", "b"]
    assert conn.execute("SELECT COUNT(*) c FROM youtube_uploads").fetchone()["c"] == 1


def test_upsert_new_row_defaults_kids_and_ai_flags(tmp_path):
    conn = _conn(tmp_path)

    def create(c, payload):
        return youtube_module.enqueue_upload(
            c, video_path=payload["video_path"], title=payload["title"],
            description=payload["description"], tags=payload["tags"],
            privacy_status=payload["privacy_status"], playlist_id=payload["playlist_id"],
            not_for_kids=payload["not_for_kids"], ai_labels_enabled=payload["ai_labels_enabled"],
        )

    summary = youtube_io.apply_import(conn, [
        {"id": "", "title": "Mới", "video_path": "D:/videos/new.mp4"},
        {"id": "", "title": "Dành cho trẻ em", "video_path": "D:/videos/kid.mp4", "not_for_kids": "false"},
    ], mode="upsert", create=create)

    assert summary["counts"]["created"] == 2
    rows = {r["title"]: r for r in conn.execute("SELECT title, not_for_kids, ai_labels_enabled FROM youtube_uploads").fetchall()}
    assert rows["Mới"]["not_for_kids"] == 1
    assert rows["Mới"]["ai_labels_enabled"] == 0
    assert rows["Dành cho trẻ em"]["not_for_kids"] == 0


def test_update_mode_refuses_to_create(tmp_path):
    conn = _conn(tmp_path)
    summary = youtube_io.apply_import(conn, [{"title": "Mới", "video_path": "D:/x.mp4"}])
    assert summary["counts"]["error"] == 1
    assert conn.execute("SELECT COUNT(*) c FROM youtube_uploads").fetchone()["c"] == 0


def test_apply_import_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError):
        youtube_io.apply_import(_conn(tmp_path), [], mode="delete")


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


def test_export_route_serves_json_and_csv_downloads(client):
    _seed(client.conn, title="Tập 1")

    res = client.client.get("/youtube/uploads/export?format=json")
    assert res.status_code == 200
    assert "attachment" in res.headers["content-disposition"]
    assert res.json()["uploads"][0]["title"] == "Tập 1"

    csv_res = client.client.get("/youtube/uploads/export?format=csv")
    assert csv_res.status_code == 200
    assert csv_res.headers["content-type"].startswith("text/csv")
    assert "Tập 1" in csv_res.content.decode("utf-8-sig")


def test_export_route_validates_format_and_ids(client):
    assert client.client.get("/youtube/uploads/export?format=xml").status_code == 400
    assert client.client.get("/youtube/uploads/export?ids=1,abc").status_code == 400


def test_import_route_dry_run_then_apply(client):
    upload_id = _seed(client.conn)
    sheet = json.dumps({"uploads": [{"id": upload_id, "title": "Tên mới"}]}).encode()

    preview = client.client.post(
        "/youtube/uploads/import",
        files={"file": ("edit.json", sheet, "application/json")},
        data={"dry_run": "true"},
    )
    assert preview.status_code == 200
    assert preview.json()["counts"]["updated"] == 1
    assert client.conn.execute("SELECT title FROM youtube_uploads WHERE id=?",
                               (upload_id,)).fetchone()["title"] == "Tập 1 - Chương 1"

    applied = client.client.post(
        "/youtube/uploads/import",
        files={"file": ("edit.json", sheet, "application/json")},
    )
    assert applied.status_code == 200
    assert client.conn.execute("SELECT title FROM youtube_uploads WHERE id=?",
                               (upload_id,)).fetchone()["title"] == "Tên mới"


def test_import_route_accepts_csv_by_extension(client):
    upload_id = _seed(client.conn)
    csv_text = f"id,title\n{upload_id},Tiêu đề CSV\n".encode("utf-8-sig")

    res = client.client.post(
        "/youtube/uploads/import",
        files={"file": ("edit.csv", csv_text, "text/csv")},
    )

    assert res.status_code == 200
    assert client.conn.execute("SELECT title FROM youtube_uploads WHERE id=?",
                               (upload_id,)).fetchone()["title"] == "Tiêu đề CSV"


def test_import_route_rejects_empty_and_malformed_files(client):
    empty = client.client.post("/youtube/uploads/import",
                               files={"file": ("edit.json", b"   ", "application/json")})
    assert empty.status_code == 400

    broken = client.client.post("/youtube/uploads/import",
                                files={"file": ("edit.json", b"{oops}", "application/json")})
    assert broken.status_code == 400

    no_rows = client.client.post("/youtube/uploads/import",
                                 files={"file": ("edit.json", b"[]", "application/json")})
    assert no_rows.status_code == 400

    bad_mode = client.client.post("/youtube/uploads/import",
                                  files={"file": ("edit.json", b"[]", "application/json")},
                                  data={"mode": "delete"})
    assert bad_mode.status_code == 400


def test_import_route_upsert_queues_a_job(client):
    enqueued = []
    from app.jobqueue import store

    original = store.enqueue

    def spy(conn, job_type, **kwargs):
        enqueued.append((job_type, kwargs.get("payload")))
        return original(conn, job_type, **kwargs)

    store.enqueue, saved = spy, store.enqueue
    try:
        res = client.client.post(
            "/youtube/uploads/import",
            files={"file": ("new.csv",
                            "id,title,video_path\n,Video mới,D:/videos/new.mp4\n".encode("utf-8-sig"),
                            "text/csv")},
            data={"mode": "upsert"},
        )
    finally:
        store.enqueue = saved

    assert res.status_code == 200
    assert res.json()["counts"]["created"] == 1
    assert enqueued and enqueued[0][0] == "youtube_upload"
    assert client.conn.execute("SELECT COUNT(*) c FROM youtube_uploads").fetchone()["c"] == 1
