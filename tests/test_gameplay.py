from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from app import db
from app.gameplay_config import profile_key
from app.gameplay_models import Fighter
from app.gameplay_repository import (apply_replay_stats, create_clip, list_fighters,
                                      save_replay, seed_catalog)
from app.gameplay_simulation import CLASS_STATS, simulate_match
from app.gameplay_themes import ASSETS, install_theme_zip, theme_prompt
from app.video_config import validate_video_config


def _conn(path=":memory:"):
    conn = db.connect(path)
    db.init_schema(conn)
    return conn


def _themes():
    return [{"id": "neon-geometry", "version": 1, "name": "Neon Geometry"}]


def test_simulation_is_deterministic_and_bounded():
    roster = [Fighter(f"f{i}", f"F{i}", ("tank", "assassin", "ranger")[i % 3]) for i in range(48)]
    first = simulate_match(42, roster, _themes())
    assert first.to_dict() == simulate_match(42, roster, _themes()).to_dict()
    assert first.to_dict() != simulate_match(43, roster, _themes()).to_dict()
    assert 180 <= first.duration_seconds <= 300
    assert len(first.roster) == 24 and len(first.top3) == 3
    assert any(event["type"] == "result" for event in first.events)


def test_class_roles_are_distinct():
    assert CLASS_STATS["tank"]["hp"] > CLASS_STATS["ranger"]["hp"] > CLASS_STATS["assassin"]["hp"]
    assert CLASS_STATS["assassin"]["speed"] > CLASS_STATS["ranger"]["speed"] > CLASS_STATS["tank"]["speed"]
    assert CLASS_STATS["ranger"]["range"] > CLASS_STATS["tank"]["range"]
    assert CLASS_STATS["assassin"]["damage"] > CLASS_STATS["tank"]["damage"]


def test_catalog_and_stats_are_idempotent():
    conn = _conn()
    seed_catalog(conn); seed_catalog(conn)
    assert conn.execute("SELECT COUNT(*) FROM gameplay_fighter").fetchone()[0] == 48
    replay = simulate_match(9, list_fighters(conn), _themes())
    row = save_replay(conn, "test:stats", replay)
    assert apply_replay_stats(conn, row["id"]) is True
    assert apply_replay_stats(conn, row["id"]) is False
    assert conn.execute("SELECT SUM(matches) FROM gameplay_fighter").fetchone()[0] == 24
    assert conn.execute("SELECT SUM(wins) FROM gameplay_fighter").fetchone()[0] == 1


def _theme_zip(*, traversal=False, alpha=True):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr("theme.json", json.dumps({"id": "army-one", "version": 1, "name": "Army One"}))
        for filename in ASSETS:
            image = Image.new("RGBA" if alpha else "RGB", (64, 32), (255, 0, 255, 120) if alpha else (255, 0, 255))
            data = io.BytesIO(); image.save(data, "PNG")
            archive.writestr(("../" if traversal and filename == ASSETS[0] else "") + filename, data.getvalue())
    return output.getvalue()


def test_theme_zip_validation_and_prompt(tmp_path):
    result = install_theme_zip(_theme_zip(), tmp_path)
    with Image.open(Path(result["asset_dir"]) / "tank.png") as image:
        assert image.size == (256, 256) and "A" in image.getbands()
    assert "no watermark" in theme_prompt("navy").lower()
    with pytest.raises(ValueError, match="invalid theme pack path"):
        install_theme_zip(_theme_zip(traversal=True), tmp_path)
    with pytest.raises(ValueError, match="with alpha"):
        install_theme_zip(_theme_zip(alpha=False), tmp_path)


def test_profile_changes_for_every_render_dimension():
    base = profile_key(1920, 1080, 30, _themes(), renderer_version="1")
    assert base != profile_key(1280, 720, 30, _themes(), renderer_version="1")
    assert base != profile_key(1920, 1080, 60, _themes(), renderer_version="1")
    assert base != profile_key(1920, 1080, 30, _themes(), renderer_version="2")
    assert base != profile_key(1920, 1080, 30, [{"id": "x", "version": 1}], renderer_version="1")


def test_old_config_defaults_to_media():
    assert validate_video_config({})["background_type"] == "media"
    assert validate_video_config({"background_type": "battle_royale"})["background_type"] == "battle_royale"
    with pytest.raises(ValueError):
        validate_video_config({"background_type": "random"})


def test_clip_reservation_is_atomic_and_retry_stable(tmp_path):
    db_path = str(tmp_path / "gameplay.db")
    conn = _conn(db_path)
    replay = simulate_match(7, list_fighters(conn), _themes())
    row = save_replay(conn, "reserve", replay)
    clip = tmp_path / "clip.mp4"; clip.write_bytes(b"clip")
    create_clip(conn, "profile", row["id"], 240, str(clip))
    conn.execute("INSERT INTO book (title,original_filename,epub_path,status,created_at,updated_at) VALUES ('b','b','b','done','n','n')")
    book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    for index in range(2):
        conn.execute("INSERT INTO patch (book_id,patch_index,chapter_start,chapter_end,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                     (book_id, index, 0, 0, "done", "n", "n"))
    patch_ids = [row[0] for row in conn.execute("SELECT id FROM patch ORDER BY id")]
    conn.commit(); conn.close()
    from app.gameplay_repository import reserve_clips
    def reserve(patch_id):
        local = db.connect(db_path)
        try: return reserve_clips(local, "profile", patch_id, 120)
        finally: local.close()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve, patch_ids))
    assert sorted(len(value) for value in results) == [0, 1]
    winning_patch = patch_ids[0] if results[0] else patch_ids[1]
    assert reserve(winning_patch)[0]["id"] == (results[0] or results[1])[0]["id"]
