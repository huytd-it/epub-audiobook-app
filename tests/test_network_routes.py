"""Trang "Mạng & Server" (app/routes/network.py, /network/api) + alias cũ dưới
/socials/api/network, và cảnh báo kênh đang ghim proxy lệch định tuyến."""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, egress, social_accounts
from app import youtube as youtube_module
from app.routes import network as network_routes
from app.routes import socials as socials_routes

_NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def ctx(tmp_path):
    conn = db.connect(str(tmp_path / "network.db"))
    db.init_schema(conn)
    app = FastAPI()
    app.include_router(socials_routes.router)
    app.include_router(network_routes.router)
    app.state.conn = conn
    app.state.db_lock = threading.Lock()
    yield SimpleNamespace(conn=conn, client=TestClient(app))
    conn.close()


def _proxy(client, base, label, host):
    return client.post(f"{base}/endpoints", json={
        "kind": "proxy", "label": label, "url": f"socks5h://u:pw@{host}:1080"}).json()


def test_new_router_lists_new_scopes_and_masks_passwords(ctx):
    _proxy(ctx.client, "/network/api", "LAN", "192.168.1.20")
    body = ctx.client.get("/network/api").json()
    assert {"kaggle", "drive"} <= set(body["scopes"])
    assert body["policy"]["kaggle"]["mode"] == "direct"
    assert body["relay_scopes"] == ["ai"]
    assert body["endpoints"][0]["url"] == "socks5h://u:***@192.168.1.20:1080"
    assert body["pinned_conflicts"] == []


def test_old_socials_paths_are_aliases_of_the_same_handlers(ctx):
    created = _proxy(ctx.client, "/socials/api/network", "LAN", "192.168.1.20")
    assert ctx.client.get("/network/api").json()["endpoints"][0]["id"] == created["id"]
    policy = ctx.client.put("/socials/api/network/policy", json={
        "youtube": {"mode": "proxy", "strategy": "round_robin", "endpoint_ids": [created["id"]]}})
    assert policy.status_code == 200
    assert ctx.client.get("/network/api").json()["policy"]["youtube"]["mode"] == "proxy"
    assert ctx.client.patch(f"/network/api/endpoints/{created['id']}",
                            json={"label": "LAN-2"}).json()["label"] == "LAN-2"
    assert ctx.client.delete(f"/socials/api/network/endpoints/{created['id']}").status_code == 200
    assert ctx.client.get("/socials/api/network").json()["endpoints"] == []


def test_relay_is_refused_for_kaggle_and_drive(ctx):
    relay = ctx.client.post("/network/api/endpoints", json={
        "kind": "relay", "url": "https://relay.example.app", "secret": "k"}).json()
    for scope in ("kaggle", "drive"):
        refused = ctx.client.put("/network/api/policy", json={
            scope: {"mode": "relay", "endpoint_ids": [relay["id"]]}})
        assert refused.status_code == 400


def test_pinned_conflicts_flag_accounts_outside_the_scope_route(ctx):
    lan = _proxy(ctx.client, "/network/api", "LAN", "192.168.1.20")
    other = _proxy(ctx.client, "/network/api", "Cũ", "10.9.9.9")
    channel = youtube_module.save_credentials(
        ctx.conn, access_token="at", refresh_token="rt", token_expiry=_NOW,
        channel_id="UC1", channel_name="Kênh 1")
    social_accounts.update(ctx.conn, channel, egress_endpoint_id=other["id"])

    # youtube đang direct -> ghim bị bỏ qua -> cảnh báo
    conflicts = ctx.client.get("/network/api").json()["pinned_conflicts"]
    assert [c["account_id"] for c in conflicts] == [channel]
    assert conflicts[0]["platform"] == "youtube" and conflicts[0]["endpoint_label"] == "Cũ"

    # youtube = proxy [LAN] -> endpoint ghim không nằm trong danh sách -> cảnh báo
    egress.set_policy(ctx.conn, {"youtube": {"mode": "proxy", "endpoint_ids": [lan["id"]]}})
    conflicts = ctx.client.get("/network/api").json()["pinned_conflicts"]
    assert conflicts and "không nằm trong" in conflicts[0]["reason"]

    # ghim đúng endpoint của luồng -> hết cảnh báo
    social_accounts.update(ctx.conn, channel, egress_endpoint_id=lan["id"])
    assert ctx.client.get("/network/api").json()["pinned_conflicts"] == []
