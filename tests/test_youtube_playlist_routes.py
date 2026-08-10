"""Route tests for the YouTube playlist management JSON APIs in app/routes/youtube.py.

Covers the API contract of every /youtube/api/playlists* endpoint:

* disconnected / auth CTA (not configured, no stored creds, missing channel id)
* structured errors from the network layer (auth / quota / 404 / 500)
* malformed and empty request bodies
* page-size validation (>50, <1)
* source == destination rejection for copy/move
* pagination token / title query / channel-id-from-creds pass-through
* 1-based -> 0-based position conversion
* playlistItem.id -> video.id mapping for copy/move
* partial-response flagging for bulk operations
* the no-freeze guarantee: every YouTube service call runs off the shared db_lock
  on a throwaway connection, never on request.app.state.conn
"""
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from app import db
from app import youtube as youtube_module
from app.routes import youtube as youtube_routes


def _seed_creds(conn, *, channel_id="UCtest", channel_name="Test Channel"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO youtube_credentials
           (access_token, refresh_token, token_expiry, channel_id, channel_name, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        ("access", "refresh", now, channel_id, channel_name, now, now),
    )
    conn.commit()


@pytest.fixture
def make_client(tmp_path, monkeypatch):
    """Factory yielding a FastAPI app wired to a throwaway file DB.

    Uses a real file database (not :memory:) so _youtube_api_conn opens a genuinely
    separate connection for the service functions instead of falling back to the shared
    one.
    """
    created = []
    monkeypatch.setattr(youtube_module, "is_configured", lambda: True)

    def _make(*, connected=True):
        db_path = str(tmp_path / f"routes{len(created)}.db")
        conn = db.connect(db_path)
        db.init_schema(conn)
        if connected:
            _seed_creds(conn)
        app = FastAPI()
        app.include_router(youtube_routes.router)
        lock = threading.Lock()
        app.state.conn = conn
        app.state.db_lock = lock
        created.append((conn, lock))
        return SimpleNamespace(conn=conn, lock=lock, client=TestClient(app), db_path=db_path)

    yield _make
    for conn, _lock in created:
        conn.close()


class _Probe:
    """Records whether the shared db_lock was held when a network call ran, and which
    connection the service function received."""

    def __init__(self, lock: threading.Lock):
        self.lock = lock
        self.records: list[tuple[bool, object]] = []

    def record(self, conn) -> None:
        acquired = self.lock.acquire(blocking=False)
        held = not acquired
        if acquired:
            self.lock.release()
        self.records.append((held, conn))


def _assert_off_lock(probe: _Probe, shared_conn) -> None:
    assert probe.records, "the mocked YouTube service call never ran"
    for held, conn in probe.records:
        assert held is False, (
            "db_lock was held while a YouTube network call ran - this freezes the whole app"
        )
        assert conn is not shared_conn, (
            "a YouTube network call ran on the shared connection; it must use a throwaway one"
        )


def _patch_service(monkeypatch, probe: _Probe, name: str, result):
    def fake(conn, *args, **kwargs):
        probe.record(conn)
        return result

    monkeypatch.setattr(youtube_module, name, fake)


def _page(items=None, next_token=None, prev_token=None, total=0):
    return {
        "items": items or [],
        "next_page_token": next_token,
        "prev_page_token": prev_token,
        "total": total,
    }


def _batch(*, succeeded=0, failed=0, skipped=0, items=None):
    return {
        "requested": succeeded + failed + skipped,
        "succeeded": succeeded,
        "skipped": skipped,
        "failed": failed,
        "items": items or [],
    }


def _item(playlist_item_id, video_id, playlist_id="PL1", position=0):
    return {
        "playlist_item_id": playlist_item_id,
        "playlist_id": playlist_id,
        "video_id": video_id,
        "title": f"Title {video_id}",
        "thumbnail": None,
        "position": position,
        "published_at": None,
    }


def _source_items():
    return [_item("PI1", "V1"), _item("PI2", "V2")]


# ---------------------------------------------------------------------------
# Disconnected / auth CTA
# ---------------------------------------------------------------------------


def test_not_configured_returns_400_without_auth_cta(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(youtube_module, "is_configured", lambda: False)
    response = c.client.get("/youtube/api/playlists")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "not_configured"
    assert "auth" not in detail


def test_missing_creds_returns_401_with_auth_cta(make_client):
    c = make_client(connected=False)
    response = c.client.get("/youtube/api/playlists")
    assert response.status_code == 401
    detail = response.json()["detail"]
    assert detail["code"] == "auth_required"
    assert detail["auth"]["required"] is True
    assert detail["auth"]["connect_url"] == "/youtube/connect"
    assert detail["auth"]["cta"] == "Connect your YouTube account"


def test_missing_channel_id_returns_400_with_auth_cta(make_client):
    c = make_client()
    c.conn.execute(
        "UPDATE youtube_credentials SET channel_id=NULL, channel_name=NULL"
    )
    c.conn.commit()
    response = c.client.get("/youtube/api/channel/videos")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "validation"
    assert detail["auth"]["required"] is True
    assert detail["auth"]["connect_url"] == "/youtube/connect"


# ---------------------------------------------------------------------------
# Structured network-layer errors (auth / quota / 404 / 500)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status, reason=""):
        self.status = status
        self.reason = reason or ("Not Found" if status == 404 else "Other")


def _quota_http_error(reason="quotaExceeded"):
    content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    return HttpError(_Resp(403, reason), content, "uri")


@pytest.mark.parametrize(
    "exc, expected_status, expected_code, expect_auth",
    [
        (HttpError(_Resp(404), b"{}", "uri"), 404, "not_found", False),
        (_quota_http_error("quotaExceeded"), 429, "quota_exceeded", False),
        (_quota_http_error("rateLimitExceeded"), 429, "quota_exceeded", False),
        (_quota_http_error("forbidden"), 403, "auth_required", True),
        (HttpError(_Resp(403, "Other"), b"{}", "uri"), 403, "forbidden", False),
        (HttpError(_Resp(401), b"{}", "uri"), 401, "auth_required", True),
        (RefreshError("token expired"), 401, "auth_required", True),
        (ValueError("YouTube not connected. Please connect first."), 401, "auth_required", True),
        (RuntimeError("boom"), 500, "internal", False),
    ],
    ids=[
        "http404", "quota_exceeded", "rate_limit", "permission_403",
        "forbidden_403", "http401", "refresh_error", "not_connected_value",
        "internal_500",
    ],
)
def test_network_errors_are_normalized(
    make_client, monkeypatch, exc, expected_status, expected_code, expect_auth
):
    c = make_client()

    def raise_exc(conn, *args, **kwargs):
        raise exc

    monkeypatch.setattr(youtube_module, "list_playlists", raise_exc)
    response = c.client.get("/youtube/api/playlists")

    assert response.status_code == expected_status
    detail = response.json()["detail"]
    assert detail["code"] == expected_code
    assert ("auth" in detail) is expect_auth
    if expect_auth:
        assert detail["auth"]["connect_url"] == "/youtube/connect"
        assert detail["auth"]["cta"]


# ---------------------------------------------------------------------------
# Malformed / empty bodies
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "method, path, body",
    [
        ("post", "/youtube/api/playlists/PL1/items", {}),
        ("post", "/youtube/api/playlists/PL1/items", {"video_ids": []}),
        ("post", "/youtube/api/playlists/PL1/videos", {}),
        ("post", "/youtube/api/playlists/PL1/videos", {"video_ids": []}),
        ("delete", "/youtube/api/playlists/PL1/items", {}),
        ("delete", "/youtube/api/playlists/PL1/items", {"item_ids": []}),
        ("post", "/youtube/api/playlists/PL1/copy", {}),
        ("post", "/youtube/api/playlists/PL1/copy", {"dest_playlist_id": "PL2", "item_ids": []}),
        ("post", "/youtube/api/playlists/PL1/copy", {"dest_playlist_id": "", "item_ids": ["PI1"]}),
        ("post", "/youtube/api/playlists/PL1/move", {"dest_playlist_id": "PL2"}),
        ("post", "/youtube/api/playlists/PL1/move", {"item_ids": ["PI1"]}),
        ("post", "/youtube/api/playlists/PL1/items/IT1/position", {"position": 0}),
        ("post", "/youtube/api/playlists/PL1/reorder", {"positions": {}}),
        ("post", "/youtube/api/playlists/PL1/sort/preview", {"direction": "sideways"}),
        ("post", "/youtube/api/playlists/PL1/sort/apply", {"direction": "up"}),
        ("post", "/youtube/api/playlists/PL1/sort/preview", {"mode": "vibes"}),
        ("post", "/youtube/api/playlists/PL1/reorder-all", {}),
        ("post", "/youtube/api/playlists/PL1/reorder-all", {"item_ids": []}),
    ],
)
def test_malformed_or_empty_body_rejected(make_client, method, path, body):
    c = make_client()
    response = c.client.request(method, path, json=body)
    assert response.status_code == 422, (
        f"{method} {path} body={body} should be a 422 validation error, got "
        f"{response.status_code}: {response.text}"
    )


# ---------------------------------------------------------------------------
# Page-size validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("max_results", [0, 51, 500])
def test_page_size_out_of_range_400(make_client, max_results):
    c = make_client()
    response = c.client.get(f"/youtube/api/playlists?max_results={max_results}")
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "validation"


def test_page_size_out_of_range_400_on_items_and_channel(make_client):
    c = make_client()
    for path in (
        f"/youtube/api/playlists/PL1/items?max_results=51",
        f"/youtube/api/channel/videos?max_results=0",
    ):
        response = c.client.get(path)
        assert response.status_code == 400, path
        assert response.json()["detail"]["code"] == "validation"


def test_page_size_of_50_is_accepted(make_client, monkeypatch):
    c = make_client()
    probe = _Probe(c.lock)
    _patch_service(monkeypatch, probe, "list_playlists", [{"id": "PL1"}])
    response = c.client.get("/youtube/api/playlists?max_results=50")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# source == destination
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ["copy", "move"])
def test_copy_move_source_equals_destination_rejected(make_client, monkeypatch, route):
    c = make_client()
    called = []

    def unexpected(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr(youtube_module, "get_all_playlist_items", unexpected)
    response = c.client.post(
        f"/youtube/api/playlists/PL1/{route}",
        json={"dest_playlist_id": "PL1", "item_ids": ["PI1"]},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "validation"
    assert called == [], "no service call should run before the distinct check"


# ---------------------------------------------------------------------------
# Pagination token / query / channel id
# ---------------------------------------------------------------------------


def test_list_playlist_items_passes_page_token_and_max_results(make_client, monkeypatch):
    c = make_client()
    calls = []

    def fake_list(conn, playlist_id, max_results=50, page_token=None):
        calls.append((playlist_id, max_results, page_token))
        return _page([_item("PI1", "V1")], next_token="NEXT", prev_token="PREV", total=7)

    monkeypatch.setattr(youtube_module, "list_playlist_items", fake_list)
    response = c.client.get("/youtube/api/playlists/PL1/items?max_results=25&page_token=CURSOR")

    assert response.status_code == 200
    assert calls == [("PL1", 25, "CURSOR")]
    body = response.json()
    assert body["playlist_id"] == "PL1"
    assert body["items"][0]["video_id"] == "V1"
    assert body["next_page_token"] == "NEXT"
    assert body["prev_page_token"] == "PREV"
    assert body["total"] == 7
    assert body["count"] == 1


def test_channel_videos_uses_channel_id_from_creds_and_query(make_client, monkeypatch):
    c = make_client()
    calls = []

    def fake_list(conn, channel_id, max_results=50, page_token=None, title_query=None):
        calls.append((channel_id, max_results, page_token, title_query))
        return _page([], next_token="NEXT")

    monkeypatch.setattr(youtube_module, "list_channel_videos", fake_list)
    response = c.client.get("/youtube/api/channel/videos?max_results=10&page_token=CUR&q=hello")

    assert response.status_code == 200
    assert calls == [("UCtest", 10, "CUR", "hello")]
    assert response.json()["next_page_token"] == "NEXT"
    assert response.json()["count"] == 0


# ---------------------------------------------------------------------------
# 1-based -> 0-based position conversion
# ---------------------------------------------------------------------------


def test_position_body_is_converted_from_1_based_to_0_based(make_client, monkeypatch):
    c = make_client()
    calls = []

    def fake_update(conn, playlist_id, playlist_item_id, position, video_id=None):
        calls.append((playlist_id, playlist_item_id, position))
        return {"id": playlist_item_id, "position": position}

    monkeypatch.setattr(youtube_module, "update_playlist_item_position", fake_update)
    response = c.client.post(
        "/youtube/api/playlists/PL1/items/IT1/position", json={"position": 3}
    )

    assert response.status_code == 200
    assert calls == [("PL1", "IT1", 2)]
    assert response.json() == {"id": "IT1", "position": 2}


# ---------------------------------------------------------------------------
# playlistItem.id -> video.id mapping for copy / move
# ---------------------------------------------------------------------------


def test_copy_maps_item_ids_to_video_ids(make_client, monkeypatch):
    c = make_client()
    mapping_calls = []
    copy_calls = []
    monkeypatch.setattr(
        youtube_module, "get_all_playlist_items",
        lambda conn, pid: mapping_calls.append(pid) or _source_items(),
    )
    monkeypatch.setattr(
        youtube_module, "copy_playlist_items",
        lambda conn, src, dst, vids=None, skip_duplicates=True: (
            copy_calls.append((src, dst, vids)) or _batch(succeeded=1)
        ),
    )

    response = c.client.post(
        "/youtube/api/playlists/PL1/copy",
        json={"dest_playlist_id": "PL2", "item_ids": ["PI1", "PI2"]},
    )

    assert response.status_code == 200
    assert mapping_calls == ["PL1"]
    assert copy_calls == [("PL1", "PL2", ["V1", "V2"])]


def test_copy_all_skips_mapping_when_no_item_ids(make_client, monkeypatch):
    c = make_client()
    mapping_calls = []
    copy_calls = []
    monkeypatch.setattr(
        youtube_module, "get_all_playlist_items",
        lambda conn, pid: mapping_calls.append(pid) or _source_items(),
    )
    monkeypatch.setattr(
        youtube_module, "copy_playlist_items",
        lambda conn, src, dst, vids=None, skip_duplicates=True: (
            copy_calls.append((src, dst, vids)) or _batch(succeeded=2)
        ),
    )

    response = c.client.post("/youtube/api/playlists/PL1/copy", json={"dest_playlist_id": "PL2"})

    assert response.status_code == 200
    assert mapping_calls == []
    assert copy_calls == [("PL1", "PL2", None)]


def test_copy_unknown_item_returns_400(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(youtube_module, "get_all_playlist_items", lambda conn, pid: _source_items())
    monkeypatch.setattr(
        youtube_module, "copy_playlist_items",
        lambda *a, **k: pytest.fail("copy must not be called for an unknown item id"),
    )

    response = c.client.post(
        "/youtube/api/playlists/PL1/copy",
        json={"dest_playlist_id": "PL2", "item_ids": ["PI1", "PI9"]},
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["code"] == "validation"
    assert "not found in source playlist" in detail["message"]
    assert "PI9" in detail["message"]


def test_move_maps_item_ids_to_video_ids(make_client, monkeypatch):
    c = make_client()
    move_calls = []
    monkeypatch.setattr(youtube_module, "get_all_playlist_items", lambda conn, pid: _source_items())
    monkeypatch.setattr(
        youtube_module, "move_playlist_items",
        lambda conn, src, dst, vids=None, skip_duplicates=True: (
            move_calls.append((src, dst, vids)) or _batch(succeeded=1)
        ),
    )

    response = c.client.post(
        "/youtube/api/playlists/PL1/move",
        json={"dest_playlist_id": "PL2", "item_ids": ["PI2"]},
    )

    assert response.status_code == 200
    assert move_calls == [("PL1", "PL2", ["V2"])]


def test_reorder_sorts_positions_and_computes_page_index(make_client, monkeypatch):
    c = make_client()
    calls = []
    monkeypatch.setattr(
        youtube_module, "get_all_playlist_items",
        lambda conn, pid: [_item("PI1", "V1", position=3), _item("PI2", "V2", position=4)],
    )
    monkeypatch.setattr(
        youtube_module, "reorder_playlist_page",
        lambda conn, pid, page_index, order, page_size=50: (
            calls.append((pid, page_index, order, page_size)) or _batch(succeeded=2)
        ),
    )

    response = c.client.post(
        "/youtube/api/playlists/PL1/reorder",
        json={"positions": {"PI2": 1, "PI1": 0}},
    )

    assert response.status_code == 200
    assert calls == [("PL1", 0, ["V1", "V2"], 50)]


def test_reorder_all_maps_item_ids_to_video_ids_in_order(make_client, monkeypatch):
    c = make_client()
    calls = []
    monkeypatch.setattr(
        youtube_module, "get_all_playlist_items",
        lambda conn, pid: [_item("PI1", "V1", position=0), _item("PI2", "V2", position=1)],
    )
    monkeypatch.setattr(
        youtube_module, "reorder_playlist",
        lambda conn, pid, order=None, **kw: calls.append((pid, order)) or _batch(succeeded=2),
    )

    response = c.client.post(
        "/youtube/api/playlists/PL1/reorder-all", json={"item_ids": ["PI2", "PI1"]}
    )

    assert response.status_code == 200
    assert calls == [("PL1", ["V2", "V1"])]


def test_reorder_all_unknown_item_returns_400(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(
        youtube_module, "get_all_playlist_items",
        lambda conn, pid: [_item("PI1", "V1", position=0)],
    )
    monkeypatch.setattr(
        youtube_module, "reorder_playlist",
        lambda *a, **k: pytest.fail("reorder must not be called for an unknown item id"),
    )

    response = c.client.post(
        "/youtube/api/playlists/PL1/reorder-all", json={"item_ids": ["PI1", "PI9"]}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "validation"
    assert "PI9" in response.json()["detail"]["message"]


def test_sort_routes_pass_direction_and_mode(make_client, monkeypatch):
    c = make_client()
    calls = []
    for name in ("sort_playlist_preview", "sort_playlist"):
        monkeypatch.setattr(
            youtube_module, name,
            lambda conn, pid, direction="asc", mode="natural", _n=name: (
                calls.append((_n, pid, direction, mode)) or {"items": [], "ordered": []}
            ),
        )

    c.client.post("/youtube/api/playlists/PL1/sort/preview",
                  json={"direction": "desc", "mode": "episode"})
    c.client.post("/youtube/api/playlists/PL1/sort/apply", json={"direction": "asc"})

    assert calls == [
        ("sort_playlist_preview", "PL1", "desc", "episode"),
        # mode defaults to the pre-existing natural-title behaviour
        ("sort_playlist", "PL1", "asc", "natural"),
    ]


def test_list_items_fetch_all_returns_every_item_without_page_tokens(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(
        youtube_module, "get_all_playlist_items",
        lambda conn, pid: [_item(f"PI{i}", f"V{i}", position=i) for i in range(120)],
    )
    monkeypatch.setattr(
        youtube_module, "list_playlist_items",
        lambda *a, **k: pytest.fail("fetch_all must not fall back to the paged call"),
    )

    response = c.client.get("/youtube/api/playlists/PL1/items?fetch_all=1")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 120 and body["total"] == 120
    assert body["next_page_token"] is None and body["prev_page_token"] is None


def test_list_items_fetch_all_ignores_the_page_size_cap(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(youtube_module, "get_all_playlist_items", lambda conn, pid: [])
    response = c.client.get("/youtube/api/playlists/PL1/items?fetch_all=1&max_results=500")
    assert response.status_code == 200


def test_reorder_unknown_item_returns_400(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(
        youtube_module, "get_all_playlist_items",
        lambda conn, pid: [_item("PI1", "V1", position=3)],
    )
    monkeypatch.setattr(
        youtube_module, "reorder_playlist_page",
        lambda *a, **k: pytest.fail("reorder must not be called for an unknown item id"),
    )

    response = c.client.post(
        "/youtube/api/playlists/PL1/reorder", json={"positions": {"PI1": 0, "PI9": 1}}
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "validation"
    assert "PI9" in response.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# Partial response flagging
# ---------------------------------------------------------------------------


def test_bulk_add_marks_partial_on_failure(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(
        youtube_module, "bulk_add_to_playlist",
        lambda conn, pid, vids, **kw: _batch(succeeded=1, failed=1),
    )
    response = c.client.post(
        "/youtube/api/playlists/PL1/items", json={"video_ids": ["V1", "V2"]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["failed"] == 1
    assert body["partial"] is True


def test_bulk_add_marks_partial_false_on_full_success(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(
        youtube_module, "bulk_add_to_playlist",
        lambda conn, pid, vids, **kw: _batch(succeeded=2),
    )
    response = c.client.post(
        "/youtube/api/playlists/PL1/items", json={"video_ids": ["V1", "V2"]}
    )
    assert response.json()["partial"] is False


def test_bulk_remove_marks_partial_on_errors_key(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(
        youtube_module, "bulk_remove_from_playlist",
        lambda conn, pid, **kw: {
            "requested": 1, "succeeded": 0, "failed": 0, "skipped": 0,
            "items": [], "errors": ["something went wrong"],
        },
    )
    response = c.client.request(
        "delete", "/youtube/api/playlists/PL1/items", json={"item_ids": ["PI1"]}
    )
    assert response.status_code == 200
    assert response.json()["partial"] is True


def test_remove_single_item_returns_partial_flag(make_client, monkeypatch):
    c = make_client()
    monkeypatch.setattr(
        youtube_module, "bulk_remove_from_playlist",
        lambda conn, pid, **kw: _batch(succeeded=1),
    )
    response = c.client.delete("/youtube/api/playlist-items/PI1")
    assert response.status_code == 200
    assert response.json()["partial"] is False


# ---------------------------------------------------------------------------
# The no-freeze guarantee: every service call runs off the shared db_lock
# on a throwaway connection
# ---------------------------------------------------------------------------


OFF_LOCK_CASES = [
    ("get", "/youtube/api/playlists", None, "list_playlists", [{"id": "PL1"}]),
    (
        "get", "/youtube/api/playlists/PL1/items", None, "list_playlist_items",
        _page([_item("PI1", "V1")], next_token="N1"),
    ),
    (
        "get", "/youtube/api/channel/videos", None, "list_channel_videos",
        _page([], next_token="N1"),
    ),
    (
        "post", "/youtube/api/playlists/PL1/items", {"video_ids": ["V1"]},
        "bulk_add_to_playlist", _batch(succeeded=1),
    ),
    (
        "post", "/youtube/api/playlists/PL1/videos", {"video_ids": ["V1"]},
        "bulk_add_to_playlist", _batch(succeeded=1),
    ),
    (
        "delete", "/youtube/api/playlists/PL1/items", {"item_ids": ["PI1"]},
        "bulk_remove_from_playlist", _batch(succeeded=1),
    ),
    (
        "delete", "/youtube/api/playlist-items/PI1", None,
        "bulk_remove_from_playlist", _batch(succeeded=1),
    ),
    (
        "post", "/youtube/api/playlists/PL1/items/IT1/position", {"position": 2},
        "update_playlist_item_position", {"id": "IT1"},
    ),
    (
        "post", "/youtube/api/playlists/PL1/sort/preview", {"direction": "asc"},
        "sort_playlist_preview", {"items": []},
    ),
    (
        "post", "/youtube/api/playlists/PL1/sort/apply", {"direction": "desc"},
        "sort_playlist", _batch(succeeded=1),
    ),
]

OFF_LOCK_IDS = [
    "list_playlists",
    "list_playlist_items",
    "list_channel_videos",
    "add_items",
    "add_videos_alias",
    "remove_items",
    "remove_single_item",
    "update_position",
    "sort_preview",
    "sort_apply",
]


@pytest.mark.parametrize("method, path, body, service, result", OFF_LOCK_CASES, ids=OFF_LOCK_IDS)
def test_single_service_calls_run_off_lock_on_throwaway_conn(
    make_client, monkeypatch, method, path, body, service, result
):
    c = make_client()
    probe = _Probe(c.lock)
    _patch_service(monkeypatch, probe, service, result)

    response = c.client.request(method, path, json=body)

    assert response.status_code == 200
    _assert_off_lock(probe, c.conn)


@pytest.mark.parametrize("route", ["copy", "move"], ids=["copy", "move"])
def test_copy_move_service_calls_run_off_lock_on_throwaway_conn(make_client, monkeypatch, route):
    c = make_client()
    probe = _Probe(c.lock)
    monkeypatch.setattr(
        youtube_module, "get_all_playlist_items",
        lambda conn, pid: probe.record(conn) or _source_items(),
    )
    service = "copy_playlist_items" if route == "copy" else "move_playlist_items"
    monkeypatch.setattr(
        youtube_module, service,
        lambda conn, src, dst, vids=None, skip_duplicates=True: probe.record(conn) or _batch(succeeded=1),
    )

    response = c.client.post(
        f"/youtube/api/playlists/PL1/{route}",
        json={"dest_playlist_id": "PL2", "item_ids": ["PI1"]},
    )

    assert response.status_code == 200
    assert len(probe.records) == 2, "copy/move must run a mapping call and the batch call"
    _assert_off_lock(probe, c.conn)


def test_reorder_service_calls_run_off_lock_on_throwaway_conn(make_client, monkeypatch):
    c = make_client()
    probe = _Probe(c.lock)
    monkeypatch.setattr(
        youtube_module, "get_all_playlist_items",
        lambda conn, pid: probe.record(conn) or [_item("PI1", "V1", position=0)],
    )
    monkeypatch.setattr(
        youtube_module, "reorder_playlist_page",
        lambda conn, pid, page_index, order, page_size=50: probe.record(conn) or _batch(succeeded=1),
    )

    response = c.client.post(
        "/youtube/api/playlists/PL1/reorder", json={"positions": {"PI1": 0}}
    )

    assert response.status_code == 200
    assert len(probe.records) == 2
    _assert_off_lock(probe, c.conn)


def test_reorder_all_service_calls_run_off_lock_on_throwaway_conn(make_client, monkeypatch):
    c = make_client()
    probe = _Probe(c.lock)
    monkeypatch.setattr(
        youtube_module, "get_all_playlist_items",
        lambda conn, pid: probe.record(conn) or [_item("PI1", "V1", position=0)],
    )
    monkeypatch.setattr(
        youtube_module, "reorder_playlist",
        lambda conn, pid, order=None, **kw: probe.record(conn) or _batch(succeeded=1),
    )

    response = c.client.post(
        "/youtube/api/playlists/PL1/reorder-all", json={"item_ids": ["PI1"]}
    )

    assert response.status_code == 200
    assert len(probe.records) == 2, "reorder-all must run a mapping call and the reorder call"
    _assert_off_lock(probe, c.conn)
