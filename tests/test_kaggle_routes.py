"""Routes for the Kaggle API TTS automation: account CRUD + export-batch-kaggle."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, kaggle_accounts as ka, repository
from app.config import settings
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as test_client:
        yield test_client


def _seed_book_and_patch(tmp_path):
    conn = db.connect(str(tmp_path / "app.db"))
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES ('B', 'b.epub', 'b.epub', 10, 'ready', ?, ?)", (now, now),
    )
    book_id = cur.lastrowid
    conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, 0, 'C', 'Hello world.', 12)",
        (book_id,),
    )
    cur = conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
        "VALUES (?, 0, 0, 0, 'pending', ?, ?)", (book_id, now, now),
    )
    patch_id = cur.lastrowid
    conn.commit()
    conn.close()
    return book_id, patch_id


def test_create_list_and_delete_account(client, tmp_path):
    resp = client.post("/kaggle/accounts", data={"label": "acc1", "username": "u1", "api_key": "k1"}, follow_redirects=False)
    assert resp.status_code == 303

    data = client.get("/api/ui/kaggle").json()
    assert len(data["accounts"]) == 1
    account = data["accounts"][0]
    assert account["label"] == "acc1"
    assert account["username"] == "u1"
    assert account["status"] == "idle"
    assert account["remaining_quota_hours"] == 30.0

    resp = client.post(f"/kaggle/accounts/{account['id']}/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert client.get("/api/ui/kaggle").json()["accounts"] == []


def test_update_account_keeps_key_when_blank(client, tmp_path):
    client.post("/kaggle/accounts", data={"label": "acc1", "username": "u1", "api_key": "k1"}, follow_redirects=False)
    account_id = client.get("/api/ui/kaggle").json()["accounts"][0]["id"]
    resp = client.post(
        f"/kaggle/accounts/{account_id}/edit",
        data={"label": "renamed", "username": "u1", "api_key": ""},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    with db.connect(str(tmp_path / "app.db")) as conn:
        row = ka.get_account(conn, account_id)
    assert row["label"] == "renamed"
    assert row["api_key"] == "k1"


def test_toggle_account_flips_disabled(client, tmp_path):
    client.post("/kaggle/accounts", data={"label": "acc1", "username": "u1", "api_key": "k1"}, follow_redirects=False)
    account_id = client.get("/api/ui/kaggle").json()["accounts"][0]["id"]
    client.post(f"/kaggle/accounts/{account_id}/toggle", follow_redirects=False)
    assert client.get("/api/ui/kaggle").json()["accounts"][0]["status"] == "disabled"
    client.post(f"/kaggle/accounts/{account_id}/toggle", follow_redirects=False)
    assert client.get("/api/ui/kaggle").json()["accounts"][0]["status"] == "idle"


def test_delete_refuses_an_account_in_use(client, tmp_path):
    client.post("/kaggle/accounts", data={"label": "acc1", "username": "u1", "api_key": "k1"}, follow_redirects=False)
    account_id = client.get("/api/ui/kaggle").json()["accounts"][0]["id"]
    with db.connect(str(tmp_path / "app.db")) as conn:
        ka.claim_idle_account(conn, job_id=1)
    resp = client.post(f"/kaggle/accounts/{account_id}/delete", follow_redirects=False)
    assert resp.status_code == 400


def test_edit_and_delete_of_unknown_account_returns_404(client):
    assert client.post("/kaggle/accounts/999/edit", data={"label": "x", "username": "x"}, follow_redirects=False).status_code == 404
    assert client.post("/kaggle/accounts/999/delete", follow_redirects=False).status_code == 404
    assert client.post("/kaggle/accounts/999/toggle", follow_redirects=False).status_code == 404


def test_export_batch_kaggle_enqueues_a_job(client, tmp_path):
    book_id, patch_id = _seed_book_and_patch(tmp_path)
    resp = client.post(
        f"/books/{book_id}/patches/export-batch-kaggle",
        data={"patch_ids": [patch_id], "model_id": "zerotts"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "job_id" in body

    from app.jobqueue import store
    with db.connect(str(tmp_path / "app.db")) as conn:
        job = store.get(conn, body["job_id"])
    assert job.job_type == "kaggle_tts"
    assert job.payload["book_id"] == book_id
    assert job.payload["patch_ids"] == [patch_id]
    assert job.payload["model_id"] == "zerotts"


def test_export_batch_kaggle_dedupes_per_book(client, tmp_path):
    book_id, patch_id = _seed_book_and_patch(tmp_path)
    first = client.post(
        f"/books/{book_id}/patches/export-batch-kaggle",
        data={"patch_ids": [patch_id], "model_id": "zerotts"},
    ).json()
    second = client.post(
        f"/books/{book_id}/patches/export-batch-kaggle",
        data={"patch_ids": [patch_id], "model_id": "zerotts"},
    ).json()
    assert first["job_id"] == second["job_id"]


def test_export_batch_kaggle_rejects_unknown_patch(client, tmp_path):
    book_id, _ = _seed_book_and_patch(tmp_path)
    resp = client.post(
        f"/books/{book_id}/patches/export-batch-kaggle",
        data={"patch_ids": [999999], "model_id": "zerotts"},
    )
    assert resp.status_code == 404


def test_book_exports_endpoint_includes_kaggle_accounts(client, tmp_path):
    book_id, _ = _seed_book_and_patch(tmp_path)
    client.post("/kaggle/accounts", data={"label": "acc1", "username": "u1", "api_key": "k1"}, follow_redirects=False)
    data = client.get(f"/api/ui/books/{book_id}/exports").json()
    assert len(data["kaggle_accounts"]) == 1
    assert data["kaggle_accounts"][0]["username"] == "u1"
    assert "remaining_quota_hours" in data["kaggle_accounts"][0]
