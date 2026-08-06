"""Tests for the studio overlay preview: live params, drag rect header, offsets."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings
    from app.routes import video as video_routes

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    # _BACKGROUNDS_DIR is a module-level constant computed from the real
    # settings at import time - repoint it so the whitelist uses the test dir.
    monkeypatch.setattr(video_routes, "_BACKGROUNDS_DIR", tmp_path / "backgrounds")
    with TestClient(app) as c:
        yield c


def _db(client) -> sqlite3.Connection:
    from app.config import settings

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _make_png(path: Path, size=(640, 360), color=(20, 20, 60)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(str(path), "PNG")
    return path


def _seed_book(client, tmp_path, *, with_patch=True, bg_size=(640, 360)) -> Path:
    bg = _make_png(tmp_path / "book_bg.png", size=bg_size)
    now = datetime.now(timezone.utc).isoformat()
    conn = _db(client)
    conn.execute(
        "INSERT INTO book (id, title, original_filename, epub_path, patch_size, status, "
        "background_image_path, created_at, updated_at) "
        "VALUES (1, 'Sách Test', 'f.epub', '/tmp/f.epub', 10, 'ready', ?, ?, ?)",
        (str(bg), now, now),
    )
    if with_patch:
        conn.execute(
            "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, "
            "audio_path, created_at, updated_at) "
            "VALUES (1, 0, 0, 5, 'done', '/tmp/a.wav', ?, ?)",
            (now, now),
        )
    conn.commit()
    conn.close()
    return bg


def _rect(resp) -> dict:
    assert "X-Overlay-Rect" in resp.headers
    return json.loads(resp.headers["X-Overlay-Rect"])


# ---------------------------------------------------------------------------
# Preview endpoint
# ---------------------------------------------------------------------------


def test_preview_returns_png_and_rect_header(client, tmp_path):
    _seed_book(client, tmp_path)
    resp = client.get("/books/1/overlay-preview")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    rect = _rect(resp)
    assert rect["img_w"] == 640 and rect["img_h"] == 360
    assert 0 <= rect["x"] <= rect["img_w"] - rect["w"]
    assert 0 <= rect["y"] <= rect["img_h"] - rect["h"]
    assert rect["w"] > 0 and rect["h"] > 0


def test_preview_live_position_overrides_saved_config(client, tmp_path):
    _seed_book(client, tmp_path)
    top = _rect(client.get("/books/1/overlay-preview", params={"live": "1", "position": "top"}))
    bottom = _rect(client.get("/books/1/overlay-preview", params={"live": "1", "position": "bottom"}))
    assert bottom["y"] > top["y"]


def test_preview_live_offset_shifts_rect(client, tmp_path):
    _seed_book(client, tmp_path)
    base = _rect(client.get(
        "/books/1/overlay-preview",
        params={"live": "1", "position": "top", "alignment": "left"},
    ))
    shifted = _rect(client.get(
        "/books/1/overlay-preview",
        params={"live": "1", "position": "top", "alignment": "left",
                "offset_x": "57", "offset_y": "23"},
    ))
    assert shifted["x"] == base["x"] + 57
    assert shifted["y"] == base["y"] + 23


def test_preview_offset_clamped_inside_image(client, tmp_path):
    _seed_book(client, tmp_path)
    rect = _rect(client.get(
        "/books/1/overlay-preview",
        params={"live": "1", "offset_x": "3000", "offset_y": "3000"},
    ))
    assert rect["x"] + rect["w"] <= rect["img_w"]
    assert rect["y"] + rect["h"] <= rect["img_h"]
    assert rect["x"] >= 0 and rect["y"] >= 0


def test_preview_without_patches_uses_placeholder_label(client, tmp_path):
    _seed_book(client, tmp_path, with_patch=False)
    resp = client.get("/books/1/overlay-preview")
    assert resp.status_code == 200


def test_preview_ignores_background_path_outside_whitelist(client, tmp_path):
    _seed_book(client, tmp_path, bg_size=(640, 360))
    sneaky = _make_png(tmp_path / "secret" / "sneaky.png", size=(111, 99))
    rect = _rect(client.get(
        "/books/1/overlay-preview", params={"background_path": str(sneaky)},
    ))
    # Falls back to the book background instead of reading an arbitrary path.
    assert (rect["img_w"], rect["img_h"]) == (640, 360)


def test_preview_accepts_whitelisted_background_path(client, tmp_path):
    _seed_book(client, tmp_path, bg_size=(640, 360))
    allowed = _make_png(tmp_path / "backgrounds" / "alt.png", size=(320, 200))
    rect = _rect(client.get(
        "/books/1/overlay-preview", params={"background_path": str(allowed)},
    ))
    assert (rect["img_w"], rect["img_h"]) == (320, 200)


# ---------------------------------------------------------------------------
# Saving offsets
# ---------------------------------------------------------------------------


def test_overlay_config_post_persists_offsets(client, tmp_path):
    _seed_book(client, tmp_path)
    resp = client.post(
        "/books/1/overlay-config",
        data={"position": "bottom", "alignment": "left", "offset_x": "42", "offset_y": "-17"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    conn = _db(client)
    raw = conn.execute("SELECT overlay_config FROM book WHERE id = 1").fetchone()[0]
    conn.close()
    cfg = json.loads(raw)
    assert cfg["offset_x"] == 42
    assert cfg["offset_y"] == -17
    assert cfg["position"] == "bottom"


def test_overlay_config_post_clamps_offsets(client, tmp_path):
    _seed_book(client, tmp_path)
    client.post(
        "/books/1/overlay-config",
        data={"offset_x": "999999", "offset_y": "-999999"},
        follow_redirects=False,
    )
    conn = _db(client)
    raw = conn.execute("SELECT overlay_config FROM book WHERE id = 1").fetchone()[0]
    conn.close()
    cfg = json.loads(raw)
    assert cfg["offset_x"] == 4000
    assert cfg["offset_y"] == -4000


# ---------------------------------------------------------------------------
# Studio page + patch overlay rendering
# ---------------------------------------------------------------------------


def test_book_detail_page_renders_studio(client, tmp_path):
    _seed_book(client, tmp_path)
    resp = client.get("/books/1")
    assert resp.status_code == 200
    assert 'id="studio-modal"' in resp.text
    assert 'id="drag-rect" tabindex="0" role="button"' in resp.text
    assert 'id="mix-play"' in resp.text
    assert "shadow.enabled" in resp.text
    assert "box.opacity" in resp.text



def test_overlay_font_list_includes_bundled_display_font():
    from app import image_overlay

    fonts = image_overlay.list_overlay_fonts()
    pacifico = next((font for font in fonts if "Pacifico" in font["name"]), None)
    assert pacifico is not None
    assert Path(pacifico["path"]).name == "Pacifico-Regular.ttf"


def test_overlay_font_list_excludes_invalid_font(tmp_path, monkeypatch):
    from app import image_overlay

    invalid = tmp_path / "broken.ttf"
    invalid.write_bytes(b"not a font")
    monkeypatch.setattr(image_overlay.settings, "default_font_path", str(invalid))
    assert all(font["path"] != str(invalid) for font in image_overlay.list_overlay_fonts())


def test_mix_reference_lists_voices(client, tmp_path):
    _seed_book(client, tmp_path)
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    (voices_dir / "giong_nu.wav").write_bytes(b"RIFFfake")
    (voices_dir / "notes.txt").write_bytes(b"not audio")
    resp = client.get("/books/1")
    assert resp.status_code == 200
    assert 'value="giong_nu.wav"' in resp.text
    assert "notes.txt" not in resp.text


def test_mix_reference_preselects_book_voice(client, tmp_path):
    _seed_book(client, tmp_path)
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    (voices_dir / "a.wav").write_bytes(b"RIFFfake")
    (voices_dir / "b.wav").write_bytes(b"RIFFfake")
    conn = _db(client)
    conn.execute("UPDATE book SET voice_clip_path = ? WHERE id = 1",
                 (str(voices_dir / "b.wav"),))
    conn.commit()
    conn.close()
    resp = client.get("/books/1")
    assert 'value="b.wav" selected' in resp.text


def test_voice_select_post_sets_book_voice_clip_path(client, tmp_path):
    _seed_book(client, tmp_path)
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    (voices_dir / "narrator.wav").write_bytes(b"RIFFfake")
    resp = client.post(
        "/books/1/voice-select", data={"voice_name": "narrator.wav"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    conn = _db(client)
    path = conn.execute("SELECT voice_clip_path FROM book WHERE id = 1").fetchone()[0]
    conn.close()
    assert path == str(voices_dir / "narrator.wav")


def test_voice_select_post_clears_when_empty(client, tmp_path):
    _seed_book(client, tmp_path)
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    voice = voices_dir / "narrator.wav"
    voice.write_bytes(b"RIFFfake")
    conn = _db(client)
    conn.execute("UPDATE book SET voice_clip_path = ? WHERE id = 1", (str(voice),))
    conn.commit()
    conn.close()
    resp = client.post("/books/1/voice-select", data={"voice_name": ""}, follow_redirects=False)
    assert resp.status_code == 303
    conn = _db(client)
    path = conn.execute("SELECT voice_clip_path FROM book WHERE id = 1").fetchone()[0]
    conn.close()
    assert path is None


def test_voice_select_post_rejects_missing_file(client, tmp_path):
    _seed_book(client, tmp_path)
    resp = client.post(
        "/books/1/voice-select", data={"voice_name": "ghost.wav"}, follow_redirects=False,
    )
    assert resp.status_code == 400


def test_voice_select_post_rejects_path_traversal(client, tmp_path):
    _seed_book(client, tmp_path)
    resp = client.post(
        "/books/1/voice-select", data={"voice_name": "../secrets.wav"}, follow_redirects=False,
    )
    assert resp.status_code == 400


def test_render_patch_overlay_with_box_enabled(tmp_path):
    """Regression: the box branch used PIL.Image without importing it."""
    from app import image_overlay
    from app.models import Book, Patch

    bg = _make_png(tmp_path / "bg.png")
    book = Book(id=1, title="t", original_filename="f", epub_path="", patch_size=10,
                status="done", final_audio_path=None, final_video_path=None,
                background_image_path=str(bg), voice_clip_path=None,
                voice_transcript=None, created_at="", updated_at="")
    patch = Patch(id=1, book_id=1, patch_index=0, chapter_start=0, chapter_end=5,
                  status="done", audio_path=None, error_message=None,
                  attempt_count=0, created_at="", updated_at="", name="P1")
    cfg = image_overlay.get_default_overlay_config()
    cfg["box"]["enabled"] = True
    out = tmp_path / "out.png"
    image_overlay.render_patch_overlay(book, patch, cfg, str(out))
    assert out.exists()
    assert Image.open(str(out)).size == (640, 360)


def test_expand_overlay_text_uses_patch_placeholders():
    from types import SimpleNamespace
    from app import image_overlay

    book = SimpleNamespace(title="My Book")
    patch = SimpleNamespace(name="Opening", patch_index=2, chapter_start=4, chapter_end=7)
    text = image_overlay.expand_overlay_text(
        "{book_title}|{patch_name}|{episode}|{chapter}|{chapter_start}|{chapter_end}",
        book, patch,
    )
    assert text == "My Book|Opening|3|4-7|4|7"


def test_overlay_config_accepts_multiple_layers():
    import json
    from app import image_overlay

    cfg = image_overlay.overlay_cfg_from_values({
        "overlays_json": json.dumps([
            {"text": "{book_title}", "position": "top", "font_size": 60},
            {"text": "Tập {episode}", "position": "bottom", "font_size": 36},
        ])
    })
    assert [item["text"] for item in cfg["overlays"]] == ["{book_title}", "Tập {episode}"]
    assert [item["position"] for item in cfg["overlays"]] == ["top", "bottom"]


def test_overlay_config_accepts_advanced_layer_settings():
    from app import image_overlay

    cfg = image_overlay.overlay_cfg_from_values({
        "overlays_json": json.dumps([{
            "text": "Tập {episode}",
            "shadow": {"enabled": False, "color": "#112233", "offset": 99},
            "box": {
                "enabled": True,
                "color": "#445566",
                "opacity": 125,
                "padding_x": 31,
                "padding_y": 17,
                "radius": 22,
            },
        }])
    })
    layer = cfg["overlays"][0]
    assert layer["shadow"] == {"enabled": False, "color": "#112233", "offset": 20}
    assert layer["box"] == {
        "enabled": True,
        "color": "#445566",
        "opacity": 100,
        "padding_x": 31,
        "padding_y": 17,
        "radius": 22,
    }
