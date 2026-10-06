"""Route tests for the Socials hub (app/routes/socials.py) and for per-request account
selection on the YouTube routes (X-Social-Account header / ?account_id=)."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, facebook, social_accounts
from app import youtube as youtube_module
from app.routes import shorts as shorts_routes
from app.routes import socials as socials_routes
from app.routes import youtube as youtube_routes

_NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(youtube_module, "is_configured", lambda: True)
    conn = db.connect(str(tmp_path / "socials.db"))
    db.init_schema(conn)
    app = FastAPI()
    app.include_router(socials_routes.router)
    app.include_router(youtube_routes.router)
    app.include_router(shorts_routes.router)
    app.state.conn = conn
    app.state.db_lock = threading.Lock()
    yield SimpleNamespace(conn=conn, client=TestClient(app))
    conn.close()


def _youtube(conn, channel_id: str) -> int:
    return youtube_module.save_credentials(
        conn, access_token=f"at-{channel_id}", refresh_token=f"rt-{channel_id}",
        token_expiry=_NOW, channel_id=channel_id, channel_name=f"Channel {channel_id}")


# ------------------------------------------------------------------ tài khoản

def test_overview_lists_accounts_per_platform_without_tokens(ctx):
    _youtube(ctx.conn, "UC1")
    facebook.save_credentials(ctx.conn, page_id="p1", page_name="Page", page_access_token="fb-secret")
    response = ctx.client.get("/socials/api/overview")
    assert response.status_code == 200
    body = response.json()
    assert [a["external_id"] for a in body["youtube"]["accounts"]] == ["UC1"]
    assert body["facebook"]["accounts"][0]["label"] == "Page"
    assert body["tiktok"]["accounts"] == []
    assert "fb-secret" not in response.text and "at-UC1" not in response.text


def test_default_rename_and_delete_account(ctx):
    first, second = _youtube(ctx.conn, "UC1"), _youtube(ctx.conn, "UC2")
    assert ctx.client.post(f"/socials/api/accounts/{second}/default").json()["is_default"] is True
    assert social_accounts.get_default(ctx.conn, "youtube")["id"] == second
    renamed = ctx.client.patch(f"/socials/api/accounts/{first}", json={"label": "Kênh phụ"})
    assert renamed.json()["label"] == "Kênh phụ"
    assert ctx.client.delete(f"/socials/api/accounts/{second}").status_code == 200
    assert social_accounts.get_default(ctx.conn, "youtube")["id"] == first
    assert ctx.client.delete("/socials/api/accounts/9999").status_code == 404


def test_pinning_requires_an_existing_proxy_and_null_unpins(ctx):
    account = _youtube(ctx.conn, "UC1")
    proxy = ctx.client.post("/socials/api/network/endpoints", json={
        "kind": "proxy", "label": "sg", "url": "http://u:pw@10.0.0.1:8080"}).json()
    relay = ctx.client.post("/socials/api/network/endpoints", json={
        "kind": "relay", "url": "https://relay.example.app", "secret": "k"}).json()

    assert ctx.client.patch(f"/socials/api/accounts/{account}",
                            json={"egress_endpoint_id": relay["id"]}).status_code == 400
    pinned = ctx.client.patch(f"/socials/api/accounts/{account}",
                              json={"egress_endpoint_id": proxy["id"]})
    assert pinned.json()["egress_endpoint_id"] == proxy["id"]
    # Đổi nhãn không được làm mất proxy đang ghim.
    assert ctx.client.patch(f"/socials/api/accounts/{account}",
                            json={"label": "x"}).json()["egress_endpoint_id"] == proxy["id"]
    assert ctx.client.patch(f"/socials/api/accounts/{account}",
                            json={"egress_endpoint_id": None}).json()["egress_endpoint_id"] is None


# ------------------------------------------------------------------ Facebook / TikTok

def test_add_facebook_page_verifies_token_before_saving(ctx, monkeypatch):
    monkeypatch.setattr(facebook, "fetch_page",
                        lambda **kw: {"id": kw["page_id"], "name": "Trang Sách Nói"})
    response = ctx.client.post("/socials/api/facebook/accounts",
                               json={"page_id": "123", "page_access_token": "EAAG-page-secret"})
    assert response.status_code == 200
    assert response.json()["label"] == "Trang Sách Nói"
    assert "EAAG-page-secret" not in response.text
    assert facebook.get_credentials(ctx.conn)["page_access_token"] == "EAAG-page-secret"


def test_rejected_facebook_token_is_not_saved_and_not_echoed(ctx, monkeypatch):
    def refuse(**kwargs):
        error = requests.HTTPError("400 for url: https://graph.facebook.com/x?access_token=tok-leak")
        error.response = SimpleNamespace(
            status_code=400, json=lambda: {"error": {"message": "Invalid OAuth access token."}})
        raise error

    monkeypatch.setattr(facebook, "fetch_page", refuse)
    response = ctx.client.post("/socials/api/facebook/accounts",
                               json={"page_id": "123", "page_access_token": "tok-leak"})
    assert response.status_code == 400
    assert "Invalid OAuth access token." in response.json()["detail"]
    assert "tok-leak" not in response.text
    assert social_accounts.list_accounts(ctx.conn, "facebook") == []


def test_tiktok_accounts_are_keyed_by_open_id(ctx):
    first = ctx.client.post("/socials/api/tiktok/accounts",
                            json={"open_id": "o1", "access_token": "t1", "display_name": "A"}).json()
    second = ctx.client.post("/socials/api/tiktok/accounts",
                             json={"open_id": "o2", "access_token": "t2"}).json()
    again = ctx.client.post("/socials/api/tiktok/accounts",
                            json={"open_id": "o1", "access_token": "t1-new"}).json()
    assert again["id"] == first["id"] != second["id"]
    assert len(social_accounts.list_accounts(ctx.conn, "tiktok")) == 2
    assert ctx.client.post("/socials/api/tiktok/accounts", json={"access_token": ""}).status_code == 422


def test_account_upload_history_attributes_unassigned_rows_to_the_default(ctx):
    page_a = facebook.save_credentials(ctx.conn, page_id="pA", page_name="A", page_access_token="t")
    page_b = facebook.save_credentials(ctx.conn, page_id="pB", page_name="B", page_access_token="t")
    ctx.conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                             created_at, updated_at)
           VALUES (1, 'Truyện', 'f.epub', '/tmp/f.epub', 10, 'ready', ?, ?)""", (_NOW, _NOW))
    for short_id, account in ((1, None), (2, page_b)):
        ctx.conn.execute("INSERT INTO shorts (id, book_id, created_at, updated_at) VALUES (?, 1, ?, ?)",
                         (short_id, _NOW, _NOW))
        ctx.conn.execute(
            "INSERT INTO short_uploads (short_id, platform, status, created_at, account_id)"
            " VALUES (?, 'fb', 'done', ?, ?)", (short_id, _NOW, account))
    ctx.conn.commit()

    def history(account_id):
        items = ctx.client.get(f"/socials/api/facebook/accounts/{account_id}/uploads").json()["items"]
        return [item["short_id"] for item in items]

    assert history(page_a) == [1]
    assert history(page_b) == [2]
    assert ctx.client.get(f"/socials/api/tiktok/accounts/{page_a}/uploads").status_code == 404


def test_publish_short_validates_and_stores_per_channel_accounts(ctx):
    page = facebook.save_credentials(ctx.conn, page_id="pA", page_name="A", page_access_token="t")
    channel = _youtube(ctx.conn, "UC1")
    ctx.conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                             created_at, updated_at)
           VALUES (1, 't', 'f.epub', '/tmp/f.epub', 10, 'ready', ?, ?)""", (_NOW, _NOW))
    ctx.conn.execute("INSERT INTO shorts (id, book_id, video_path, created_at, updated_at)"
                     " VALUES (1, 1, '/v.mp4', ?, ?)", (_NOW, _NOW))
    ctx.conn.commit()

    # Tài khoản YouTube không thể dùng cho kênh Facebook.
    wrong = ctx.client.post("/shorts/1/publish", json={"accounts": {"fb": channel}})
    assert wrong.status_code == 400

    ok = ctx.client.post("/shorts/1/publish", json={"accounts": {"fb": page, "youtube": None}})
    assert ok.status_code == 200
    rows = {r["platform"]: r["account_id"] for r in
            ctx.conn.execute("SELECT platform, account_id FROM short_uploads WHERE short_id=1")}
    assert rows == {"fb": page, "tiktok": None, "youtube": None}


# ------------------------------------------------------------------ mạng: relay / proxy

def test_network_endpoints_never_return_secrets(ctx):
    ctx.client.post("/socials/api/network/endpoints", json={
        "kind": "proxy", "label": "sg", "url": "http://user:hunter2@10.0.0.1:8080", "region": "sg"})
    ctx.client.post("/socials/api/network/endpoints", json={
        "kind": "relay", "url": "https://relay.example.app", "secret": "relay-secret"})
    response = ctx.client.get("/socials/api/network")
    assert response.status_code == 200
    assert "hunter2" not in response.text and "relay-secret" not in response.text
    body = response.json()
    assert [e["kind"] for e in body["endpoints"]] == ["proxy", "relay"]
    assert body["endpoints"][0]["url"] == "http://user:***@10.0.0.1:8080"
    assert body["relay_scopes"] == ["ai"]


def test_invalid_endpoint_and_policy_are_rejected(ctx):
    assert ctx.client.post("/socials/api/network/endpoints",
                           json={"kind": "proxy", "url": "not a url"}).status_code == 400
    relay = ctx.client.post("/socials/api/network/endpoints", json={
        "kind": "relay", "url": "https://relay.example.app", "secret": "k"}).json()
    refused = ctx.client.put("/socials/api/network/policy",
                             json={"youtube": {"mode": "relay", "endpoint_ids": [relay["id"]]}})
    assert refused.status_code == 400
    accepted = ctx.client.put("/socials/api/network/policy",
                              json={"ai": {"mode": "relay", "endpoint_ids": [relay["id"]]}})
    assert accepted.json()["policy"]["ai"] == {
        "mode": "relay", "strategy": "round_robin", "endpoint_ids": [relay["id"]]}


def test_endpoint_test_reports_exit_ip_and_records_failures(ctx, monkeypatch):
    endpoint = ctx.client.post("/socials/api/network/endpoints", json={
        "kind": "proxy", "url": "http://10.0.0.1:8080"}).json()
    monkeypatch.setattr(socials_routes.egress, "test_endpoint",
                        lambda endpoint: {"ok": True, "ip": "203.0.113.7", "elapsed_ms": 42})
    assert ctx.client.post(f"/socials/api/network/endpoints/{endpoint['id']}/test").json()["ip"] == "203.0.113.7"

    monkeypatch.setattr(socials_routes.egress, "test_endpoint",
                        lambda endpoint: {"ok": False, "error": "ProxyError: refused"})
    assert ctx.client.post(f"/socials/api/network/endpoints/{endpoint['id']}/test").json()["ok"] is False
    listed = ctx.client.get("/socials/api/network").json()["endpoints"][0]
    assert listed["fail_count"] == 1 and listed["last_error"] == "ProxyError: refused"
    assert ctx.client.post("/socials/api/network/endpoints/9999/test").status_code == 404


# ------------------------------------------------------------------ chọn tài khoản trên route YouTube

def test_youtube_routes_follow_the_selected_account(ctx, monkeypatch):
    first, second = _youtube(ctx.conn, "UC1"), _youtube(ctx.conn, "UC2")
    seen = []

    def fake_list_playlists(conn, max_results=50):
        seen.append(youtube_module.get_creds_from_db(conn)["channel_id"])
        return []

    monkeypatch.setattr(youtube_module, "list_playlists", fake_list_playlists)
    assert ctx.client.get("/youtube/api/playlists").status_code == 200
    assert ctx.client.get("/youtube/api/playlists",
                          headers={"X-Social-Account": str(second)}).status_code == 200
    assert ctx.client.get(f"/youtube/api/playlists?account_id={second}").status_code == 200
    # Request kế tiếp không gửi gì thì lại về mặc định: lựa chọn không dính sang request sau.
    assert ctx.client.get("/youtube/api/playlists").status_code == 200
    assert seen == ["UC1", "UC2", "UC2", "UC1"]
    assert first != second


def test_unknown_selected_account_is_auth_required_not_default(ctx):
    _youtube(ctx.conn, "UC1")
    response = ctx.client.get("/youtube/api/playlists", headers={"X-Social-Account": "9999"})
    assert response.status_code == 401
    assert response.json()["detail"]["code"] == "auth_required"


def test_disconnect_removes_only_the_selected_account(ctx):
    first, second = _youtube(ctx.conn, "UC1"), _youtube(ctx.conn, "UC2")
    response = ctx.client.post("/youtube/disconnect", headers={"X-Social-Account": str(second)})
    assert response.status_code == 200
    assert [a["id"] for a in social_accounts.list_accounts(ctx.conn, "youtube")] == [first]
