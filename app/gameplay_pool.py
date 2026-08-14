"""Clip-pool planning and deterministic multi-game clip creation."""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from app.config import settings
from app.gameplay_config import profile_key
from app.gameplay_registry import get_game, simulate_game
from app.gameplay_repository import (create_clip, list_fighters, list_themes, pool_status,
                                     save_gameplay_replay, save_replay)
from app.gameplay_simulation import simulate_match
from app.jobqueue import store


def active_profile(conn, width: int, height: int, fps: int, *, game_id: str = "battle_royale",
                   quality: int = 23) -> tuple[str, list[dict]]:
    themes = [{"id": row["id"], "version": row["version"], "name": row["name"],
               "asset_dir": row.get("asset_dir"), "manifest_json": row.get("manifest_json", "{}")}
              for row in list_themes(conn, enabled_only=True)]
    if not themes:
        themes = [{"id": "builtin", "version": 1, "name": "Built-in"}]
    if game_id == "battle_royale":
        return profile_key(width, height, fps, themes), themes
    game = get_game(game_id)
    selected = [{"id": f"builtin-{game.family}", "version": 1, "name": f"Built-in {game.family.title()}"}]
    return profile_key(width, height, fps, selected, game_id=game_id,
                       renderer_version=game.renderer_version,
                       simulation_version=game.simulation_version, quality=quality), selected


def _create_replay(conn, game_id: str, seed: int, themes: list[dict], replay_key: str, config: dict) -> dict:
    if game_id == "battle_royale":
        return save_replay(conn, replay_key, simulate_match(seed, list_fighters(conn), themes))
    return save_gameplay_replay(conn, replay_key, simulate_game(game_id, seed, config))


def enqueue_generation(conn, *, width: int, height: int, fps: int, count: int = 1,
                       game_id: str = "battle_royale", quality: int = 23,
                       config: dict | None = None) -> list[int]:
    profile, themes = active_profile(conn, width, height, fps, game_id=game_id, quality=quality)
    request_id = uuid.uuid4().hex
    ids = []
    for ordinal in range(max(0, count)):
        seed = int(hashlib.sha256(f"pool:{game_id}:{profile}:{request_id}:{ordinal}".encode()).hexdigest()[:12], 16)
        key = f"pool:{game_id}:{profile}:{request_id}:{ordinal}"
        saved = _create_replay(conn, game_id, seed, themes, key, config or {})
        path = str(Path(settings.data_root) / "gameplay" / "clips" / profile / f"{saved['id']}.mp4")
        duration = float(saved["duration_seconds"])
        clip_id = create_clip(conn, profile, saved["id"], duration, path, status="rendering",
                              game_id=game_id,
                              render_profile={"game_id": game_id, "width": width, "height": height,
                                              "fps": fps, "quality": quality})
        job_id = store.enqueue(conn, "gameplay_clip",
                               payload={"clip_id": clip_id, "resolution": [width, height], "fps": fps,
                                        "quality": quality, "game_id": game_id},
                               dedupe_key=f"gameplay_clip:clip={clip_id}", max_attempts=3)
        if job_id is not None:
            ids.append(job_id)
    return ids


def maintain_pool(conn, *, width: int, height: int, fps: int,
                  target_seconds: int | None = None, max_enqueue: int = 1,
                  game_id: str = "battle_royale", quality: int = 23,
                  config: dict | None = None) -> dict:
    target = settings.gameplay_pool_target_seconds if target_seconds is None else target_seconds
    profile, _ = active_profile(conn, width, height, fps, game_id=game_id, quality=quality)
    available = conn.execute(
        "SELECT COALESCE(SUM(duration_seconds),0) FROM gameplay_clip WHERE profile_key=? AND status IN ('available','rendering')",
        (profile,),).fetchone()[0]
    shortage = max(0.0, target - float(available))
    count = min(max_enqueue, int((shortage + 239) // 240))
    jobs = enqueue_generation(conn, width=width, height=height, fps=fps, count=count,
                              game_id=game_id, quality=quality, config=config) if count else []
    return {"profile_key": profile, "game_id": game_id, "available_seconds": available,
            "target_seconds": target, "job_ids": jobs}


def ensure_patch_coverage(conn, patch_id: int, required_seconds: float, *, width: int, height: int,
                          fps: int, game_id: str = "battle_royale", quality: int = 23,
                          config: dict | None = None) -> list[dict]:
    """Create or reserve a complete retry-stable clip set for one patch."""
    if required_seconds <= 0:
        raise ValueError("required gameplay duration must be positive")
    profile, themes = active_profile(conn, width, height, fps, game_id=game_id, quality=quality)
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            """SELECT * FROM gameplay_clip WHERE reserved_patch_id=?
               AND status IN ('reserved','consumed') ORDER BY id""", (patch_id,)).fetchall()
        if existing:
            if any((row["game_id"] or "battle_royale") != game_id or row["profile_key"] != profile for row in existing):
                raise ValueError("patch already owns incompatible gameplay clips")
            total = sum(float(row["duration_seconds"]) for row in existing)
        else:
            total = 0.0
        token = existing[0]["reservation_token"] if existing else uuid.uuid4().hex
        if not existing:
            candidates = conn.execute(
                """SELECT * FROM gameplay_clip WHERE profile_key=? AND status='available'
                   ORDER BY id""", (profile,),).fetchall()
            selected = []
            for row in candidates:
                if not row["file_path"] or not Path(row["file_path"]).is_file():
                    conn.execute("UPDATE gameplay_clip SET status='failed', error_message='clip file missing', updated_at=? WHERE id=?",
                                 (conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0], row["id"]))
                    continue
                selected.append(row["id"])
                total += float(row["duration_seconds"])
                if total >= required_seconds:
                    break
            if selected:
                placeholders = ",".join("?" for _ in selected)
                conn.execute(f"""UPDATE gameplay_clip SET status='reserved', reserved_patch_id=?,
                              reservation_token=?, updated_at=CURRENT_TIMESTAMP
                              WHERE id IN ({placeholders}) AND status='available'""",
                             (patch_id, token, *selected))
        index = conn.execute(
            "SELECT COUNT(*) FROM gameplay_clip WHERE reserved_patch_id=? AND profile_key=?",
            (patch_id, profile)).fetchone()[0]
        while total < required_seconds:
            key = f"patch:{patch_id}:{game_id}:{profile}:{index}"
            seed = int(hashlib.sha256(key.encode()).hexdigest()[:12], 16)
            if game_id == "battle_royale":
                replay = simulate_match(seed, list_fighters(conn), themes)
                saved = save_replay(conn, key, replay, commit=False)
            else:
                replay = simulate_game(game_id, seed, config or {})
                saved = save_gameplay_replay(conn, key, replay, commit=False)
            path = str(Path(settings.data_root) / "gameplay" / "clips" / profile / f"{saved['id']}.mp4")
            now = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
            existing_clip = conn.execute(
                "SELECT * FROM gameplay_clip WHERE reserved_patch_id=? AND replay_id=?", (patch_id, saved["id"])).fetchone()
            if existing_clip is None:
                conn.execute(
                    """INSERT INTO gameplay_clip
                       (profile_key,replay_id,duration_seconds,file_path,status,reserved_patch_id,
                        reservation_token,game_id,render_profile_json,created_at,updated_at)
                       VALUES (?,?,?,?, 'reserved', ?,?,?,?,?,?)""",
                    (profile, saved["id"], replay.duration_seconds, path, patch_id, token, game_id,
                     json.dumps({"game_id": game_id, "width": width, "height": height,
                                 "fps": fps, "quality": quality}), now, now))
                total += replay.duration_seconds
            else:
                total += float(existing_clip["duration_seconds"])
            index += 1
        rows = conn.execute(
            """SELECT * FROM gameplay_clip WHERE reserved_patch_id=?
               AND status IN ('reserved','consumed') ORDER BY id""", (patch_id,)).fetchall()
        if sum(float(row["duration_seconds"]) for row in rows) < required_seconds:
            raise RuntimeError("gameplay coverage transaction is incomplete")
        conn.commit()
        return [dict(row) for row in rows]
    except Exception:
        conn.rollback()
        raise


__all__ = ["active_profile", "enqueue_generation", "maintain_pool", "ensure_patch_coverage", "pool_status"]
