import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
from app.video_config import VIDEO_DEFAULTS, get_book_video_config, validate_video_config


def test_video_config_defaults_match_video_creator():
    config = validate_video_config({})
    assert config["codec"] == "libx264"
    assert config["audio_bitrate"] == "320k"
    assert config["quality"] == 23
    assert config["concurrency"] == 3
    assert config["image_duration_seconds"] == 15
    assert config["waveform_enabled"] is False
    assert config["waveform_style"] == "line"
    assert config["waveform_layout"] == "horizontal"
    assert config["waveform_background_opacity"] == 0.55


@pytest.mark.parametrize("field,value", [("codec", "bad"), ("audio_bitrate", "64k"), ("background_mode", "shuffle")])
def test_video_config_rejects_invalid_values(field, value):
    with pytest.raises(ValueError):
        validate_video_config({field: value})


@pytest.mark.parametrize("field,value", [("waveform_style", "bars"), ("waveform_color", "white"), ("waveform_layout", "diagonal"), ("waveform_background_color", "black"), ("waveform_height", 20), ("waveform_opacity", 2), ("waveform_background_opacity", 2)])
def test_video_config_rejects_invalid_waveform_values(field, value):
    with pytest.raises(ValueError):
        validate_video_config({field: value})


def test_video_config_reads_legacy_columns_and_json(tmp_path):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        """INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,video_resolution,video_fps,default_image_animation,automation_config,created_at,updated_at)
           VALUES (1,'Book','book.epub','book.epub',10,'ready','1920x1080',24,'zoom-in',?,?,?)""",
        (json.dumps({"video": {"codec": "h264_nvenc"}}), now, now),
    )
    conn.commit()
    book = conn.execute("SELECT * FROM book WHERE id=1").fetchone()
    config = get_book_video_config(conn, SimpleNamespace(**dict(zip(book.keys(), tuple(book)))))
    assert config["resolution"] == "1920x1080"
    assert config["fps"] == 24
    assert config["image_animation"] == "zoom-in"
    assert config["codec"] == "h264_nvenc"


def test_video_config_route_persists_json_block(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = client.app.state.conn
        now = "2026-01-01T00:00:00+00:00"
        conn.execute(
            "INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,created_at,updated_at) VALUES (1,'Book','b.epub','b.epub',10,'ready',?,?)",
            (now, now),
        )
        conn.commit()
        response = client.post("/books/1/video-config", json={"codec": "h264_nvenc", "quality": 20})
        stored = conn.execute("SELECT automation_config FROM book WHERE id=1").fetchone()[0]

    assert response.status_code == 200
    assert response.json()["codec"] == "h264_nvenc"
    assert json.loads(stored)["video"]["quality"] == 20
