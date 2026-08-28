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
from app.gameplay_models import GameplayReplay
from app.gameplay_procedural import PROCEDURAL_GAMES
from app.gameplay_registry import list_games, migrate_game_id, resolve_game_id, simulate_game
from app.gameplay_retro import RETRO_GAMES, build_engine, rank_tier
from app.gameplay_retro_render import RetroClip
from app.gameplay_scores import high_score, leaderboard, standings
from app.gameplay_repository import (apply_replay_stats, consume_reserved, create_clip,
                                      load_replay, save_gameplay_replay, seed_catalog)
from app.gameplay_themes import ASSETS, install_theme_zip, theme_prompt
from app.video_config import validate_video_config


def _conn(path=":memory:"):
    conn = db.connect(path)
    db.init_schema(conn)
    return conn


def _themes():
    return [{"id": "neon-geometry", "version": 1, "name": "Neon Geometry"}]


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
    with pytest.raises(ValueError):
        validate_video_config({"background_type": "random"})


def test_generic_games_are_deterministic_versioned_and_bounded():
    catalog = list_games()
    assert len(catalog) == 16
    assert sum(game["family"] == "retro" for game in catalog) == 10
    assert sum(game["family"] == "procedural" for game in catalog) == 6
    # No catalog game may need art any more: every family paints itself.
    assert all(game["sprite_roles"] == [] for game in catalog)
    for spec in RETRO_GAMES:
        first = simulate_game(spec.id, 123, {"preset": "calm"})
        assert first == simulate_game(spec.id, 123, {"preset": "calm"})
        assert first != simulate_game(spec.id, 124, {"preset": "calm"})
        assert first.schema_version == 3 and first.game_id == spec.id
        assert 180 <= first.duration_seconds <= 300
        assert first.result["status"] == "complete"
        assert first.result["score"] > 0 and first.payload["player"]
        assert first.result["metrics"]["level"] >= 1


def test_retro_games_replay_identically_on_a_re_render():
    for spec in RETRO_GAMES:
        replay = simulate_game(spec.id, 4242, {"preset": "calm", "hi_score": 5_000})
        assert replay.payload["hi_score"] == 5_000  # frozen: the HUD target cannot drift
        renders = []
        for _ in range(2):
            clip = RetroClip(spec.id, replay.payload, replay.duration_seconds, 480, 270, 24)
            renders.append([clip.frame(index, index / 24).tobytes() for index in range(0, 120, 12)])
        assert renders[0] == renders[1], spec.id
        assert len(set(renders[0])) > 1, spec.id  # the board actually moves
    portrait = simulate_game("snake_arena", 7, {})
    clip = RetroClip("snake_arena", portrait.payload, portrait.duration_seconds, 270, 480, 24)
    assert clip.frame(0, 0.0).size == (270, 480)


def test_spaceship_bosses_rotate_and_cast_distinct_skills():
    engine = build_engine("spaceship_voyager", 77, {})
    observed = set()
    for boss_kind in range(3):
        engine.boss_index = boss_kind
        engine._spawn_boss()
        engine._boss_skill()
        observed.add(engine.events[-1]["skill"])
    assert observed == {"COMET BARRAGE", "VOID CURTAIN", "NOVA SPIRAL"}
    assert engine.stats["skills"] == 3


def test_retro_runs_are_ranked_and_promoted_once_rendered():
    conn = _conn()
    assert high_score(conn, "snake_arena") == 0
    rows = []
    for index, game_id in enumerate(("snake_arena", "snake_arena", "brick_stack")):
        replay = simulate_game(game_id, 500 + index, {"hi_score": high_score(conn, game_id)})
        rows.append(save_gameplay_replay(conn, f"run:{index}", replay))
    assert conn.execute("SELECT COUNT(*) FROM gameplay_score").fetchone()[0] == 3
    save_gameplay_replay(conn, "run:0", simulate_game("snake_arena", 500, {}))
    assert conn.execute("SELECT COUNT(*) FROM gameplay_score").fetchone()[0] == 3  # idempotent
    board = leaderboard(conn, "snake_arena")
    assert [entry["position"] for entry in board] == [1, 2]
    assert board[0]["score"] >= board[1]["score"]
    assert board[0]["rating"] == 1000 and board[0]["player_tag"]
    overall = leaderboard(conn)
    assert {entry["game_id"] for entry in overall} == {"snake_arena", "brick_stack"}
    assert overall[0]["rating"] >= overall[-1]["rating"]  # cross-game ranking is normalised
    assert high_score(conn, "snake_arena") == board[0]["score"]
    assert not board[0]["rendered"]
    assert apply_replay_stats(conn, rows[0]["id"]) is True
    assert conn.execute("SELECT rendered FROM gameplay_score WHERE replay_id=?",
                        (rows[0]["id"],)).fetchone()[0] == 1
    summary = {entry["game_id"]: entry for entry in standings(conn)}
    assert summary["snake_arena"]["runs"] == 2 and summary["snake_arena"]["champion"]
    assert rank_tier(100, 0) == "S" and rank_tier(100, 100) == "S" and rank_tier(10, 100) == "E"


def test_procedural_games_need_no_assets_and_render_deterministically():
    from app.gameplay_effects import ProceduralClip

    catalog = {game["id"]: game for game in list_games()}
    for spec in PROCEDURAL_GAMES:
        game = catalog[spec.id]
        assert game["family"] == "procedural" and game["sprite_roles"] == []
        replay = simulate_game(spec.id, 31337, {"preset": "calm"})
        assert replay == simulate_game(spec.id, 31337, {"preset": "calm"})
        assert replay != simulate_game(spec.id, 31338, {"preset": "calm"})
        assert 180 <= replay.duration_seconds <= 300 and replay.result["status"] == "complete"
        renders = []
        for _ in range(2):
            clip = ProceduralClip(spec.id, replay.payload, replay.duration_seconds, 256, 144, 24)
            renders.append([clip.frame(index, index / 24).tobytes() for index in range(8)])
        # Same replay, same pixels: the pool may re-render a clip after a failed attempt.
        assert renders[0] == renders[1], spec.id
        assert max(max(frame) for frame in renders[0]) > 0, spec.id


def test_gameplay_api_serves_the_catalog_and_the_boards(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.config import settings as app_settings
    from app.main import app

    monkeypatch.setattr(app_settings, "db_path", str(tmp_path / "api.db"))
    monkeypatch.setattr(app_settings, "data_root", str(tmp_path))
    monkeypatch.setattr(app_settings, "enable_worker", False)
    with TestClient(app) as client:
        conn = app.state.conn
        for index, game_id in enumerate(("snake_arena", "brick_stack")):
            save_gameplay_replay(conn, f"api:{index}", simulate_game(game_id, 900 + index, {}))
        status = client.get("/gameplay/status").json()
        assert {game["id"] for game in status["catalog"]} >= {spec.id for spec in RETRO_GAMES}
        assert status["stat_labels"]["snake_arena"] == [["apples", "Mồi"], ["length", "Độ dài"]]
        assert len(status["leaderboard"]) == 2 and len(status["standings"]) == 2
        board = client.get("/gameplay/leaderboard", params={"game_id": "snake_arena"}).json()
        assert [entry["position"] for entry in board["entries"]] == [1]
        assert board["entries"][0]["score"] > 0 and board["entries"][0]["rank_tier"] == "S"
        assert client.get("/gameplay/leaderboard", params={"game_id": "nope"}).status_code == 404


def test_books_configured_before_the_retro_catalog_still_render():
    # A book saved against the retired pixel/neon catalog must not fail validation now.
    config = validate_video_config({"background_type": "gameplay", "gameplay": {
        "selection_mode": "single", "game_id": "garden_cycle", "preset": "calm"}})
    assert config["gameplay"]["game_id"] == "snake_arena"
    rotation = validate_video_config({"background_type": "gameplay", "gameplay": {
        "selection_mode": "rotation", "game_ids": ["orbit_drift", "signal_garden"], "preset": "calm"}})
    assert rotation["gameplay"]["game_ids"] == ["star_defender", "brick_stack"]
    assert migrate_game_id("snake_arena") == "snake_arena"
    assert resolve_game_id({"selection_mode": "single", "game_id": "cloud_runner"},
                           book_id=1, patch_id=1, patch_index=0) == "pixel_dash"


def test_rotation_resolution_is_retry_stable():
    config = {"selection_mode": "rotation", "game_ids": ["brick_stack", "snake_arena"]}
    first = resolve_game_id(config, book_id=1, patch_id=2, patch_index=3)
    assert first in config["game_ids"]
    assert first == resolve_game_id(config, book_id=1, patch_id=2, patch_index=3)
    assert first == resolve_game_id({**config, "game_ids": list(reversed(config["game_ids"]))},
                                    book_id=1, patch_id=2, patch_index=3)


def test_gameplay_video_config_and_profile_v2():
    config = validate_video_config({"background_type": "gameplay", "gameplay": {
        "selection_mode": "rotation", "game_ids": ["snake_arena", "brick_stack"], "preset": "calm"}})
    assert config["background_mode"] == "sequential"
    assert config["gameplay"]["selection_mode"] == "rotation"
    snake = profile_key(1920, 1080, 30, [{"id": "builtin-retro", "version": 1}], game_id="snake_arena")
    tetris = profile_key(1920, 1080, 30, [{"id": "builtin-retro", "version": 1}], game_id="brick_stack")
    assert len(snake) == 32 and snake != tetris


def test_clip_reservation_is_atomic_and_retry_stable(tmp_path):
    db_path = str(tmp_path / "gameplay.db")
    conn = _conn(db_path)
    replay = simulate_game("snake_arena", 7, {"preset": "calm"})
    row = save_gameplay_replay(conn, "reserve", replay)
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
                                  game_id="snake_arena", config={"preset": "calm"})
    second = ensure_patch_coverage(conn, patch_id, 520, width=854, height=480, fps=24,
                                   game_id="snake_arena", config={"preset": "calm"})
    assert sum(row["duration_seconds"] for row in first) >= 520
    assert [row["id"] for row in first] == [row["id"] for row in second]
    assert len({row["reservation_token"] for row in first}) == 1
    with pytest.raises(ValueError, match="token mismatch"):
        consume_reserved(conn, patch_id, "stale-token")
    assert consume_reserved(conn, patch_id, first[0]["reservation_token"])
    assert conn.execute("SELECT COUNT(*) FROM gameplay_clip WHERE reserved_patch_id=? AND status='consumed'",
                        (patch_id,)).fetchone()[0] == len(first)


def test_patch_coverage_rotates_selected_games(tmp_path):
    from app.gameplay_pool import ensure_patch_coverage
    conn = _conn(str(tmp_path / "rotation-coverage.db"))
    conn.execute("INSERT INTO book (title,original_filename,epub_path,status,created_at,updated_at) VALUES ('b','b','b','done','n','n')")
    book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO patch (book_id,patch_index,chapter_start,chapter_end,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                 (book_id, 0, 0, 0, "done", "n", "n"))
    patch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    clips = ensure_patch_coverage(conn, patch_id, 520, width=854, height=480, fps=24,
                                  game_ids=["snake_arena", "aurora_veil"], config={"preset": "calm"})
    assert [clip["game_id"] for clip in clips[:2]] == ["snake_arena", "aurora_veil"]
    assert {clip["game_id"] for clip in clips} == {"snake_arena", "aurora_veil"}
