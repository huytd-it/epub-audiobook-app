"""Unit tests for the channel-videos local cache (app/youtube.py).

The Videos-kênh tab browses youtube_channel_videos - a snapshot built by
sync_channel_videos() - rather than calling the YouTube API on every keystroke.
These tests seed that table directly and exercise the read side
(list_cached_channel_videos / channel_videos_sync_status / get_cached_channel_videos)
plus the pure helpers (_parse_iso8601_duration) and the best-effort cache mirrors used
by the playlist bulk-add/remove functions.
"""
from __future__ import annotations

import json

from app import db
from app import youtube as yt


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "channel.db"))
    db.init_schema(conn)
    return conn


def _seed_video(conn, video_id, *, title="", description="", privacy_status="private",
                playlist_ids=None, published_at="2026-01-01T00:00:00Z", view_count=0,
                duration_sec=60, synced_at="2026-01-01T00:00:00Z"):
    conn.execute(
        "INSERT INTO youtube_channel_videos "
        "(video_id, title, description, tags, privacy_status, category_id, thumbnail, "
        " duration_sec, view_count, published_at, playlist_ids, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (video_id, title, description, "[]", privacy_status, None, None,
         duration_sec, view_count, published_at, json.dumps(playlist_ids or []), synced_at),
    )
    conn.commit()


# --------------------------------------------------------------------- duration parsing


def test_parse_iso8601_duration_handles_hours_minutes_seconds():
    assert yt._parse_iso8601_duration("PT1H2M3S") == 3723
    assert yt._parse_iso8601_duration("PT45S") == 45
    assert yt._parse_iso8601_duration("PT5M") == 300
    assert yt._parse_iso8601_duration(None) is None
    assert yt._parse_iso8601_duration("") is None
    assert yt._parse_iso8601_duration("not-a-duration") is None


# --------------------------------------------------------------------- list/filter/sort/paginate


def test_list_cached_channel_videos_search_filters_title_and_description(tmp_path):
    conn = _conn(tmp_path)
    _seed_video(conn, "v1", title="Chương 1", description="mô tả A")
    _seed_video(conn, "v2", title="Khác hẳn", description="chứa Chương ở mô tả")
    _seed_video(conn, "v3", title="Không liên quan", description="không liên quan")

    result = yt.list_cached_channel_videos(conn, search="Chương")
    ids = {item["video_id"] for item in result["items"]}
    assert ids == {"v1", "v2"}
    assert result["total"] == 2


def test_list_cached_channel_videos_privacy_and_has_playlist_filters(tmp_path):
    conn = _conn(tmp_path)
    _seed_video(conn, "pub", privacy_status="public", playlist_ids=["PL1"])
    _seed_video(conn, "priv", privacy_status="private", playlist_ids=[])

    only_public = yt.list_cached_channel_videos(conn, privacy_status="public")
    assert [i["video_id"] for i in only_public["items"]] == ["pub"]

    has_playlist = yt.list_cached_channel_videos(conn, has_playlist="yes")
    assert [i["video_id"] for i in has_playlist["items"]] == ["pub"]

    no_playlist = yt.list_cached_channel_videos(conn, has_playlist="no")
    assert [i["video_id"] for i in no_playlist["items"]] == ["priv"]


def test_list_cached_channel_videos_playlist_id_filter_matches_membership(tmp_path):
    conn = _conn(tmp_path)
    _seed_video(conn, "in_pl", playlist_ids=["PL1", "PL2"])
    _seed_video(conn, "other_pl", playlist_ids=["PL2"])
    _seed_video(conn, "no_pl", playlist_ids=[])

    result = yt.list_cached_channel_videos(conn, playlist_id="PL1")
    assert [i["video_id"] for i in result["items"]] == ["in_pl"]
    # playlist_ids round-trips as a real list, not a JSON string
    assert result["items"][0]["playlist_ids"] == ["PL1", "PL2"]


def test_list_cached_channel_videos_sort_and_pagination(tmp_path):
    conn = _conn(tmp_path)
    for i in range(5):
        _seed_video(conn, f"v{i}", title=f"Video {i}", view_count=i)

    page1 = yt.list_cached_channel_videos(conn, sort="view_count", order="asc", page=1, page_size=2)
    assert [i["video_id"] for i in page1["items"]] == ["v0", "v1"]
    assert page1["total"] == 5

    page2 = yt.list_cached_channel_videos(conn, sort="view_count", order="asc", page=2, page_size=2)
    assert [i["video_id"] for i in page2["items"]] == ["v2", "v3"]

    desc = yt.list_cached_channel_videos(conn, sort="view_count", order="desc", page=1, page_size=1)
    assert [i["video_id"] for i in desc["items"]] == ["v4"]


def test_list_cached_channel_videos_rejects_unknown_sort_column_safely(tmp_path):
    conn = _conn(tmp_path)
    _seed_video(conn, "v1")
    # An unknown `sort` value falls back to the default column instead of raising or
    # building unsafe SQL from it.
    result = yt.list_cached_channel_videos(conn, sort="'; DROP TABLE youtube_channel_videos; --")
    assert result["total"] == 1


def test_channel_videos_sync_status_reports_count_and_latest_sync(tmp_path):
    conn = _conn(tmp_path)
    assert yt.channel_videos_sync_status(conn) == {"count": 0, "synced_at": None}
    _seed_video(conn, "v1", synced_at="2026-01-01T00:00:00Z")
    _seed_video(conn, "v2", synced_at="2026-01-02T00:00:00Z")
    status = yt.channel_videos_sync_status(conn)
    assert status == {"count": 2, "synced_at": "2026-01-02T00:00:00Z"}


def test_get_cached_channel_videos_returns_only_requested_ids(tmp_path):
    conn = _conn(tmp_path)
    _seed_video(conn, "v1")
    _seed_video(conn, "v2")
    _seed_video(conn, "v3")
    rows = yt.get_cached_channel_videos(conn, ["v1", "v3", "missing"])
    assert {r["video_id"] for r in rows} == {"v1", "v3"}
    assert yt.get_cached_channel_videos(conn, []) == []


# --------------------------------------------------------------------- cache mirror helpers


def test_cache_add_and_remove_video_from_playlist_updates_membership(tmp_path):
    conn = _conn(tmp_path)
    _seed_video(conn, "v1", playlist_ids=["PL1"])

    yt._cache_add_video_to_playlists(conn, "v1", "PL2")
    row = conn.execute("SELECT playlist_ids FROM youtube_channel_videos WHERE video_id='v1'").fetchone()
    assert json.loads(row["playlist_ids"]) == ["PL1", "PL2"]

    yt._cache_remove_video_from_playlist(conn, "v1", "PL1")
    row = conn.execute("SELECT playlist_ids FROM youtube_channel_videos WHERE video_id='v1'").fetchone()
    assert json.loads(row["playlist_ids"]) == ["PL2"]


def test_cache_mirror_helpers_are_a_safe_no_op_without_a_real_connection():
    # The playlist-management unit tests call bulk_add_to_playlist/bulk_remove_from_playlist
    # with conn=None (get_youtube_service is monkeypatched to ignore it); the cache
    # mirror must not raise in that case.
    yt._cache_add_video_to_playlists(None, "v1", "PL1")
    yt._cache_remove_video_from_playlist(None, "v1", "PL1")
    yt._commit_if_real(None)


def test_cache_mirror_helpers_ignore_a_video_not_in_the_cache(tmp_path):
    conn = _conn(tmp_path)
    # No row for "missing" - should not raise, and should not insert one either.
    yt._cache_add_video_to_playlists(conn, "missing", "PL1")
    assert conn.execute("SELECT COUNT(*) c FROM youtube_channel_videos").fetchone()["c"] == 0


# --------------------------------------------------------------------- sync_channel_videos


class _FakeService:
    """Fake of just the playlistItems surface sync_channel_videos touches."""

    def __init__(self, uploads_items):
        self._uploads_items = uploads_items
        self.list_calls = []

    def playlistItems(self):
        api = self

        class _Res:
            def list(self, **params):
                return _Call(api, params)

        return _Res()


class _Call:
    def __init__(self, service, params):
        self._service, self._params = service, params

    def execute(self):
        service = self._service
        service.list_calls.append(dict(self._params))
        assert self._params["playlistId"] == "UUchan"
        return {
            "items": service._uploads_items,
            "nextPageToken": None,
            "pageInfo": {"totalResults": len(service._uploads_items)},
        }


def _raw_playlist_item(video_id):
    return {
        "id": f"PI_{video_id}",
        "snippet": {
            "playlistId": "UUchan",
            "position": 0,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
            "title": f"Title {video_id}",
            "thumbnails": {},
        },
        "contentDetails": {},
    }


def test_sync_channel_videos_does_not_scan_playlists(tmp_path, monkeypatch):
    """The whole point: membership comes from the local cache, so the only
    playlistItems.list the sync issues is for the channel's uploads playlist."""
    conn = _conn(tmp_path)
    _seed_video(conn, "v_old", playlist_ids=["PL1", "PL2"])
    _seed_video(conn, "v_gone", playlist_ids=["PL1"])  # removed from the channel

    uploads = [_raw_playlist_item("v_old"), _raw_playlist_item("v_new")]
    service = _FakeService(uploads)
    monkeypatch.setattr(yt, "get_youtube_service", lambda conn: service)
    monkeypatch.setattr(yt, "_require_google_imports", lambda: None)
    monkeypatch.setattr(yt, "_video_details_by_id", lambda service, ids: {})

    result = yt.sync_channel_videos(conn, "UCchan")

    assert service.list_calls, "uploads playlist must be listed"
    assert all(c["playlistId"] == "UUchan" for c in service.list_calls), (
        "no per-playlist membership scan may happen"
    )
    assert result["playlists_scanned"] == 0
    assert result["synced"] == 2

    rows = {r["video_id"]: r for r in conn.execute("SELECT * FROM youtube_channel_videos")}
    # membership carried forward from the previous snapshot
    assert json.loads(rows["v_old"]["playlist_ids"]) == ["PL1", "PL2"]
    # a brand-new video starts with no membership until a mutation mirrors it
    assert json.loads(rows["v_new"]["playlist_ids"]) == []
    # a video no longer on the channel is gone from the cache
    assert "v_gone" not in rows


def test_sync_channel_videos_membership_reset_when_cleared(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _seed_video(conn, "v1", playlist_ids=["PL1"])

    service = _FakeService([_raw_playlist_item("v1")])
    monkeypatch.setattr(yt, "get_youtube_service", lambda conn: service)
    monkeypatch.setattr(yt, "_require_google_imports", lambda: None)
    monkeypatch.setattr(yt, "_video_details_by_id", lambda service, ids: {})

    # Documented rebuild path: clear the membership column, then sync.
    conn.execute("UPDATE youtube_channel_videos SET playlist_ids='[]'")
    conn.commit()
    yt.sync_channel_videos(conn, "UCchan")

    row = conn.execute("SELECT playlist_ids FROM youtube_channel_videos WHERE video_id='v1'").fetchone()
    assert json.loads(row["playlist_ids"]) == []


def test_single_add_and_remove_mirror_into_the_cache(tmp_path, monkeypatch):
    """add_video_to_playlist / remove_video_from_playlist keep the membership column
    in step so sync_channel_videos can carry it forward without re-scanning."""
    conn = _conn(tmp_path)
    _seed_video(conn, "v1", playlist_ids=[])

    class _SingleItemService:
        def playlistItems(self):
            api = self

            class _Res:
                def list(self, **params):
                    return _Page(api, params)

                def insert(self, **body):
                    return _Insert()

                def delete(self, **kw):
                    return _Delete()

            return _Res()

        def _all(self):
            return [_raw_playlist_item("v1")]

    class _Page:
        def __init__(self, service, params):
            self._service, self._params = service, params

        def execute(self):
            self._service.list_calls.append(dict(self._params))
            return {
                "items": self._service._all(),
                "nextPageToken": None,
                "pageInfo": {"totalResults": 1},
            }

    class _Insert:
        def execute(self):
            return _raw_playlist_item("v1")

    class _Delete:
        def execute(self):
            return {}

    service = _SingleItemService()
    service.list_calls = []
    monkeypatch.setattr(yt, "get_youtube_service", lambda conn: service)
    monkeypatch.setattr(yt, "_require_google_imports", lambda: None)
    monkeypatch.setattr(yt, "MIN_REQUEST_INTERVAL", 0)

    yt.add_video_to_playlist(conn, "PL9", "v1")
    row = conn.execute("SELECT playlist_ids FROM youtube_channel_videos WHERE video_id='v1'").fetchone()
    assert json.loads(row["playlist_ids"]) == ["PL9"]

    assert yt.remove_video_from_playlist(conn, "PL9", "v1") is True
    row = conn.execute("SELECT playlist_ids FROM youtube_channel_videos WHERE video_id='v1'").fetchone()
    assert json.loads(row["playlist_ids"]) == []
