"""Tài khoản mạng xã hội dùng chung (app/social_accounts.py): chép từ ba bảng
credentials cũ, tài khoản mặc định, upsert theo external_id, và việc YouTube /
Facebook / TikTok chọn đúng tài khoản khi có nhiều tài khoản."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import db, facebook, shorts_repository, social_accounts, tiktok, youtube

_NOW = datetime.now(timezone.utc).isoformat()


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def _legacy_youtube(conn, channel_id="UClegacy", name="Legacy"):
    conn.execute(
        """INSERT INTO youtube_credentials
           (access_token, refresh_token, token_expiry, channel_id, channel_name, created_at, updated_at)
           VALUES ('at', 'rt', ?, ?, ?, ?, ?)""",
        (_NOW, channel_id, name, _NOW, _NOW),
    )
    conn.commit()


def _youtube(conn, channel_id: str, name: str | None = None) -> int:
    return youtube.save_credentials(conn, access_token=f"at-{channel_id}",
                                    refresh_token=f"rt-{channel_id}", token_expiry=_NOW,
                                    channel_id=channel_id, channel_name=name or channel_id)


# ------------------------------------------------------------------ migration

def test_migration_copies_legacy_rows_once_and_backfills_account_id(conn):
    _legacy_youtube(conn)
    conn.execute("INSERT INTO facebook_credentials (page_id, page_name, page_access_token, created_at, updated_at)"
                 " VALUES ('p1', 'Page', 'fbtoken', ?, ?)", (_NOW, _NOW))
    conn.execute("INSERT INTO tiktok_credentials (open_id, display_name, access_token, created_at, updated_at)"
                 " VALUES ('o1', 'Tik', 'tttoken', ?, ?)", (_NOW, _NOW))
    conn.execute("INSERT INTO youtube_uploads (video_path, title, created_at) VALUES ('/v.mp4', 't', ?)", (_NOW,))
    conn.commit()

    db.init_schema(conn)
    db.init_schema(conn)  # chạy lại ở mỗi lần khởi động: không được nhân đôi

    accounts = {a["platform"]: a for a in social_accounts.list_accounts(conn)}
    assert set(accounts) == {"youtube", "facebook", "tiktok"}
    assert len(social_accounts.list_accounts(conn)) == 3
    assert accounts["youtube"]["external_id"] == "UClegacy"
    assert accounts["youtube"]["refresh_token"] == "rt"
    assert accounts["facebook"]["access_token"] == "fbtoken"
    assert accounts["tiktok"]["external_id"] == "o1"
    assert all(a["is_default"] for a in accounts.values())
    upload = conn.execute("SELECT account_id FROM youtube_uploads").fetchone()
    assert upload["account_id"] == accounts["youtube"]["id"]


def test_legacy_row_is_readable_before_any_migration(conn):
    """Bản sao lưu cũ nạp qua /database-io chỉ có bảng cũ: vẫn phải dùng được ngay."""
    _legacy_youtube(conn)
    creds = youtube.get_creds_from_db(conn)
    assert creds["channel_id"] == "UClegacy" and creds["id"] is None


def test_disconnecting_the_last_account_does_not_resurrect_the_legacy_row(conn):
    _legacy_youtube(conn)
    db.init_schema(conn)
    youtube.delete_credentials(conn)
    assert youtube.get_creds_from_db(conn) is None
    db.init_schema(conn)
    assert youtube.get_creds_from_db(conn) is None


# ------------------------------------------------------------------ default / upsert

def test_first_account_is_default_and_reconnect_updates_in_place(conn):
    first = _youtube(conn, "UC1")
    second = _youtube(conn, "UC2")
    assert social_accounts.get_default(conn, "youtube")["id"] == first
    again = youtube.save_credentials(conn, access_token="new", refresh_token="",
                                     token_expiry=_NOW, channel_id="UC1", channel_name="Renamed")
    assert again == first
    row = social_accounts.get_account(conn, first)
    assert row["access_token"] == "new"
    assert row["refresh_token"] == "rt-UC1"  # refresh_token rỗng không ghi đè
    assert len(social_accounts.list_accounts(conn, "youtube")) == 2
    assert second != first


def test_set_default_and_delete_promotes_another_account(conn):
    first, second = _youtube(conn, "UC1"), _youtube(conn, "UC2")
    social_accounts.set_default(conn, second)
    assert [a["id"] for a in social_accounts.list_accounts(conn, "youtube") if a["is_default"]] == [second]
    social_accounts.delete(conn, second)
    assert social_accounts.get_default(conn, "youtube")["id"] == first
    assert social_accounts.get_account(conn, first)["is_default"] == 1


def test_resolve_refuses_an_account_of_another_platform(conn):
    youtube_id = _youtube(conn, "UC1")
    facebook_id = facebook.save_credentials(conn, page_id="p1", page_name="P", page_access_token="t")
    assert social_accounts.resolve(conn, "youtube", facebook_id) is None
    assert social_accounts.resolve(conn, "youtube", 9999) is None
    assert social_accounts.resolve(conn, "youtube", youtube_id)["id"] == youtube_id


def test_public_view_never_contains_tokens(conn):
    account = social_accounts.get_account(conn, _youtube(conn, "UC1"))
    public = social_accounts.public(account)
    assert "at-UC1" not in str(public) and "rt-UC1" not in str(public)
    assert public["has_refresh_token"] is True


# ------------------------------------------------------------------ YouTube theo tài khoản

def test_use_account_selects_credentials_and_restores_default(conn):
    first, second = _youtube(conn, "UC1"), _youtube(conn, "UC2")
    assert youtube.get_creds_from_db(conn)["channel_id"] == "UC1"
    with youtube.use_account(second):
        assert youtube.get_creds_from_db(conn)["channel_id"] == "UC2"
    assert youtube.get_creds_from_db(conn)["id"] == first


def test_deleted_active_account_reads_as_not_connected(conn):
    _youtube(conn, "UC1")
    second = _youtube(conn, "UC2")
    social_accounts.delete(conn, second)
    with youtube.use_account(second):
        # Không rơi về kênh mặc định: đăng nhầm kênh tệ hơn báo chưa kết nối.
        assert youtube.get_creds_from_db(conn) is None


def test_enqueue_upload_pins_the_account(conn):
    first, second = _youtube(conn, "UC1"), _youtube(conn, "UC2")

    def account_of(upload_id):
        return conn.execute("SELECT account_id FROM youtube_uploads WHERE id=?",
                            (upload_id,)).fetchone()["account_id"]

    assert account_of(youtube.enqueue_upload(conn, "/v.mp4", "default")) == first
    assert account_of(youtube.enqueue_upload(conn, "/v.mp4", "explicit", account_id=second)) == second
    with youtube.use_account(second):
        assert account_of(youtube.enqueue_upload(conn, "/v.mp4", "active")) == second


def test_upload_functions_run_as_the_uploads_account(conn):
    _youtube(conn, "UC1")
    second = _youtube(conn, "UC2")
    upload_id = youtube.enqueue_upload(conn, "/v.mp4", "t", account_id=second)

    @youtube._with_upload_account
    def channel_seen(conn, upload_id):
        return youtube.get_creds_from_db(conn)["channel_id"]

    assert channel_seen(conn, upload_id) == "UC2"
    assert youtube.active_account_id() is None


def test_list_uploads_is_scoped_to_the_active_account(conn):
    first, second = _youtube(conn, "UC1"), _youtube(conn, "UC2")
    youtube.enqueue_upload(conn, "/a.mp4", "a", account_id=first)
    youtube.enqueue_upload(conn, "/b.mp4", "b", account_id=second)
    assert {u["title"] for u in youtube.list_uploads(conn)} == {"a", "b"}
    with youtube.use_account(second):
        assert [u["title"] for u in youtube.list_uploads(conn)] == ["b"]


def test_channel_video_cache_is_kept_per_account(conn):
    first, second = _youtube(conn, "UC1"), _youtube(conn, "UC2")
    for video_id, account in (("v1", first), ("v2", second)):
        conn.execute("INSERT INTO youtube_channel_videos (video_id, title, synced_at, account_id)"
                     " VALUES (?, ?, ?, ?)", (video_id, video_id, _NOW, account))
    conn.commit()
    assert [v["video_id"] for v in youtube.list_cached_channel_videos(conn)["items"]] == ["v1"]
    with youtube.use_account(second):
        assert [v["video_id"] for v in youtube.list_cached_channel_videos(conn)["items"]] == ["v2"]
        assert youtube.channel_videos_sync_status(conn)["count"] == 1


def test_book_channel_is_used_for_its_patch_uploads(conn):
    _youtube(conn, "UC1")
    second = _youtube(conn, "UC2")
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                             created_at, updated_at, youtube_account_id)
           VALUES (1, 't', 'f.epub', '/tmp/f.epub', 10, 'ready', ?, ?, ?)""", (_NOW, _NOW, second))
    patch_id = conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                              created_at, updated_at) VALUES (1, 0, 0, 0, 'done', ?, ?)""",
        (_NOW, _NOW)).lastrowid
    conn.commit()
    upload_id = youtube.enqueue_upload(conn, "/v.mp4", "t", render_source_type="patch",
                                       render_source_id=patch_id)
    row = conn.execute("SELECT account_id FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert row["account_id"] == second
    with youtube.use_book_account(conn, 1):
        assert youtube.get_creds_from_db(conn)["channel_id"] == "UC2"
    # Kênh của sách bị ngắt -> quay về mặc định thay vì chặn cả sách.
    social_accounts.delete(conn, second)
    with youtube.use_book_account(conn, 1):
        assert youtube.get_creds_from_db(conn)["channel_id"] == "UC1"


# ------------------------------------------------------------------ Facebook / TikTok

def test_facebook_and_tiktok_pick_the_requested_account(conn, monkeypatch):
    page_a = facebook.save_credentials(conn, page_id="pA", page_name="A", page_access_token="tokA")
    page_b = facebook.save_credentials(conn, page_id="pB", page_name="B", page_access_token="tokB")
    tik_a = tiktok.save_credentials(conn, open_id="oA", access_token="tA")
    tik_b = tiktok.save_credentials(conn, open_id="oB", access_token="tB")
    assert facebook.get_credentials(conn)["page_id"] == "pA"
    assert facebook.get_credentials(conn, page_b)["page_access_token"] == "tokB"
    assert tiktok.get_credentials(conn, tik_b)["access_token"] == "tB"
    assert tiktok.get_credentials(conn)["id"] == tik_a
    assert facebook.get_credentials(conn, tik_a) is None

    seen = {}
    monkeypatch.setattr(facebook, "upload_reel", lambda **kw: seen.update(fb=kw["token"]) or "fb1")
    monkeypatch.setattr(tiktok, "upload_file", lambda **kw: seen.update(tt=kw["token"]) or "tt1")
    facebook.publish_short_video(conn, "/v.mp4", "cap", account_id=page_b)
    tiktok.publish_short_video(conn, "/v.mp4", "cap", account_id=tik_b)
    assert seen == {"fb": "tokB", "tt": "tB"}
    assert page_a != page_b


def test_short_upload_account_choice_is_stored_per_channel(conn):
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                             created_at, updated_at)
           VALUES (1, 't', 'f.epub', '/tmp/f.epub', 10, 'ready', ?, ?)""", (_NOW, _NOW))
    short_id = conn.execute("INSERT INTO shorts (book_id, created_at, updated_at) VALUES (1, ?, ?)",
                            (_NOW, _NOW)).lastrowid
    conn.commit()
    shorts_repository.ensure_uploads(conn, short_id)
    page = facebook.save_credentials(conn, page_id="pA", page_name="A", page_access_token="tokA")
    shorts_repository.set_upload_account(conn, short_id, "fb", page)
    assert shorts_repository.get_upload(conn, short_id, "fb").account_id == page
    assert shorts_repository.get_upload(conn, short_id, "tiktok").account_id is None
