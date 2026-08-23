"""Tests for the 1:1 podcast cover: the square crop, its preview and its file.

The cover is deliberately not a second piece of artwork — it is the very same
background + overlay stack the 16:9 thumbnail uses, cut to a square. These
tests pin that down: same pixels, different frame, plus the persistence rules
around the crop settings.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import image_overlay
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings
    from app.routes import video as video_routes

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(video_routes, "_BACKGROUNDS_DIR", tmp_path / "backgrounds")
    with TestClient(app) as c:
        yield c


def _db(client) -> sqlite3.Connection:
    from app.config import settings

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_book(client, tmp_path, *, bg_size=(640, 360)) -> Path:
    bg = tmp_path / "book_bg.png"
    bg.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", bg_size, (20, 20, 60)).save(str(bg), "PNG")
    now = datetime.now(timezone.utc).isoformat()
    conn = _db(client)
    conn.execute(
        "INSERT INTO book (id, title, original_filename, epub_path, patch_size, status, "
        "background_image_path, created_at, updated_at) "
        "VALUES (1, 'Sách Test', 'f.epub', '/tmp/f.epub', 10, 'ready', ?, ?, ?)",
        (str(bg), now, now),
    )
    conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, "
        "audio_path, created_at, updated_at) VALUES (1, 0, 0, 5, 'done', '/tmp/a.wav', ?, ?)",
        (now, now),
    )
    conn.commit()
    conn.close()
    return bg


def _image(resp) -> Image.Image:
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"
    return Image.open(BytesIO(resp.content))


# ---------------------------------------------------------------------------
# crop_square
# ---------------------------------------------------------------------------


def test_crop_square_takes_the_short_side_and_resizes():
    source = Image.new("RGB", (640, 360), (10, 10, 10))
    cover = image_overlay.crop_square(source, size=800)
    assert cover.size == (800, 800)


def test_crop_square_focus_slides_along_the_long_axis():
    source = Image.new("RGB", (640, 360), (0, 0, 0))
    for x in range(640):
        for y in range(360):
            source.putpixel((x, y), (x % 256, 0, 0))
    left = image_overlay.crop_square(source, focus_x=0, size=400)
    right = image_overlay.crop_square(source, focus_x=100, size=400)
    # The left crop starts at column 0, the right one at 640-360=280.
    assert left.getpixel((0, 0))[0] == 0
    assert right.getpixel((0, 0))[0] == 280 % 256
    assert left.tobytes() != right.tobytes()


def test_crop_square_ignores_focus_on_the_short_axis():
    source = Image.new("RGB", (640, 360), (5, 5, 5))
    top = image_overlay.crop_square(source, focus_y=0, size=400)
    bottom = image_overlay.crop_square(source, focus_y=100, size=400)
    assert top.tobytes() == bottom.tobytes()


def test_crop_square_clamps_the_output_size():
    source = Image.new("RGB", (640, 360), (5, 5, 5))
    assert image_overlay.crop_square(source, size=10).size == (
        image_overlay.PODCAST_COVER_MIN_SIZE,
        image_overlay.PODCAST_COVER_MIN_SIZE,
    )
    assert image_overlay.crop_square(source, size=99_999).size == (
        image_overlay.PODCAST_COVER_MAX_SIZE,
        image_overlay.PODCAST_COVER_MAX_SIZE,
    )


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_podcast_cover_defaults_are_present_and_clamped():
    cfg = image_overlay.parse_overlay_config(None)
    assert cfg["podcast_cover"] == {"enabled": False, "focus_x": 50, "focus_y": 50, "size": 1280}

    stored = json.dumps({"podcast_cover": {"enabled": True, "focus_x": -30, "size": "abc"}})
    cover = image_overlay.parse_overlay_config(stored)["podcast_cover"]
    assert cover["enabled"] is True
    assert cover["focus_x"] == 0
    assert cover["size"] == 1280


def test_podcast_cover_is_not_copied_into_text_layers():
    """The legacy single-layer fallback must not turn book-level keys into layer keys."""
    cfg = image_overlay.parse_overlay_config(json.dumps({"text": "x", "podcast_cover": {"enabled": True}}))
    assert cfg["overlays"] and "podcast_cover" not in cfg["overlays"][0]


def test_form_values_carry_the_crop_settings():
    cfg = image_overlay.overlay_cfg_from_values({
        "overlays_json": json.dumps([{"text": "a"}]),
        "podcast_cover_enabled": "on",
        "podcast_focus_x": "80",
        "podcast_focus_y": "10",
        "podcast_cover_size": "1600",
    })
    assert cfg["podcast_cover"] == {"enabled": True, "focus_x": 80, "focus_y": 10, "size": 1600}
    assert "podcast_cover" not in cfg["overlays"][0]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def test_preview_returns_a_square_png_at_the_requested_size(client, tmp_path):
    _seed_book(client, tmp_path)
    resp = client.get("/books/1/podcast-cover-preview", params={
        "live": "1", "podcast_cover_enabled": "on", "podcast_cover_size": "800",
    })
    cover = _image(resp)
    assert cover.size == (800, 800)
    assert resp.headers["X-Podcast-Cover-Size"] == "800"


def test_preview_focus_changes_the_pixels(client, tmp_path):
    bg = _seed_book(client, tmp_path)
    # A left-to-right gradient so the two crops cannot come out identical.
    gradient = Image.new("RGB", (640, 360))
    for x in range(640):
        for y in range(360):
            gradient.putpixel((x, y), (x % 256, 40, 90))
    gradient.save(str(bg), "PNG")

    params = {"live": "1", "podcast_cover_enabled": "on", "podcast_cover_size": "400"}
    left = _image(client.get("/books/1/podcast-cover-preview", params={**params, "podcast_focus_x": "0"}))
    right = _image(client.get("/books/1/podcast-cover-preview", params={**params, "podcast_focus_x": "100"}))
    assert left.tobytes() != right.tobytes()


def test_preview_matches_the_thumbnail_artwork(client, tmp_path):
    """Same overlay stack: the cover is a crop, never a re-render with other text."""
    _seed_book(client, tmp_path)
    params = {"live": "1", "overlays_json": json.dumps([{"text": "Chỉ một dòng", "position": "center"}])}
    full = _image(client.get("/books/1/overlay-preview", params=params))
    cover = _image(client.get("/books/1/podcast-cover-preview", params={
        **params, "podcast_cover_enabled": "on", "podcast_cover_size": "400",
    }))
    # 640x360 gives a 360px square centred at x=140, scaled up to the 400px output.
    expected = full.convert("RGB").crop((140, 0, 500, 360)).resize((400, 400), Image.LANCZOS)
    assert cover.convert("RGB").tobytes() == expected.tobytes()


def test_regenerate_writes_the_cover_file_and_serves_it(client, tmp_path):
    _seed_book(client, tmp_path)
    resp = client.post("/books/1/podcast-cover/regenerate")
    assert resp.status_code == 200, resp.text
    path = Path(resp.json()["path"])
    assert path == image_overlay.get_podcast_cover_path(1)
    assert path.is_file()
    with Image.open(path) as saved:
        assert saved.size[0] == saved.size[1] == resp.json()["size"]

    served = _image(client.get("/books/1/podcast-cover"))
    assert served.size[0] == served.size[1]


def test_saving_the_overlay_config_persists_and_invalidates_the_cover(client, tmp_path):
    _seed_book(client, tmp_path)
    client.post("/books/1/podcast-cover/regenerate")
    cover_file = image_overlay.get_podcast_cover_path(1)
    assert cover_file.is_file()

    resp = client.post("/books/1/overlay-config", data={
        "overlays_json": json.dumps([{"text": "Mới"}]),
        "podcast_cover_enabled": "on",
        "podcast_focus_x": "25",
        "podcast_cover_size": "1600",
    })
    assert resp.status_code == 200, resp.text
    assert not cover_file.exists(), "ảnh bìa cũ phải bị xoá vì artwork đã đổi"

    saved = client.get("/books/1/overlay-config").json()["config"]["podcast_cover"]
    assert saved == {"enabled": True, "focus_x": 25, "focus_y": 50, "size": 1600}


def test_a_form_without_podcast_fields_keeps_the_saved_crop(client, tmp_path):
    _seed_book(client, tmp_path)
    client.post("/books/1/overlay-config", data={
        "overlays_json": json.dumps([{"text": "A"}]),
        "podcast_cover_enabled": "on",
        "podcast_focus_x": "30",
    })
    client.post("/books/1/overlay-config", data={"overlays_json": json.dumps([{"text": "B"}])})

    saved = client.get("/books/1/overlay-config").json()["config"]["podcast_cover"]
    assert saved["enabled"] is True and saved["focus_x"] == 30


def test_cover_endpoints_404_for_an_unknown_book(client, tmp_path):
    _seed_book(client, tmp_path)
    assert client.post("/books/99/podcast-cover/regenerate").status_code == 404
    assert client.get("/books/99/podcast-cover").status_code == 404
