"""Google Drive API đi qua egress scope ``drive`` (server 2) khi scope đặt proxy;
mặc định (direct) vẫn dựng client y như cũ."""
from __future__ import annotations

import pytest

from app import db, egress, google_drive


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def _account(conn) -> int:
    return google_drive.save_credentials(conn, "tok", "refresh", "", "a@example.com")


def _proxy_drive(conn, monkeypatch) -> egress.Endpoint:
    endpoint_id = egress.create_endpoint(conn, kind="proxy", label="LAN",
                                         url="socks5h://u:p@192.168.1.20:1080")
    egress.set_policy(conn, {"drive": {"mode": "proxy"}})
    monkeypatch.setattr(egress, "_reachable", lambda endpoint, timeout=2.0: True)
    return egress.get_endpoint(conn, endpoint_id)


def test_direct_policy_builds_the_client_exactly_as_before(conn, monkeypatch):
    account_id = _account(conn)
    seen = {}
    monkeypatch.setattr(google_drive, "build", lambda *a, **kw: seen.update(args=a, **kw) or "svc")
    assert google_drive.get_drive_service(conn, account_id) == "svc"
    assert seen["args"] == ("drive", "v3")
    assert "credentials" in seen and "http" not in seen


def test_proxy_policy_routes_the_client_through_the_proxy(conn, monkeypatch):
    account_id = _account(conn)
    _proxy_drive(conn, monkeypatch)
    seen = {}
    monkeypatch.setattr(google_drive, "build", lambda *a, **kw: seen.update(kw) or "svc")
    google_drive.get_drive_service(conn, account_id)
    authorized = seen["http"]
    info = authorized.http.proxy_info
    assert (info.proxy_host, info.proxy_port) == ("192.168.1.20", 1080)
    assert "credentials" not in seen


def test_token_refresh_uses_a_proxied_session(conn, monkeypatch):
    endpoint = _proxy_drive(conn, monkeypatch)
    request = google_drive._auth_request(conn)
    assert request.session.proxies == {"http": endpoint.url, "https": endpoint.url}
    assert request.session.trust_env is False


def test_unreachable_server_raises_egress_unavailable(conn, monkeypatch):
    account_id = _account(conn)
    _proxy_drive(conn, monkeypatch)
    monkeypatch.setattr(egress, "_reachable", lambda endpoint, timeout=2.0: False)
    monkeypatch.setattr(google_drive, "build", lambda *a, **kw: pytest.fail("không được đi thẳng"))
    with pytest.raises(egress.EgressUnavailable):
        google_drive.get_drive_service(conn, account_id)


def test_kaggle_credentials_are_unchanged_by_the_proxy(conn, monkeypatch):
    account_id = _account(conn)
    _proxy_drive(conn, monkeypatch)
    creds = google_drive.kaggle_credentials(conn, account_id)
    assert creds["refresh_token"] == "refresh"
    assert set(creds) == {"client_id", "client_secret", "refresh_token"}
