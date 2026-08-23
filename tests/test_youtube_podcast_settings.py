"""Tests for YouTube podcast settings: the playlist flag and its 1:1 cover art.

A YouTube podcast is just a playlist with `status.podcastStatus = enabled` plus
a square hero image (playlistImages). Both are show-level settings, so the
interesting behaviour is *not* re-sending them for every published episode —
that is what the recorded state in youtube_podcast_state is for.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from PIL import Image

from app import db as app_db
from app import youtube as yt
from app.youtube_metadata import (DEFAULT_BOOK_YOUTUBE_CONFIG, get_book_youtube_config,
                                  save_book_youtube_config, validate_book_youtube_config)


# ---------------------------------------------------------------------------
# Fake API
# ---------------------------------------------------------------------------


class _Call:
    def __init__(self, api, method, kwargs):
        self._api, self._method, self._kwargs = api, method, kwargs

    def execute(self):
        return getattr(self._api, f"_handle_{self._method}")(**self._kwargs)


class FakePlaylists:
    def __init__(self, playlists):
        self.playlists = playlists
        self.updates = []

    def list(self, **kwargs):
        return _Call(self, "list", kwargs)

    def update(self, **kwargs):
        return _Call(self, "update", kwargs)

    def _handle_list(self, **params):
        found = [p for p in self.playlists if p["id"] == params.get("id")]
        return {"items": found}

    def _handle_update(self, **params):
        body = params["body"]
        self.updates.append(body)
        for playlist in self.playlists:
            if playlist["id"] == body["id"]:
                playlist["snippet"] = {**playlist["snippet"], **body.get("snippet", {})}
                playlist["status"] = {**playlist["status"], **body.get("status", {})}
                return playlist
        raise AssertionError(f"unknown playlist {body['id']}")


class FakePlaylistImages:
    def __init__(self, images=None):
        self.images = list(images or [])
        self.inserts = []
        self.updates = []

    def list(self, **kwargs):
        return _Call(self, "list", kwargs)

    def insert(self, **kwargs):
        return _Call(self, "insert", kwargs)

    def update(self, **kwargs):
        return _Call(self, "update", kwargs)

    def _handle_list(self, **params):
        return {"items": [i for i in self.images if i["snippet"]["playlistId"] == params["parent"]]}

    def _handle_insert(self, **params):
        self.inserts.append(params)
        image = {"id": f"IMG{len(self.images) + 1}", "snippet": dict(params["body"]["snippet"])}
        self.images.append(image)
        return image

    def _handle_update(self, **params):
        self.updates.append(params)
        return {"id": params["body"]["id"], "snippet": dict(params["body"]["snippet"])}


class FakeService:
    def __init__(self, playlists=None, images=None):
        self._playlists = FakePlaylists(playlists if playlists is not None else [_playlist("PL1")])
        self._images = FakePlaylistImages(images)

    def playlists(self):
        return self._playlists

    def playlistImages(self):
        return self._images


def _playlist(playlist_id, *, title="Sách Test", description="mô tả", privacy="unlisted", podcast=None):
    status = {"privacyStatus": privacy}
    if podcast:
        status["podcastStatus"] = podcast
    return {"id": playlist_id, "snippet": {"title": title, "description": description}, "status": status}


@pytest.fixture
def service(monkeypatch):
    fake = FakeService()
    monkeypatch.setattr(yt, "_require_google_imports", lambda: None)
    monkeypatch.setattr(yt, "get_youtube_service", lambda conn: fake)
    return fake


@pytest.fixture
def conn():
    connection = app_db.connect(":memory:")
    app_db.init_schema(connection)
    now = datetime.now(timezone.utc).isoformat()
    connection.execute(
        "INSERT INTO book (id, title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES (1, 'Sách Test', 'f.epub', '/tmp/f.epub', 10, 'ready', ?, ?)",
        (now, now),
    )
    connection.commit()
    yield connection
    connection.close()


def _cover(tmp_path, color=(200, 30, 30), name="cover.png"):
    path = tmp_path / name
    Image.new("RGB", (400, 400), color).save(str(path), "PNG")
    return path


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


def test_podcast_section_defaults_to_off():
    config = validate_book_youtube_config({})
    assert config["podcast"] == {"enabled": False, "upload_cover": True}
    assert DEFAULT_BOOK_YOUTUBE_CONFIG["podcast"]["enabled"] is False


def test_podcast_section_rejects_non_booleans():
    with pytest.raises(ValueError, match="podcast.enabled"):
        validate_book_youtube_config({"podcast": {"enabled": "yes"}})
    with pytest.raises(ValueError, match="podcast must be an object"):
        validate_book_youtube_config({"podcast": "on"})


def test_podcast_section_round_trips_through_the_book_config(conn):
    save_book_youtube_config(conn, 1, {
        "playlist": {"mode": "existing", "playlist_id": "PL1"},
        "podcast": {"enabled": True, "upload_cover": False},
    })
    stored = get_book_youtube_config(conn, 1)
    assert stored["podcast"] == {"enabled": True, "upload_cover": False}


def test_a_patch_override_dropping_the_playlist_does_not_break_podcast_books():
    """Podcast needs a playlist, but that is enforced when publishing, not here."""
    config = validate_book_youtube_config({
        "playlist": {"mode": "none", "playlist_id": ""},
        "podcast": {"enabled": True, "upload_cover": True},
    })
    assert config["podcast"]["enabled"] is True


# ---------------------------------------------------------------------------
# Single API operations
# ---------------------------------------------------------------------------


def test_set_playlist_podcast_keeps_the_existing_snippet(service, conn):
    yt.set_playlist_podcast(conn, "PL1", True)
    body = service._playlists.updates[-1]
    assert body["status"]["podcastStatus"] == "enabled"
    # playlists.update replaces whole parts: title/description/privacy must survive.
    assert body["snippet"] == {"title": "Sách Test", "description": "mô tả"}
    assert body["status"]["privacyStatus"] == "unlisted"


def test_set_playlist_podcast_can_turn_it_off(service, conn):
    yt.set_playlist_podcast(conn, "PL1", False)
    assert service._playlists.updates[-1]["status"]["podcastStatus"] == "disabled"


def test_set_playlist_podcast_reports_a_missing_playlist(service, conn):
    with pytest.raises(ValueError, match="not found"):
        yt.set_playlist_podcast(conn, "PL404", True)


def test_set_playlist_cover_inserts_the_first_hero_image(service, conn, tmp_path):
    yt.set_playlist_cover(conn, "PL1", str(_cover(tmp_path)))
    assert len(service._images.inserts) == 1
    snippet = service._images.inserts[0]["body"]["snippet"]
    assert snippet == {"playlistId": "PL1", "type": "hero"}
    assert service._images.inserts[0]["media_body"] is not None


def test_set_playlist_cover_replaces_an_existing_hero_image(service, conn, tmp_path):
    service._images.images.append({"id": "IMG9", "snippet": {"playlistId": "PL1", "type": "hero"}})
    yt.set_playlist_cover(conn, "PL1", str(_cover(tmp_path)))
    assert not service._images.inserts
    assert service._images.updates[0]["body"]["id"] == "IMG9"


def test_set_playlist_cover_requires_the_file(service, conn, tmp_path):
    with pytest.raises(FileNotFoundError):
        yt.set_playlist_cover(conn, "PL1", str(tmp_path / "missing.png"))


# ---------------------------------------------------------------------------
# sync_playlist_podcast
# ---------------------------------------------------------------------------


def test_sync_applies_the_flag_and_the_cover_once(service, conn, tmp_path):
    cover = _cover(tmp_path)
    first = yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=str(cover))
    assert first == {"playlist_id": "PL1", "podcast": "enabled", "cover": "uploaded", "changed": True}

    second = yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=str(cover))
    assert second["changed"] is False
    assert len(service._playlists.updates) == 1
    assert len(service._images.inserts) == 1


def test_sync_uploads_cover_before_enabling_podcast(service, conn, tmp_path, monkeypatch):
    cover = _cover(tmp_path)
    calls = []
    original_cover = yt.set_playlist_cover
    original_podcast = yt.set_playlist_podcast

    def record_cover(*args, **kwargs):
        calls.append("cover")
        return original_cover(*args, **kwargs)

    def record_podcast(*args, **kwargs):
        calls.append("podcast")
        return original_podcast(*args, **kwargs)

    monkeypatch.setattr(yt, "set_playlist_cover", record_cover)
    monkeypatch.setattr(yt, "set_playlist_podcast", record_podcast)

    yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=str(cover))

    assert calls == ["cover", "podcast"]


def test_sync_reuploads_when_the_cover_art_changes(service, conn, tmp_path):
    cover = _cover(tmp_path)
    yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=str(cover))
    Image.new("RGB", (400, 400), (10, 200, 10)).save(str(cover), "PNG")

    result = yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=str(cover))
    assert result["cover"] == "uploaded"
    assert result["podcast"] == "unchanged"
    assert len(service._playlists.updates) == 1, "cờ podcast không đổi thì không gọi lại"


def test_sync_force_resends_everything(service, conn, tmp_path):
    cover = _cover(tmp_path)
    yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=str(cover))
    result = yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=str(cover), force=True)
    assert result["changed"] is True
    assert len(service._playlists.updates) == 2
    assert len(service._images.inserts) + len(service._images.updates) == 2


def test_sync_without_a_cover_only_sets_the_flag(service, conn):
    result = yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=None)
    assert result["cover"] == "disabled"
    assert not service._images.inserts
    assert service._playlists.updates


def test_sync_reports_a_missing_cover_file(service, conn, tmp_path):
    result = yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=str(tmp_path / "nope.png"))
    assert result["cover"] == "missing"
    assert not service._images.inserts


def test_sync_disabling_skips_the_cover(service, conn, tmp_path):
    cover = _cover(tmp_path)
    yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=str(cover))
    result = yt.sync_playlist_podcast(conn, 1, "PL1", enabled=False, cover_path=str(cover))
    assert result["podcast"] == "disabled" and result["cover"] == "skipped"
    assert service._playlists.updates[-1]["status"]["podcastStatus"] == "disabled"


def test_sync_records_the_state_per_playlist(service, conn, tmp_path):
    cover = _cover(tmp_path)
    yt.sync_playlist_podcast(conn, 1, "PL1", enabled=True, cover_path=str(cover))
    state = yt.get_podcast_state(conn, 1, "PL1")
    assert state["podcast_status"] == "enabled" and state["cover_sha"]

    # A different playlist for the same book starts from scratch.
    service._playlists.playlists.append(_playlist("PL2"))
    result = yt.sync_playlist_podcast(conn, 1, "PL2", enabled=True, cover_path=str(cover))
    assert result["changed"] is True


def test_sync_requires_a_playlist_id(conn):
    with pytest.raises(ValueError, match="playlist_id"):
        yt.sync_playlist_podcast(conn, 1, "", enabled=True)


# ---------------------------------------------------------------------------
# Auto-apply after an episode is published
# ---------------------------------------------------------------------------


def test_apply_book_podcast_does_nothing_when_the_book_opted_out(service, conn):
    save_book_youtube_config(conn, 1, {"playlist": {"mode": "existing", "playlist_id": "PL1"}})
    assert yt.apply_book_podcast(conn, 1, "PL1") is None
    assert not service._playlists.updates


def test_apply_book_podcast_syncs_an_opted_in_book(service, conn, tmp_path, monkeypatch):
    from app import image_overlay

    save_book_youtube_config(conn, 1, {
        "playlist": {"mode": "existing", "playlist_id": "PL1"},
        "podcast": {"enabled": True, "upload_cover": True},
    })
    cover = _cover(tmp_path)
    monkeypatch.setattr(image_overlay, "ensure_podcast_cover", lambda *a, **k: str(cover))

    result = yt.apply_book_podcast(conn, 1, "PL1")
    assert result["podcast"] == "enabled" and result["cover"] == "uploaded"


def test_apply_book_podcast_never_raises_into_the_publish_path(service, conn, monkeypatch):
    save_book_youtube_config(conn, 1, {
        "playlist": {"mode": "existing", "playlist_id": "PL1"},
        "podcast": {"enabled": True, "upload_cover": False},
    })

    def _boom(*args, **kwargs):
        raise RuntimeError("YouTube nói không")

    monkeypatch.setattr(yt, "set_playlist_podcast", _boom)
    assert yt.apply_book_podcast(conn, 1, "PL1") is None


def test_apply_book_podcast_ignores_a_missing_book_or_playlist(service, conn):
    assert yt.apply_book_podcast(conn, None, "PL1") is None
    assert yt.apply_book_podcast(conn, 1, "") is None


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@pytest.fixture
def route_client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings
    from app.main import app
    from app.routes import video as video_routes

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(video_routes, "_BACKGROUNDS_DIR", tmp_path / "backgrounds")
    with TestClient(app) as client:
        background = tmp_path / "bg.png"
        Image.new("RGB", (640, 360), (12, 12, 40)).save(str(background), "PNG")
        now = datetime.now(timezone.utc).isoformat()
        with app_db.connect(settings.db_path) as db:
            db.execute(
                "INSERT INTO book (id, title, original_filename, epub_path, patch_size, status, "
                "background_image_path, automation_config, created_at, updated_at) "
                "VALUES (1, 'Sách Test', 'f.epub', '/tmp/f.epub', 10, 'ready', ?, ?, ?, ?)",
                (str(background), json.dumps({"youtube": validate_book_youtube_config({
                    "playlist": {"mode": "existing", "playlist_id": "PL1"},
                    "podcast": {"enabled": True, "upload_cover": True},
                })}), now, now),
            )
            db.execute(
                "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, "
                "audio_path, created_at, updated_at) VALUES (1, 0, 0, 5, 'done', '/tmp/a.wav', ?, ?)",
                (now, now),
            )
            db.commit()
        yield client


def test_apply_route_refuses_when_youtube_is_not_connected(route_client, monkeypatch):
    monkeypatch.setattr(yt, "get_creds_from_db", lambda conn: None)
    response = route_client.post("/books/1/podcast/apply")
    assert response.status_code == 400
    assert "Chưa kết nối YouTube" in response.json()["detail"]


def test_apply_route_pushes_the_flag_and_the_cover(route_client, service, monkeypatch):
    monkeypatch.setattr(yt, "get_creds_from_db", lambda conn: {"channel_id": "UC1"})
    response = route_client.post("/books/1/podcast/apply")
    assert response.status_code == 200, response.text
    assert response.json()["podcast"] == "enabled"
    assert response.json()["cover"] == "uploaded"
    assert service._playlists.updates[-1]["status"]["podcastStatus"] == "enabled"


def test_apply_route_needs_a_playlist(route_client, service, monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(yt, "get_creds_from_db", lambda conn: {"channel_id": "UC1"})
    with app_db.connect(settings.db_path) as db:
        db.execute(
            "UPDATE book SET automation_config=? WHERE id=1",
            (json.dumps({"youtube": validate_book_youtube_config({
                "podcast": {"enabled": True, "upload_cover": True},
            })}),),
        )
        db.commit()
    response = route_client.post("/books/1/podcast/apply")
    assert response.status_code == 400
    assert "playlist" in response.json()["detail"].lower()
