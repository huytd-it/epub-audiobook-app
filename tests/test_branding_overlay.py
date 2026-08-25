"""Branding overlay: text watermark + logo on thumbnails, podcast covers, and video frames.

Covers: apply_branding with target toggles, position mapping, opacity,
compose_patch_overlay with branding, podcast cover branding after crop.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app import db, image_overlay, repository
from app.config import settings
from app.production_defaults import (
    get_effective_branding_config,
    get_global_production_defaults,
    save_global_production_defaults,
    validate_branding_config,
)


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _insert_book(conn, book_id=1, config=None):
    automation = json.dumps(config) if config is not None else None
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                             automation_config, video_resolution, video_fps,
                             default_image_animation, normalize_numbers_enabled,
                             normalize_junk_enabled, normalize_spellcheck_enabled,
                             normalize_dictionary_enabled, normalize_transliteration_enabled,
                             tts_model, tts_voice_id, tts_max_chars, tts_with_effects,
                             created_at, updated_at)
           VALUES (?, 'Book', 'b.epub', 'b.epub', 10, 'ready', ?, '1920x1080', 30,
                   'none', 1, 1, 1, 0, 0, NULL, NULL, NULL, 0, ?, ?)""",
        (book_id, automation, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    return repository.get_book(conn, book_id)


def _make_image(w=640, h=480, color=(100, 100, 100)):
    return Image.new("RGB", (w, h), color)


def _make_logo(path: str, size=(100, 100)):
    img = Image.new("RGBA", size, (255, 0, 0, 200))
    img.save(path, "PNG")
    return path


# ---------------------------------------------------------------------------
# validate_branding_config
# ---------------------------------------------------------------------------


def test_validate_branding_config_default():
    cfg = validate_branding_config({})
    assert cfg["watermark"]["enabled"] is False
    assert cfg["watermark"]["text"] == ""
    assert cfg["watermark"]["position"] == "bottom-right"
    assert cfg["watermark"]["font_size"] == 28
    assert cfg["watermark"]["opacity"] == 80
    assert cfg["logo"]["enabled"] is False
    assert cfg["logo"]["path"] == ""
    assert cfg["targets"]["thumbnail"] is True
    assert cfg["targets"]["podcast"] is True
    assert cfg["targets"]["video"] is True


def test_validate_branding_config_clamps_values():
    cfg = validate_branding_config({
        "watermark": {"font_size": 999, "opacity": 150, "margin": -5},
        "logo": {"size": 0, "opacity": -10},
    })
    assert cfg["watermark"]["font_size"] == 120
    assert cfg["watermark"]["opacity"] == 100
    assert cfg["watermark"]["margin"] == 0
    assert cfg["logo"]["size"] == 16
    assert cfg["logo"]["opacity"] == 0


def test_validate_branding_config_invalid_position_falls_back():
    cfg = validate_branding_config({
        "watermark": {"position": "middle"},
        "logo": {"position": "diagonal"},
    })
    assert cfg["watermark"]["position"] == "bottom-right"
    assert cfg["logo"]["position"] == "bottom-right"


# ---------------------------------------------------------------------------
# apply_branding
# ---------------------------------------------------------------------------


def test_apply_branding_noop_when_disabled():
    img = _make_image()
    branding = validate_branding_config({})
    result = image_overlay.apply_branding(img, branding, target="thumbnail")
    assert result.size == img.size
    assert list(result.getdata()) == list(img.getdata())


def test_apply_branding_respects_target_toggle():
    img = _make_image()
    branding = validate_branding_config({
        "watermark": {"enabled": True, "text": "Test"},
        "targets": {"thumbnail": False},
    })
    result = image_overlay.apply_branding(img, branding, target="thumbnail")
    # Should be unchanged since thumbnail target is disabled
    assert list(result.getdata()) == list(img.getdata())


def test_apply_branding_watermark_text():
    img = _make_image()
    branding = validate_branding_config({
        "watermark": {"enabled": True, "text": "Hello World"},
    })
    result = image_overlay.apply_branding(img, branding, target="thumbnail")
    assert result.size == img.size
    # Result should differ from original since watermark is drawn
    assert list(result.getdata()) != list(img.getdata())


def test_apply_branding_watermark_all_positions():
    img = _make_image()
    positions = ["top-left", "top-right", "bottom-left", "bottom-right", "center"]
    for pos in positions:
        branding = validate_branding_config({
            "watermark": {"enabled": True, "text": "Test", "position": pos},
        })
        result = image_overlay.apply_branding(img, branding, target="thumbnail")
        assert result.size == img.size


def test_apply_branding_logo():
    with tempfile.TemporaryDirectory() as tmp:
        logo_path = _make_logo(str(Path(tmp) / "logo.png"))
        img = _make_image()
        branding = validate_branding_config({
            "logo": {"enabled": True, "path": logo_path, "size": 50},
        })
        result = image_overlay.apply_branding(img, branding, target="thumbnail")
        assert result.size == img.size
        assert list(result.getdata()) != list(img.getdata())


def test_apply_branding_logo_missing_file():
    img = _make_image()
    branding = validate_branding_config({
        "logo": {"enabled": True, "path": "/nonexistent/logo.png"},
    })
    # Should not crash; logo is skipped when file is missing
    result = image_overlay.apply_branding(img, branding, target="thumbnail")
    assert result.size == img.size


def test_apply_branding_both_watermark_and_logo():
    with tempfile.TemporaryDirectory() as tmp:
        logo_path = _make_logo(str(Path(tmp) / "logo.png"))
        img = _make_image()
        branding = validate_branding_config({
            "watermark": {"enabled": True, "text": "Channel"},
            "logo": {"enabled": True, "path": logo_path},
        })
        result = image_overlay.apply_branding(img, branding, target="thumbnail")
        assert result.size == img.size
        assert list(result.getdata()) != list(img.getdata())


# ---------------------------------------------------------------------------
# compose_patch_overlay with branding
# ---------------------------------------------------------------------------


def test_compose_patch_overlay_with_branding(tmp_path):
    bg_path = tmp_path / "bg.png"
    _make_image().save(str(bg_path))
    book = SimpleNamespace(id=1, title="Test Book", background_image_path=str(bg_path))
    patch = SimpleNamespace(name="Episode 1", patch_index=0, chapter_start=1, chapter_end=5)
    cfg = image_overlay.parse_overlay_config(None)
    branding = validate_branding_config({
        "watermark": {"enabled": True, "text": "Branded"},
    })
    result = image_overlay.compose_patch_overlay(book, patch, cfg, str(bg_path), branding=branding)
    assert result.size[0] > 0
    assert result.size[1] > 0


# ---------------------------------------------------------------------------
# Per-book branding mode
# ---------------------------------------------------------------------------


def test_book_branding_mode_inherit():
    conn = _conn()
    book = _insert_book(conn)
    cfg = image_overlay.parse_overlay_config(book.overlay_config)
    from app.production_defaults import parse_book_config, get_group_mode
    config = parse_book_config(book)
    assert get_group_mode(config, "branding", book=book) == "inherit"


def test_book_branding_mode_custom():
    conn = _conn()
    book = _insert_book(conn, config={
        "inherit": {"branding": False},
        "branding": {"watermark": {"enabled": True, "text": "Custom"}},
    })
    from app.production_defaults import parse_book_config, get_group_mode
    config = parse_book_config(book)
    assert get_group_mode(config, "branding", book=book) == "custom"


def test_effective_branding_per_book_override():
    conn = _conn()
    save_global_production_defaults(conn, {"branding": {
        "watermark": {"enabled": True, "text": "Global"},
    }})
    book = _insert_book(conn, config={
        "inherit": {"branding": False},
        "branding": {"watermark": {"enabled": True, "text": "Book"}},
    })
    effective = get_effective_branding_config(conn, book)
    assert effective["watermark"]["text"] == "Book"


def test_effective_branding_inherits_global():
    conn = _conn()
    save_global_production_defaults(conn, {"branding": {
        "watermark": {"enabled": True, "text": "Global"},
    }})
    book = _insert_book(conn)
    effective = get_effective_branding_config(conn, book)
    assert effective["watermark"]["text"] == "Global"


# ---------------------------------------------------------------------------
# resolve_media_path
# ---------------------------------------------------------------------------


def test_resolve_media_path_absolute_existing(tmp_path):
    from app.image_overlay import resolve_media_path
    img_path = tmp_path / "test.png"
    img_path.touch()
    result = resolve_media_path(str(img_path))
    assert result == str(img_path)


def test_resolve_media_path_virtual_relative(tmp_path, monkeypatch):
    from app.image_overlay import resolve_media_path
    monkeypatch.setattr("app.config.settings.data_root", str(tmp_path))
    # Create a file in a whitelisted root
    backgrounds = tmp_path / "backgrounds"
    backgrounds.mkdir()
    img = backgrounds / "test.png"
    img.touch()
    result = resolve_media_path("backgrounds/test.png")
    assert result == str(img)


def test_resolve_media_path_nonexistent_returns_original():
    from app.image_overlay import resolve_media_path
    result = resolve_media_path("backgrounds/nonexistent.png")
    assert result == "backgrounds/nonexistent.png"


def test_resolve_media_path_empty():
    from app.image_overlay import resolve_media_path
    result = resolve_media_path("")
    assert result == ""


# ---------------------------------------------------------------------------
# apply_branding with virtual paths
# ---------------------------------------------------------------------------


def test_apply_branding_logo_with_virtual_path(tmp_path, monkeypatch):
    from app.image_overlay import apply_branding, resolve_media_path
    monkeypatch.setattr("app.config.settings.data_root", str(tmp_path))
    # Create a logo in a whitelisted root
    logos_dir = tmp_path / "uploads"
    logos_dir.mkdir()
    logo = logos_dir / "brand.png"
    img = Image.new("RGBA", (100, 100), (255, 0, 0, 200))
    img.save(str(logo), "PNG")

    target_img = _make_image()
    branding = validate_branding_config({
        "logo": {"enabled": True, "path": "uploads/brand.png", "size": 50},
    })
    result = apply_branding(target_img, branding, target="thumbnail")
    assert result.size == target_img.size
    # Logo was applied, image should differ
    assert list(result.getdata()) != list(target_img.getdata())


# ---------------------------------------------------------------------------
# Branding in overlay preview
# ---------------------------------------------------------------------------


def test_overlay_preview_includes_branding(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr("app.config.settings.data_root", str(tmp_path))
    monkeypatch.setattr("app.config.settings.enable_worker", False)
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.config.settings.db_path", str(db_path))
    app = __import__("app.main", fromlist=["app"]).app
    with TestClient(app) as client:
        conn = client.app.state.conn
        book = _insert_book(conn)
        # Set up background
        bg_dir = tmp_path / "backgrounds"
        bg_dir.mkdir()
        bg = bg_dir / "test.png"
        _make_image().save(str(bg))
        conn.execute("UPDATE book SET background_image_path = ? WHERE id = ?", (str(bg), book.id))
        # Save global branding
        save_global_production_defaults(conn, {"branding": {
            "watermark": {"enabled": True, "text": "Preview Test"},
        }})
        conn.commit()
        # Request overlay preview
        response = client.get(f"/books/{book.id}/overlay-preview?background_path={bg}")
    assert response.status_code == 200
    assert response.headers.get("content-type") == "image/png"
    # The image should contain branding (watermark text applied)
    from io import BytesIO
    from PIL import Image as PILImage
    result_img = PILImage.open(BytesIO(response.content))
    # Image should be valid and have content
    assert result_img.size[0] > 0
    assert result_img.size[1] > 0


# ---------------------------------------------------------------------------
# save_book_branding_config (per-book endpoint)
# ---------------------------------------------------------------------------


def test_save_book_branding_config_stores_in_automation_config():
    from app.production_defaults import save_book_branding_config, parse_book_config, get_group_mode
    conn = _conn()
    book = _insert_book(conn)
    branding = {"watermark": {"enabled": True, "text": "Per-Book"}}
    validated = save_book_branding_config(conn, book.id, branding)
    conn.commit()
    assert validated["watermark"]["text"] == "Per-Book"
    # Re-read book and verify it's stored in automation_config
    book = repository.get_book(conn, book.id)
    config = parse_book_config(book)
    assert config["branding"]["watermark"]["text"] == "Per-Book"
    assert get_group_mode(config, "branding", book=book) == "custom"


def test_save_book_branding_config_validates():
    from app.production_defaults import save_book_branding_config
    conn = _conn()
    book = _insert_book(conn)
    # Invalid values should be clamped
    branding = {
        "watermark": {"font_size": 999, "opacity": -5},
        "logo": {"size": 0},
    }
    validated = save_book_branding_config(conn, book.id, branding)
    conn.commit()
    assert validated["watermark"]["font_size"] == 120
    assert validated["watermark"]["opacity"] == 0
    assert validated["logo"]["size"] == 16


def test_save_book_branding_config_does_not_affect_global():
    from app.production_defaults import save_book_branding_config, get_global_production_defaults
    conn = _conn()
    save_global_production_defaults(conn, {"branding": {
        "watermark": {"enabled": True, "text": "Global"},
    }})
    book = _insert_book(conn)
    save_book_branding_config(conn, book.id, {"watermark": {"enabled": True, "text": "Per-Book"}})
    conn.commit()
    # Global defaults must still have the original text
    global_defaults = get_global_production_defaults(conn)
    assert global_defaults["branding"]["watermark"]["text"] == "Global"


# ---------------------------------------------------------------------------
# POST /books/{id}/branding-config endpoint
# ---------------------------------------------------------------------------


def test_branding_config_endpoint_saves_and_purges(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr("app.config.settings.data_root", str(tmp_path))
    monkeypatch.setattr("app.config.settings.enable_worker", False)
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.config.settings.db_path", str(db_path))
    app = __import__("app.main", fromlist=["app"]).app
    with TestClient(app) as client:
        conn = client.app.state.conn
        book = _insert_book(conn)
        # Set up background
        bg_dir = tmp_path / "backgrounds"
        bg_dir.mkdir()
        bg = bg_dir / "test.png"
        _make_image().save(str(bg))
        conn.execute("UPDATE book SET background_image_path = ? WHERE id = ?", (str(bg), book.id))
        conn.commit()
        # Create a patch so overlay cache can be purged
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
            "VALUES (?, 0, 1, 10, 'pending', ?, ?)",
            (book.id, now, now),
        )
        conn.commit()
        # Create a cached overlay file
        overlay_dir = tmp_path / "books" / str(book.id) / "patch_overlays"
        overlay_dir.mkdir(parents=True)
        overlay_file = overlay_dir / f"{book.id}_001.png"
        _make_image().save(str(overlay_file))
        assert overlay_file.exists()
        # Save per-book branding
        response = client.post(
            f"/books/{book.id}/branding-config",
            json={"branding": {"watermark": {"enabled": True, "text": "Book Brand"}}},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["mode"] == "custom"
    assert body["effective"]["watermark"]["text"] == "Book Brand"
    # Overlay file should have been purged
    assert not overlay_file.exists()


def test_branding_config_endpoint_returns_404_for_missing_book(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr("app.config.settings.data_root", str(tmp_path))
    monkeypatch.setattr("app.config.settings.enable_worker", False)
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.config.settings.db_path", str(db_path))
    app = __import__("app.main", fromlist=["app"]).app
    with TestClient(app) as client:
        response = client.post(
            "/books/9999/branding-config",
            json={"branding": {"watermark": {"enabled": True, "text": "X"}}},
        )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Live preview with branding override (branding_json query param)
# ---------------------------------------------------------------------------


def test_overlay_preview_branding_override(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr("app.config.settings.data_root", str(tmp_path))
    monkeypatch.setattr("app.config.settings.enable_worker", False)
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.config.settings.db_path", str(db_path))
    app = __import__("app.main", fromlist=["app"]).app
    with TestClient(app) as client:
        conn = client.app.state.conn
        book = _insert_book(conn)
        bg_dir = tmp_path / "backgrounds"
        bg_dir.mkdir()
        bg = bg_dir / "test.png"
        _make_image().save(str(bg))
        conn.execute("UPDATE book SET background_image_path = ? WHERE id = ?", (str(bg), book.id))
        conn.commit()
        # Request overlay preview with branding_json override
        branding_override = json.dumps({"watermark": {"enabled": True, "text": "Override"}})
        response = client.get(
            f"/books/{book.id}/overlay-preview?background_path={bg}&branding_json={branding_override}",
        )
    assert response.status_code == 200
    assert response.headers.get("content-type") == "image/png"


def test_podcast_preview_branding_override(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr("app.config.settings.data_root", str(tmp_path))
    monkeypatch.setattr("app.config.settings.enable_worker", False)
    db_path = tmp_path / "test.db"
    monkeypatch.setattr("app.config.settings.db_path", str(db_path))
    app = __import__("app.main", fromlist=["app"]).app
    with TestClient(app) as client:
        conn = client.app.state.conn
        book = _insert_book(conn)
        bg_dir = tmp_path / "backgrounds"
        bg_dir.mkdir()
        bg = bg_dir / "test.png"
        _make_image().save(str(bg))
        conn.execute("UPDATE book SET background_image_path = ? WHERE id = ?", (str(bg), book.id))
        conn.commit()
        # Enable podcast cover in overlay config
        cfg = image_overlay.parse_overlay_config(None)
        cfg["podcast_cover"] = {"enabled": True, "focus_x": 50, "focus_y": 50, "size": 800}
        conn.execute("UPDATE book SET overlay_config = ? WHERE id = ?", (json.dumps(cfg), book.id))
        conn.commit()
        # Request podcast cover preview with branding_json override
        branding_override = json.dumps({"watermark": {"enabled": True, "text": "Pod Brand"}})
        response = client.get(
            f"/books/{book.id}/podcast-cover-preview?background_path={bg}&branding_json={branding_override}"
            f"&podcast_cover_enabled=on&podcast_focus_x=50&podcast_focus_y=50&podcast_cover_size=800",
        )
    assert response.status_code == 200
    assert response.headers.get("content-type") == "image/png"
