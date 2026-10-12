"""Sub folders of the media library and their use as video backgrounds."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import media_library, video_gen
from app.main import app
from app.video_config import validate_video_config


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings
    from app.routes import video as video_routes

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(video_routes, "_BACKGROUNDS_DIR", tmp_path / "backgrounds")
    with TestClient(app) as c:
        yield c


def _folder(tmp_path, name: str, *files: str) -> Path:
    d = tmp_path / "backgrounds" / name
    d.mkdir(parents=True, exist_ok=True)
    for file in files:
        (d / file).write_bytes(b"x")
    return d


def test_expand_backgrounds_appends_folder_files_in_name_order(client, tmp_path):
    d = _folder(tmp_path, "rain", "b.mp4", "a.jpg", "notes.txt")
    explicit = str(tmp_path / "backgrounds" / "single.jpg")

    paths = media_library.expand_backgrounds(
        {"backgrounds": [explicit], "background_folders": ["rain", "missing"]})

    assert paths == [explicit, str(d / "a.jpg"), str(d / "b.mp4")]


def test_reserved_and_traversal_folders_are_ignored(client, tmp_path):
    _folder(tmp_path, "cache", "a.jpg")
    _folder(tmp_path, "patch_bg", "a.jpg")
    _folder(tmp_path, "mine", "a.jpg")

    assert media_library.list_folders() == ["mine"]
    assert media_library.expand_backgrounds({"background_folders": ["cache", "..", "patch_bg"]}) == []


def test_folder_files_cover_the_whole_audio(client, tmp_path):
    d = _folder(tmp_path, "loop", "a.jpg", "b.jpg", "c.jpg")
    files = media_library.expand_backgrounds({"background_folders": ["loop"]})

    plan = video_gen.plan_background_segments(files, 100, 15)

    assert sum(length for _, length in plan) == 100
    assert [Path(p).name for p, _ in plan[:4]] == ["a.jpg", "b.jpg", "c.jpg", "a.jpg"]
    assert d.is_dir()


def test_video_config_validates_folder_names():
    assert validate_video_config({"background_folders": ["rain", "rain"]})["background_folders"] == ["rain"]
    with pytest.raises(ValueError):
        validate_video_config({"background_folders": ["../etc"]})


def test_folder_routes_create_upload_move_delete(client, tmp_path):
    assert client.post("/photos/folders/create", data={"name": "Rain"}).json() == {"name": "Rain"}
    assert client.post("/photos/folders/create", data={"name": "Rain"}).status_code == 400
    assert client.post("/photos/folders/create", data={"name": "cache"}).status_code == 400

    resp = client.post("/photos/upload", data={"folder": "Rain"},
                       files={"files": ("in.png", b"png", "image/png")}, follow_redirects=False)
    assert resp.status_code == 303
    root_file = tmp_path / "backgrounds" / "root.png"
    root_file.write_bytes(b"png")

    assert client.post("/photos/move", data={"name": "root.png", "target": "Rain"}).json()["status"] == "moved"
    assert not root_file.exists()
    assert client.get("/photos/file/root.png", params={"folder": "Rain"}).status_code == 200

    listing = client.get("/video/backgrounds").json()
    assert [(f["name"], [x["name"] for x in f["files"]]) for f in listing["folders"]] == [
        ("Rain", ["in.png", "root.png"])]
    media = client.get("/api/ui/media").json()
    assert [f["name"] for f in media["photo_folders"]] == ["Rain"]

    assert client.post("/photos/folders/delete", data={"name": "Rain"}).status_code == 200
    assert not (tmp_path / "backgrounds" / "Rain").exists()


def test_deleting_folder_drops_it_from_book_configs(client, tmp_path):
    from app.config import settings

    d = _folder(tmp_path, "rain", "a.jpg")
    conn = sqlite3.connect(settings.db_path)
    config = {"video": {"backgrounds": [str(d / "a.jpg"), "keep.jpg"], "background_folders": ["rain", "other"]}}
    conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "automation_config, created_at, updated_at) "
        "VALUES ('b', 'b.epub', 'b.epub', 10, 'ready', ?, '2026-01-01', '2026-01-01')",
        (json.dumps(config),))
    conn.commit()

    assert client.post("/photos/folders/delete", data={"name": "rain"}).status_code == 200

    stored = json.loads(conn.execute("SELECT automation_config FROM book").fetchone()[0])["video"]
    conn.close()
    assert stored == {"backgrounds": ["keep.jpg"], "background_folders": ["other"]}


def test_video_backgrounds_play_their_full_length():
    plan = video_gen.plan_background_segments(
        ["clip.mp4", "a.jpg"], 100, 15, clip_durations={"clip.mp4": 40.0})

    assert plan == [("clip.mp4", 40.0), ("a.jpg", 15), ("clip.mp4", 40.0), ("a.jpg", 5)]
