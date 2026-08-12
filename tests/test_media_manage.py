"""Route tests for the photo manager (/photos) and music rename."""
from __future__ import annotations

import sqlite3
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings
    from app.routes import music as music_routes
    from app.routes import video as video_routes

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    # _MUSIC_DIR / _BACKGROUNDS_DIR are module-level constants computed from the
    # real settings at import time - repoint them so uploads land in the test dir.
    monkeypatch.setattr(music_routes, "_MUSIC_DIR", tmp_path / "music")
    monkeypatch.setattr(video_routes, "_BACKGROUNDS_DIR", tmp_path / "backgrounds")
    with TestClient(app) as c:
        yield c


def _bg_dir(client) -> Path:
    from app.config import settings

    d = Path(settings.data_root) / "backgrounds"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db(client) -> sqlite3.Connection:
    from app.config import settings

    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    return c


def _seed_photo(client, name: str) -> Path:
    p = _bg_dir(client) / name
    p.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    return p


# ---------------------------------------------------------------------------
# Photos page
# ---------------------------------------------------------------------------




def test_photo_upload_creates_file(client, tmp_path):
    img = tmp_path / "up.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    with open(img, "rb") as f:
        resp = client.post(
            "/photos/upload",
            files={"files": ("up.png", f, "image/png")},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    names = [p.name for p in _bg_dir(client).iterdir()]
    assert any(n.endswith("up.png") for n in names)




def test_photo_upload_rejects_bad_extension(client, tmp_path):
    bad = tmp_path / "evil.exe"
    bad.write_bytes(b"MZ")
    with open(bad, "rb") as f:
        resp = client.post(
            "/photos/upload",
            files={"files": ("evil.exe", f, "application/octet-stream")},
            follow_redirects=False,
        )
    assert resp.status_code == 303  # skipped, not rejected


def test_photo_rename_renames_file_and_book_reference(client):
    old = _seed_photo(client, "old.png")

    db = _db(client)
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "background_image_path, created_at, updated_at) "
        "VALUES ('b', 'b.epub', 'b.epub', 10, 'ready', ?, ?, ?)",
        (str(old), now, now),
    )
    db.commit()

    resp = client.post(
        "/photos/rename",
        data={"old_name": "old.png", "new_name": "shiny"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert not old.exists()
    new = _bg_dir(client) / "shiny.png"
    assert new.exists()

    row = db.execute("SELECT background_image_path FROM book").fetchone()
    assert row["background_image_path"] == str(new)
    db.close()


def test_photo_rename_rejects_traversal(client):
    _seed_photo(client, "ok.png")
    for old_name, new_name in [
        ("../secret.png", "x"),
        ("ok.png", "../evil"),
        ("ok.png", "a/b"),
        ("ok.png", ""),
    ]:
        resp = client.post(
            "/photos/rename",
            data={"old_name": old_name, "new_name": new_name},
            follow_redirects=False,
        )
        assert resp.status_code == 400, (old_name, new_name)


def test_photo_rename_missing_file_404(client):
    resp = client.post(
        "/photos/rename",
        data={"old_name": "nope.png", "new_name": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_photo_rename_refuses_overwrite(client):
    _seed_photo(client, "a.png")
    _seed_photo(client, "b.png")
    resp = client.post(
        "/photos/rename",
        data={"old_name": "a.png", "new_name": "b"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_photo_delete_removes_file_and_clears_book_reference(client):
    p = _seed_photo(client, "gone.png")
    db = _db(client)
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "background_image_path, created_at, updated_at) "
        "VALUES ('b', 'b.epub', 'b.epub', 10, 'ready', ?, ?, ?)",
        (str(p), now, now),
    )
    db.commit()

    resp = client.post(
        "/photos/delete", data={"name": "gone.png"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert not p.exists()
    row = db.execute("SELECT background_image_path FROM book").fetchone()
    assert row["background_image_path"] is None
    db.close()


def test_photo_file_served(client):
    _seed_photo(client, "thumb.png")
    resp = client.get("/photos/file/thumb.png")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"


def test_media_rename_updates_patch_and_video_config_references(client):
    old = _seed_photo(client, "old.png")
    db = _db(client)
    now = "2026-01-01T00:00:00+00:00"
    db.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,automation_config,created_at,updated_at) VALUES (1,'b','b.epub','b.epub',10,'ready',?,?,?)", (json.dumps({"video": {"backgrounds": [str(old)], "quality": 20}}), now, now))
    db.execute("INSERT INTO patch (book_id,patch_index,chapter_start,chapter_end,image_path,created_at,updated_at) VALUES (1,0,0,1,?,?,?)", (str(old), now, now))
    db.commit()
    response = client.post("/photos/rename", data={"old_name": "old.png", "new_name": "new"}, follow_redirects=False)
    new = _bg_dir(client) / "new.png"
    row = db.execute("SELECT automation_config FROM book WHERE id=1").fetchone()
    patch = db.execute("SELECT image_path FROM patch WHERE book_id=1").fetchone()
    assert response.status_code == 303
    assert patch["image_path"] == str(new)
    assert json.loads(row["automation_config"])["video"]["backgrounds"] == [str(new)]


def test_media_delete_clears_patch_and_removes_video_config_reference(client):
    target = _seed_photo(client, "gone.png")
    keep = _seed_photo(client, "keep.png")
    db = _db(client)
    now = "2026-01-01T00:00:00+00:00"
    db.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,automation_config,created_at,updated_at) VALUES (1,'b','b.epub','b.epub',10,'ready',?,?,?)", (json.dumps({"video": {"backgrounds": [str(target), str(keep)]}}), now, now))
    db.execute("INSERT INTO patch (book_id,patch_index,chapter_start,chapter_end,image_path,created_at,updated_at) VALUES (1,0,0,1,?,?,?)", (str(target), now, now))
    db.commit()
    client.post("/photos/delete", data={"name": "gone.png"}, follow_redirects=False)
    row = db.execute("SELECT automation_config FROM book WHERE id=1").fetchone()
    patch = db.execute("SELECT image_path FROM patch WHERE book_id=1").fetchone()
    assert patch["image_path"] is None
    assert json.loads(row["automation_config"])["video"]["backgrounds"] == [str(keep)]


# ---------------------------------------------------------------------------
# Deleting a background straight from the video config modal (by absolute path)
# ---------------------------------------------------------------------------


def test_background_delete_by_path_removes_file_and_references(client):
    target = _seed_photo(client, "drop.png")
    keep = _seed_photo(client, "stay.png")
    db = _db(client)
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,"
        "background_image_path,automation_config,created_at,updated_at) "
        "VALUES (1,'b','b.epub','b.epub',10,'ready',?,?,?,?)",
        (str(target), json.dumps({"video": {"backgrounds": [str(target), str(keep)]}}), now, now),
    )
    db.execute(
        "INSERT INTO patch (book_id,patch_index,chapter_start,chapter_end,image_path,created_at,updated_at) "
        "VALUES (1,0,0,1,?,?,?)",
        (str(target), now, now),
    )
    db.commit()

    resp = client.post("/video/backgrounds/delete", data={"path": str(target)})
    book = db.execute("SELECT background_image_path, automation_config FROM book WHERE id=1").fetchone()
    patch = db.execute("SELECT image_path FROM patch WHERE book_id=1").fetchone()

    assert resp.status_code == 200
    assert resp.json()["status"] == "deleted"
    assert not target.exists()
    assert keep.exists()
    assert book["background_image_path"] is None
    assert patch["image_path"] is None
    assert json.loads(book["automation_config"])["video"]["backgrounds"] == [str(keep)]
    db.close()


def test_background_delete_rejects_path_outside_library(client, tmp_path):
    outsider = tmp_path / "private.png"
    outsider.write_bytes(b"secret")
    resp = client.post("/video/backgrounds/delete", data={"path": str(outsider)})
    assert resp.status_code == 404
    assert outsider.exists()


def test_background_delete_refuses_default_background(client, monkeypatch):
    from app.config import settings

    default = _seed_photo(client, "default.png")
    monkeypatch.setattr(settings, "default_background_image", str(default))
    resp = client.post("/video/backgrounds/delete", data={"path": str(default)})
    assert resp.status_code == 400
    assert default.exists()


# ---------------------------------------------------------------------------
# Music rename
# ---------------------------------------------------------------------------


def _seed_music(client) -> int:
    db = _db(client)
    now = "2026-01-01T00:00:00+00:00"
    cur = db.execute(
        "INSERT INTO music (name, file_path, duration_sec, created_at) "
        "VALUES ('old name', '/tmp/x.mp3', 10.0, ?)",
        (now,),
    )
    db.commit()
    music_id = cur.lastrowid
    db.close()
    return music_id


def test_music_rename_updates_name(client):
    music_id = _seed_music(client)
    resp = client.post(
        f"/music/{music_id}/rename", data={"name": "epic theme"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    listed = client.get("/music/list").json()["music"]
    assert listed[0]["name"] == "epic theme"


def test_music_rename_blank_name_400(client):
    music_id = _seed_music(client)
    resp = client.post(
        f"/music/{music_id}/rename", data={"name": "   "},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_music_rename_unknown_id_404(client):
    resp = client.post(
        "/music/9999/rename", data={"name": "x"}, follow_redirects=False,
    )
    assert resp.status_code == 404
