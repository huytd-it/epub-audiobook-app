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
from app.gameplay_models import Fighter, GameplayReplay, Replay
from app.gameplay_registry import list_games, resolve_game_id, simulate_game
from app.gameplay_repository import (apply_replay_stats, consume_reserved, create_clip, list_fighters,
                                      load_replay, save_gameplay_replay, save_replay, seed_catalog)
from app.gameplay_simulation import CLASS_STATS, simulate_match
from app.gameplay_theme_packs import install_theme_pack_zip
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


def test_theme_pack_v2_is_validated_and_immutable(tmp_path):
    def pack(color):
        output = io.BytesIO()
        manifest = {"schema_version": 2, "id": "pixel-calm", "version": 1,
                    "name": "Pixel Calm", "family": "pixel", "supported_games": {
                        "garden_cycle": {"contract_version": 1, "assets": {"sprite": "assets/sprite.png"}}}}
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("theme.json", json.dumps(manifest))
            image = Image.new("RGBA", (16, 16), color)
            raw = io.BytesIO(); image.save(raw, "PNG")
            archive.writestr("assets/sprite.png", raw.getvalue())
        return output.getvalue()
    first = install_theme_pack_zip(pack((1, 2, 3, 255)), tmp_path)
    assert first["family"] == "pixel" and len(first["content_sha256"]) == 64
    assert install_theme_pack_zip(pack((1, 2, 3, 255)), tmp_path)["content_sha256"] == first["content_sha256"]
    with pytest.raises(ValueError, match="immutable"):
        install_theme_pack_zip(pack((4, 5, 6, 255)), tmp_path)


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


def test_generic_games_are_deterministic_versioned_and_bounded():
    catalog = [game for game in list_games() if game["family"] != "legacy"]
    assert len(catalog) == 8
    assert sum(game["family"] == "pixel" for game in catalog) == 4
    assert sum(game["family"] == "neon" for game in catalog) == 4
    for game_id in ("garden_cycle", "aquarium_ecosystem", "parcel_route", "cloud_runner",
                    "orbit_drift", "marble_flow", "territory_bloom", "signal_garden"):
        first = simulate_game(game_id, 123, {"preset": "calm"})
        assert first == simulate_game(game_id, 123, {"preset": "calm"})
        assert first != simulate_game(game_id, 124, {"preset": "calm"})
        assert first.schema_version == 3 and first.game_id == game_id
        assert 180 <= first.duration_seconds <= 300
        assert first.result["status"] == "complete"


def test_replay_repository_dual_reads_legacy_and_envelope():
    conn = _conn()
    legacy = simulate_match(8, list_fighters(conn), _themes())
    legacy_row = save_replay(conn, "legacy", legacy)
    assert isinstance(load_replay(conn, legacy_row["id"]), Replay)
    modern = simulate_game("garden_cycle", 8, {"preset": "calm"})
    modern_row = save_gameplay_replay(conn, "modern", modern)
    loaded = load_replay(conn, modern_row["id"])
    assert isinstance(loaded, GameplayReplay)
    assert loaded.to_dict() == modern.to_dict()


def test_rotation_resolution_is_retry_stable():
    config = {"selection_mode": "rotation", "game_ids": ["orbit_drift", "garden_cycle"]}
    first = resolve_game_id(config, book_id=1, patch_id=2, patch_index=3)
    assert first in config["game_ids"]
    assert first == resolve_game_id(config, book_id=1, patch_id=2, patch_index=3)
    assert first == resolve_game_id({**config, "game_ids": list(reversed(config["game_ids"]))},
                                    book_id=1, patch_id=2, patch_index=3)


def test_gameplay_video_config_and_profile_v2():
    config = validate_video_config({"background_type": "gameplay", "gameplay": {
        "selection_mode": "rotation", "game_ids": ["garden_cycle", "orbit_drift"], "preset": "calm"}})
    assert config["background_mode"] == "sequential"
    assert config["gameplay"]["selection_mode"] == "rotation"
    pixel = profile_key(1920, 1080, 30, [{"id": "builtin-pixel", "version": 1}], game_id="garden_cycle")
    neon = profile_key(1920, 1080, 30, [{"id": "builtin-neon", "version": 1}], game_id="orbit_drift")
    assert len(pixel) == 32 and pixel != neon


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


def test_patch_coverage_is_complete_and_retry_stable(tmp_path):
    from app.gameplay_pool import ensure_patch_coverage
    conn = _conn(str(tmp_path / "coverage.db"))
    conn.execute("INSERT INTO book (title,original_filename,epub_path,status,created_at,updated_at) VALUES ('b','b','b','done','n','n')")
    book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO patch (book_id,patch_index,chapter_start,chapter_end,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                 (book_id, 0, 0, 0, "done", "n", "n"))
    patch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    first = ensure_patch_coverage(conn, patch_id, 520, width=854, height=480, fps=24,
                                  game_id="garden_cycle", config={"preset": "calm"})
    second = ensure_patch_coverage(conn, patch_id, 520, width=854, height=480, fps=24,
                                   game_id="garden_cycle", config={"preset": "calm"})
    assert sum(row["duration_seconds"] for row in first) >= 520
    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert len({row["reservation_token"] for row in first}) == 1
    with pytest.raises(ValueError, match="token mismatch"):
        consume_reserved(conn, patch_id, "stale-token")
    assert consume_reserved(conn, patch_id, first[0]["reservation_token"])
    assert conn.execute("SELECT COUNT(*) FROM gameplay_clip WHERE reserved_patch_id=? AND status='consumed'",
                        (patch_id,)).fetchone()[0] == len(first)
