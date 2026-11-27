"""Kho tài khoản trên server 2 (app/account_source.py): nạp metadata Kaggle/YouTube
vào DB, key/token chỉ đọc lúc chạy và không bao giờ ghi vào DB."""
from __future__ import annotations

import base64
import functools
import http.server
import json
import threading
from pathlib import Path

import pytest

from app import account_source, db, egress, kaggle_accounts, social_accounts, youtube

USER, PASSWORD = "egress", "lsts-123-egress"


class _AuthHandler(http.server.SimpleHTTPRequestHandler):
    """File server có liệt kê thư mục + Basic Auth, giống gost `file://user:pass@`."""

    def log_message(self, *args):  # im lặng trong test
        pass

    def do_GET(self):
        expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != expected:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="gost"')
            self.end_headers()
            return
        super().do_GET()


@pytest.fixture
def store_dir(tmp_path):
    root = tmp_path / "accounts"
    (root / "kaggle").mkdir(parents=True)
    (root / "youtube").mkdir()
    (root / "kaggle" / "acc-a.json").write_text(json.dumps({"username": "user_a", "key": "KEY-A"}))
    (root / "kaggle" / "acc-b.json").write_text(json.dumps({"username": "user_b", "key": "KEY-B", "label": "Phụ"}))
    (root / "youtube" / "kenh1.json").write_text(json.dumps(
        {"refresh_token": "RT-1", "channel_id": "UC1", "channel_title": "Kênh 1"}))
    return root


@pytest.fixture
def server(store_dir):
    handler = functools.partial(_AuthHandler, directory=str(store_dir))
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def conn(server):
    connection = db.connect(":memory:")
    db.init_schema(connection)
    account_source.set_config(connection, urls=["http://127.0.0.1:1", server],  # URL đầu chết → failover
                              username=USER, password=PASSWORD)
    yield connection
    connection.close()
    account_source.clear_cache()


def test_sync_imports_metadata_only_and_keys_are_read_at_runtime(conn):
    summary = account_source.sync(conn)
    assert summary["kaggle"]["added"] == 2 and summary["youtube"]["added"] == 1
    rows = {r["username"]: r for r in kaggle_accounts.list_accounts(conn)}
    assert set(rows) == {"user_a", "user_b"}
    assert all(r["api_key"] == "" and r["source"] == "server2" for r in rows.values())   # key không vào DB
    assert rows["user_b"]["label"] == "Phụ" and rows["user_a"]["label"] == "acc-a"
    assert kaggle_accounts.api_ref(conn, rows["user_a"]).api_key == "KEY-A"            # đọc lúc chạy


def test_youtube_channel_tokens_come_from_server_2_not_the_db(conn):
    account_source.sync(conn)
    channel = social_accounts.list_accounts(conn, "youtube")[0]
    assert (channel["external_id"], channel["refresh_token"], channel["access_token"]) == ("UC1", "", "")
    assert social_accounts.public(channel)["source"] == "server2"
    creds = youtube.get_creds_from_db(conn, channel["id"])
    assert creds["refresh_token"] == "RT-1" and creds["remote"] is True


def test_removed_files_are_unlisted_and_local_accounts_are_never_overwritten(conn, store_dir):
    kaggle_accounts.create_account(conn, "local", "user_b", "LOCAL-KEY")
    summary = account_source.sync(conn)
    assert summary["kaggle"]["added"] == 1
    assert any("local" in s["reason"] for s in summary["kaggle"]["skipped"])
    local = next(r for r in kaggle_accounts.list_accounts(conn) if r["username"] == "user_b")
    assert local["source"] == "local" and local["api_key"] == "LOCAL-KEY"

    (store_dir / "kaggle" / "acc-a.json").unlink()
    assert account_source.sync(conn)["kaggle"]["removed"] == 1
    assert [r["username"] for r in kaggle_accounts.list_accounts(conn)] == ["user_b"]


def test_server_down_defers_and_wrong_password_is_a_clear_error(conn, server):
    account_source.sync(conn)
    account = next(r for r in kaggle_accounts.list_accounts(conn) if r["username"] == "user_a")
    account_source.set_config(conn, urls=["http://127.0.0.1:1"], username=USER, password=None)
    with pytest.raises(egress.EgressUnavailable):
        kaggle_accounts.api_ref(conn, account)
    account_source.set_config(conn, urls=[server], username=USER, password="sai-mat-khau")
    with pytest.raises(account_source.AccountSourceError, match="user/pass"):
        kaggle_accounts.api_ref(conn, account)


def test_config_hides_password_and_keeps_it_when_blank(conn):
    public = account_source.public_config(conn)
    assert public["has_password"] is True and "password" not in public
    account_source.set_config(conn, urls=public["urls"], username=USER, password="")
    assert account_source.get_config(conn)["password"] == PASSWORD
    with pytest.raises(ValueError):
        account_source.set_config(conn, urls=["ftp://x"], username=USER, password=None)


def test_duplicate_username_on_server_2_is_skipped_not_fatal(conn, store_dir):
    (store_dir / "kaggle" / "acc-a-copy.json").write_text(json.dumps({"username": "user_a", "key": "KEY-A2"}))
    summary = account_source.sync(conn)
    assert summary["kaggle"]["added"] == 2
    assert any("trùng username" in s["reason"] for s in summary["kaggle"]["skipped"])


def test_plain_kaggle_json_at_the_root_is_picked_up(conn, store_dir):
    (store_dir / "kaggle.json").write_text(json.dumps({"username": "user_root", "key": "KGAT_root"}))
    summary = account_source.sync(conn)
    assert summary["kaggle"]["files"] == 3
    root = next(r for r in kaggle_accounts.list_accounts(conn) if r["username"] == "user_root")
    assert root["label"] == "user_root" and root["remote_ref"] == "kaggle.json"
    assert kaggle_accounts.api_ref(conn, root).api_key == "KGAT_root"


def test_empty_store_reports_zero_files(conn, store_dir):
    for path in list(store_dir.rglob("*.json")):
        path.unlink()
    summary = account_source.sync(conn)
    assert summary["kaggle"]["files"] == 0 and summary["youtube"]["files"] == 0
