"""Nút dọn thư mục sync Kaggle trên /drive: liệt kê (dung lượng, trạng thái import,
quota) và chỉ chuyển vào thùng rác batch đã import xong, không có job đang chạy/hoãn."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, google_drive, kaggle_packages
from app.jobqueue import store
from app.routes import drive as drive_routes

FOLDER = "application/vnd.google-apps.folder"
NOW = "2026-01-01T00:00:00+00:00"


class _Exec:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


class FakeDrive:
    """Cây thư mục giả: root -> batch folders -> file con (có size)."""

    def __init__(self, tree):
        self.tree = tree
        self.trashed: list[str] = []

    def files(self):
        drive = self

        class _Files:
            def list(self, q="", **kw):
                return _Exec({"files": [{"id": "root"}]} if "EPUB Audiobook Exports" in q else {"files": []})

            def update(self, fileId, body, **kw):
                assert body == {"trashed": True}
                drive.trashed.append(fileId)
                return _Exec({"id": fileId})
        return _Files()

    def about(self):
        class _About:
            def get(self, fields):
                return _Exec({"storageQuota": {"limit": "16106127360", "usage": "1000",
                                               "usageInDrive": "900", "usageInDriveTrash": "0"}})
        return _About()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "sync.db"))
    db.init_schema(conn)
    kaggle_packages_schema = getattr(kaggle_packages, "SCHEMA", "")
    conn.executescript(kaggle_packages_schema)
    account_id = google_drive.save_credentials(conn, "at", "rt", "", "k@x.com")
    book = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES ('Sách A', 'a.epub', 'a.epub', 10, 'ready', ?, ?)", (NOW, NOW)).lastrowid
    patches = []
    for index, status in enumerate(("done", "done", "pending")):
        patches.append(conn.execute(
            "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)", (book, index, index, index, status, NOW, NOW)).lastrowid)
    conn.commit()

    def package(batch_id, slug, patch_ids, folder_id=None):
        entries = [{"patch_id": p, "patch_index": 0, "wav": f"{p}.wav"} for p in patch_ids]
        cur = conn.execute(
            "INSERT INTO kaggle_package (batch_id, book_id, title, slug, payload_json, patches_json, "
            "drive_account_id, drive_folder_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (batch_id, book, "Sách A", slug, json.dumps({"book_id": book, "patch_ids": patch_ids}),
             json.dumps(entries), account_id if folder_id else None, folder_id, NOW, NOW))
        conn.commit()
        return cur.lastrowid

    done_pkg = package("batch-done", "slug-done", patches[:2], folder_id="f-done")
    package("batch-partial", "slug-partial", patches, folder_id=None)  # thư mục tìm theo tên slug
    tree = {
        "root": [{"id": "f-done", "name": "batch-done", "mimeType": FOLDER},
                 {"id": "f-partial", "name": "slug-partial", "mimeType": FOLDER},
                 {"id": "f-other", "name": "khong-phai-batch", "mimeType": FOLDER}],
        "f-done": [{"id": "s1", "name": "result", "mimeType": FOLDER, "modifiedTime": "2026-02-02"},
                   {"id": "x1", "name": "batch_manifest.json", "size": "100", "modifiedTime": "2026-02-01"}],
        "s1": [{"id": "x2", "name": "a.wav", "size": "5000", "modifiedTime": "2026-02-03"}],
        "f-partial": [{"id": "x3", "name": "b.wav", "size": "7", "modifiedTime": "2026-01-05"}],
    }
    service = FakeDrive(tree)
    monkeypatch.setattr(google_drive, "list_files", lambda svc, folder_id: tree.get(folder_id, []))
    monkeypatch.setattr(google_drive, "get_drive_service", lambda c, aid: service)
    monkeypatch.setattr(kaggle_packages, "backfill", lambda c: None)
    app = FastAPI()
    app.include_router(drive_routes.router)
    app.state.conn = conn
    app.state.db_lock = threading.Lock()
    yield SimpleNamespace(conn=conn, client=TestClient(app), service=service, book=book,
                          patches=patches, done_pkg=done_pkg)
    conn.close()


def test_lists_sync_folders_with_size_import_state_and_quota(ctx):
    body = ctx.client.get("/drive/kaggle-sync-folders").json()
    assert body["account"]["email"] == "k@x.com"
    assert body["quota"]["limit"] == 16106127360
    folders = {f["batch_id"]: f for f in body["folders"]}
    assert set(folders) == {"batch-done", "batch-partial"}       # thư mục lạ không hiện
    assert folders["batch-done"]["size_bytes"] == 5100            # cộng đệ quy
    assert folders["batch-done"]["modified_time"] == "2026-02-03"
    assert folders["batch-done"]["can_clean"] is True
    assert folders["batch-partial"]["imported_count"] == 2 and folders["batch-partial"]["can_clean"] is False


def test_cleanup_trashes_only_imported_idle_batches(ctx):
    result = ctx.client.post("/drive/kaggle-sync-folders/cleanup",
                             json={"batch_ids": ["batch-done", "batch-partial"]}).json()
    assert result["trashed"] == ["batch-done"]
    assert result["skipped"] == [{"batch_id": "batch-partial", "reason": "Batch chưa import xong"}]
    assert ctx.service.trashed == ["f-done"]                     # chỉ trashed=True, không xoá
    assert kaggle_packages.get(ctx.conn, ctx.done_pkg)["drive_folder_id"] is None


def test_cleanup_refuses_a_batch_with_a_live_or_deferred_job(ctx):
    store.enqueue(ctx.conn, "kaggle_tts", payload={"package_id": ctx.done_pkg})
    result = ctx.client.post("/drive/kaggle-sync-folders/cleanup", json={"batch_ids": ["batch-done"]}).json()
    assert result["trashed"] == [] and "đang chạy hoặc hoãn" in result["skipped"][0]["reason"]
    assert ctx.service.trashed == []


def test_egress_down_is_a_clear_503(ctx, monkeypatch):
    from app import egress

    def down(c, aid):
        raise egress.EgressUnavailable("server 2 chết")

    monkeypatch.setattr(google_drive, "get_drive_service", down)
    response = ctx.client.get("/drive/kaggle-sync-folders")
    assert response.status_code == 503 and "egress" in response.json()["detail"]
