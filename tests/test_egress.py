"""Lớp egress (app/egress.py): policy theo scope, chọn điểm thoát (xoay vòng / sticky /
ghim theo tài khoản), cooldown + đổi điểm thoát khi hỏng, viết lại request cho relay,
và việc không để lộ mật khẩu proxy / secret của relay ra API."""
from __future__ import annotations

import pytest
import requests

from app import db, egress, social_accounts


class FakeResponse:
    def __init__(self, status_code=200, text="{}", headers=None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def _proxy(conn, n: int, **kwargs) -> int:
    return egress.create_endpoint(conn, kind="proxy", label=f"p{n}",
                                  url=f"http://user:secret{n}@10.0.0.{n}:8080", **kwargs)


def _relay(conn, n: int) -> int:
    return egress.create_endpoint(conn, kind="relay", label=f"r{n}",
                                  url=f"https://relay{n}.example.app/", secret=f"key{n}")


def _account(conn, platform: str, external_id: str) -> int:
    return social_accounts.upsert(conn, platform, external_id=external_id, access_token="t")


# ------------------------------------------------------------------ policy

def test_default_policy_is_direct_for_every_scope(conn):
    policy = egress.get_policy(conn)
    assert set(policy) == set(egress.SCOPES)
    assert all(rule["mode"] == "direct" for rule in policy.values())
    assert egress.pick(conn, "ai") is None
    assert egress.pick(conn, "youtube", account_id=1) is None


def test_social_scopes_reject_relay(conn):
    relay_id = _relay(conn, 1)
    with pytest.raises(ValueError, match="relay"):
        egress.set_policy(conn, {"youtube": {"mode": "relay", "endpoint_ids": [relay_id]}})
    assert egress.get_policy(conn)["youtube"]["mode"] == "direct"
    # scope ai thì được.
    assert egress.set_policy(conn, {"ai": {"mode": "relay"}})["ai"]["mode"] == "relay"


def test_policy_rejects_endpoint_of_the_wrong_kind(conn):
    relay_id = _relay(conn, 1)
    with pytest.raises(ValueError, match="không phải loại proxy"):
        egress.set_policy(conn, {"ai": {"mode": "proxy", "endpoint_ids": [relay_id]}})
    with pytest.raises(ValueError, match="không tồn tại"):
        egress.set_policy(conn, {"ai": {"mode": "proxy", "endpoint_ids": [999]}})


def test_endpoint_validation(conn):
    with pytest.raises(ValueError):
        egress.create_endpoint(conn, kind="proxy", url="http://no-port.example")
    with pytest.raises(ValueError):
        egress.create_endpoint(conn, kind="proxy", url="ftp://host:21")
    with pytest.raises(ValueError):
        egress.create_endpoint(conn, kind="relay", url="http://plain.example", secret="k")
    with pytest.raises(ValueError, match="secret"):
        egress.create_endpoint(conn, kind="relay", url="https://relay.example")


# ------------------------------------------------------------------ chọn điểm thoát

def test_non_direct_scope_without_endpoints_fails_closed(conn):
    egress.set_policy(conn, {"ai": {"mode": "proxy"}})
    with pytest.raises(egress.EgressError):
        egress.pick(conn, "ai")


def test_round_robin_cycles_through_endpoints(conn):
    ids = [_proxy(conn, n) for n in (1, 2, 3)]
    egress.set_policy(conn, {"ai": {"mode": "proxy", "strategy": "round_robin"}})
    picked = [egress.pick(conn, "ai").id for _ in range(6)]
    assert picked[:3] == ids
    assert picked[3:] == ids


def test_policy_endpoint_ids_restrict_the_pool(conn):
    first, second, _third = (_proxy(conn, n) for n in (1, 2, 3))
    egress.set_policy(conn, {"ai": {"mode": "proxy", "endpoint_ids": [first, second]}})
    assert {egress.pick(conn, "ai").id for _ in range(6)} == {first, second}


def test_disabled_endpoint_is_never_picked(conn):
    first = _proxy(conn, 1)
    second = _proxy(conn, 2, enabled=False)
    egress.set_policy(conn, {"ai": {"mode": "proxy"}})
    assert {egress.pick(conn, "ai").id for _ in range(4)} == {first}
    assert second not in {egress.pick(conn, "ai").id for _ in range(4)}


def test_cooling_endpoint_is_skipped_then_used_as_last_resort(conn):
    first, second = _proxy(conn, 1), _proxy(conn, 2)
    egress.set_policy(conn, {"ai": {"mode": "proxy"}})
    egress.mark_failed(conn, egress.get_endpoint(conn, first), "boom")
    assert {egress.pick(conn, "ai").id for _ in range(4)} == {second}
    # Mọi endpoint đều đang nghỉ: vẫn chọn một cái thay vì bỏ cuộc.
    egress.mark_failed(conn, egress.get_endpoint(conn, second), "boom")
    assert egress.pick(conn, "ai").id in {first, second}


def test_mark_failed_backs_off_and_mark_ok_clears(conn):
    endpoint_id = _proxy(conn, 1)
    egress.mark_failed(conn, egress.get_endpoint(conn, endpoint_id), "one")
    first = egress.get_endpoint(conn, endpoint_id)
    egress.mark_failed(conn, first, "two")
    second = egress.get_endpoint(conn, endpoint_id)
    assert (first.fail_count, second.fail_count) == (1, 2)
    assert second.cooldown_until > first.cooldown_until
    assert second.last_error == "two"
    egress.mark_ok(conn, second)
    cleared = egress.get_endpoint(conn, endpoint_id)
    assert (cleared.fail_count, cleared.cooldown_until, cleared.last_error) == (0, None, None)


def test_sticky_gives_each_account_one_stable_endpoint(conn):
    for n in (1, 2, 3):
        _proxy(conn, n)
    egress.set_policy(conn, {"youtube": {"mode": "proxy", "strategy": "sticky"}})
    accounts = [_account(conn, "youtube", f"UC{n}") for n in range(3)]
    chosen = {account: egress.pick(conn, "youtube", account).id for account in accounts}
    for account in accounts:
        assert {egress.pick(conn, "youtube", account).id for _ in range(5)} == {chosen[account]}
    assert len(set(chosen.values())) == 3


def test_sticky_account_keeps_its_endpoint_while_it_cools_down(conn):
    for n in (1, 2):
        _proxy(conn, n)
    egress.set_policy(conn, {"youtube": {"mode": "proxy", "strategy": "sticky"}})
    account = _account(conn, "youtube", "UC1")
    endpoint = egress.pick(conn, "youtube", account)
    egress.mark_failed(conn, endpoint, "blip")
    assert egress.pick(conn, "youtube", account).id == endpoint.id


def test_pinned_endpoint_wins_over_policy_pool(conn):
    first, second = _proxy(conn, 1), _proxy(conn, 2)
    egress.set_policy(conn, {"youtube": {"mode": "proxy", "endpoint_ids": [first]}})
    account = _account(conn, "youtube", "UC1")
    social_accounts.update(conn, account, egress_endpoint_id=second)
    assert {egress.pick(conn, "youtube", account).id for _ in range(3)} == {second}


def test_unusable_pinned_endpoint_fails_instead_of_switching_ip(conn):
    first, second = _proxy(conn, 1), _proxy(conn, 2)
    egress.set_policy(conn, {"youtube": {"mode": "proxy"}})
    account = _account(conn, "youtube", "UC1")
    social_accounts.update(conn, account, egress_endpoint_id=second)
    egress.update_endpoint(conn, second, enabled=False)
    with pytest.raises(egress.EgressError):
        egress.pick(conn, "youtube", account)
    assert first != second


def test_deleting_an_endpoint_unpins_accounts_and_cleans_policy(conn):
    first, second = _proxy(conn, 1), _proxy(conn, 2)
    egress.set_policy(conn, {"youtube": {"mode": "proxy", "endpoint_ids": [first, second]}})
    account = _account(conn, "youtube", "UC1")
    social_accounts.update(conn, account, egress_endpoint_id=second)
    assert egress.delete_endpoint(conn, second) is True
    assert social_accounts.get_account(conn, account)["egress_endpoint_id"] is None
    assert egress.get_policy(conn)["youtube"]["endpoint_ids"] == [first]


# ------------------------------------------------------------------ gửi request

def test_direct_request_calls_requests_method_untouched(conn, monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda url, **kw: calls.append((url, kw)) or FakeResponse())
    egress.request("ai", "POST", "https://api.example/v1", conn=conn, json={"a": 1}, timeout=5)
    assert calls == [("https://api.example/v1", {"json": {"a": 1}, "timeout": 5})]


def test_request_without_conn_or_configured_db_goes_direct(monkeypatch):
    monkeypatch.setattr(egress, "_db_path", None)
    monkeypatch.setattr(requests, "get", lambda url, **kw: FakeResponse(text="direct"))
    assert egress.request("ai", "GET", "https://api.example/").text == "direct"


def test_proxy_request_passes_proxies(conn, monkeypatch):
    endpoint_id = _proxy(conn, 1)
    egress.set_policy(conn, {"ai": {"mode": "proxy"}})
    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr(requests, "request", fake_request)
    egress.request("ai", "POST", "https://api.example/v1", conn=conn, timeout=5)
    proxy_url = egress.get_endpoint(conn, endpoint_id).url
    assert seen["url"] == "https://api.example/v1"
    assert seen["proxies"] == {"http": proxy_url, "https": proxy_url}


def test_relay_request_is_rewritten_with_target_and_key(conn, monkeypatch):
    _relay(conn, 1)
    egress.set_policy(conn, {"ai": {"mode": "relay"}})
    seen = {}

    def fake_request(method, url, **kwargs):
        seen.update(method=method, url=url, **kwargs)
        return FakeResponse()

    monkeypatch.setattr(requests, "request", fake_request)
    egress.request("ai", "POST", "https://api.example/v1/chat", conn=conn,
                   headers={"Authorization": "Bearer k"}, params={"alt": "json"}, json={"a": 1})
    # Dấu / cuối URL relay đã được bỏ lúc lưu.
    assert seen["url"] == "https://relay1.example.app/api/relay"
    assert seen["headers"]["x-relay-target"] == "https://api.example/v1/chat?alt=json"
    assert seen["headers"]["x-relay-key"] == "key1"
    assert seen["headers"]["Authorization"] == "Bearer k"
    assert seen["json"] == {"a": 1}
    assert "params" not in seen and "proxies" not in seen


def test_connection_error_rotates_to_the_next_endpoint(conn, monkeypatch):
    first, second = _proxy(conn, 1), _proxy(conn, 2)
    egress.set_policy(conn, {"ai": {"mode": "proxy"}})
    used = []

    def fake_request(method, url, **kwargs):
        used.append(kwargs["proxies"]["https"])
        if len(used) == 1:
            raise requests.ConnectionError("proxy down")
        return FakeResponse(text="ok")

    monkeypatch.setattr(requests, "request", fake_request)
    assert egress.request("ai", "GET", "https://api.example/", conn=conn).text == "ok"
    assert len(set(used)) == 2
    assert egress.get_endpoint(conn, first).fail_count == 1
    assert egress.get_endpoint(conn, second).fail_count == 0


def test_geo_block_rotates_but_ordinary_4xx_is_returned_as_is(conn, monkeypatch):
    first, _second = _proxy(conn, 1), _proxy(conn, 2)
    egress.set_policy(conn, {"ai": {"mode": "proxy"}})
    responses = [
        FakeResponse(400, '{"error": "User location is not supported for the API use."}'),
        FakeResponse(401, '{"error": "invalid api key"}'),
    ]
    monkeypatch.setattr(requests, "request", lambda method, url, **kw: responses.pop(0))
    response = egress.request("ai", "POST", "https://api.example/", conn=conn)
    # Lượt 1 bị chặn theo vùng -> đổi endpoint; lượt 2 là lỗi thật của provider (key sai)
    # nên trả về nguyên vẹn và không phạt endpoint đó.
    assert response.status_code == 401
    assert egress.get_endpoint(conn, first).fail_count == 1


def test_all_endpoints_down_raises_the_last_connection_error(conn, monkeypatch):
    for n in (1, 2):
        _proxy(conn, n)
    egress.set_policy(conn, {"ai": {"mode": "proxy"}})

    def fake_request(method, url, **kwargs):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "request", fake_request)
    with pytest.raises(requests.ConnectionError):
        egress.request("ai", "GET", "https://api.example/", conn=conn)


def test_session_carries_proxy_and_ignores_environment(conn):
    endpoint_id = _proxy(conn, 1)
    assert egress.session(conn, "facebook").proxies == {}
    egress.set_policy(conn, {"facebook": {"mode": "proxy"}})
    http = egress.session(conn, "facebook")
    proxy_url = egress.get_endpoint(conn, endpoint_id).url
    assert http.proxies == {"http": proxy_url, "https": proxy_url}
    assert http.trust_env is False


# ------------------------------------------------------------------ không lộ bí mật

def test_public_view_masks_proxy_password_and_relay_secret(conn):
    proxy = egress.public(egress.get_endpoint(conn, _proxy(conn, 1)))
    relay = egress.public(egress.get_endpoint(conn, _relay(conn, 1)))
    assert proxy["url"] == "http://user:***@10.0.0.1:8080"
    assert "secret1" not in str(proxy)
    assert relay["has_secret"] is True
    assert "key1" not in str(relay)
    assert "secret" not in relay


def test_update_keeps_stored_url_and_secret_when_blank(conn):
    relay_id = _relay(conn, 1)
    egress.update_endpoint(conn, relay_id, label="renamed", url="", secret="")
    endpoint = egress.get_endpoint(conn, relay_id)
    assert (endpoint.label, endpoint.url, endpoint.secret) == (
        "renamed", "https://relay1.example.app", "key1")
