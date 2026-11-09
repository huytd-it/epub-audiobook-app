"""Clip-pool planning and deterministic multi-game clip creation."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import uuid
from pathlib import Path

from app.config import settings
from app.gameplay_config import profile_key
from app.gameplay_registry import get_game, simulate_game
from app.gameplay_repository import (create_clip, list_themes, pool_status,
                                     save_gameplay_replay)
from app.gameplay_retro import is_retro
from app.gameplay_scores import high_score
from app.jobqueue import store


def active_profile(conn, width: int, height: int, fps: int, *, game_id: str = "snake_arena",
                   quality: int = 23) -> tuple[str, list[dict]]:
    themes = [{"id": row["id"], "version": row["version"], "name": row["name"],
               "asset_dir": row.get("asset_dir"), "manifest_json": row.get("manifest_json", "{}")}
              for row in list_themes(conn, enabled_only=True)]
    if not themes:
        themes = [{"id": "builtin", "version": 1, "name": "Built-in"}]
    game = get_game(game_id)
    selected = [{"id": f"builtin-{game.family}", "version": 1,
                 "name": f"Built-in {game.family.title()}"}]
    return profile_key(width, height, fps, selected, game_id=game_id,
                       renderer_version=game.renderer_version,
                       simulation_version=game.simulation_version, quality=quality), selected


def _run_config(conn, game_id: str, config: dict) -> dict:
    """Retro runs render their target score into the HUD, so it is frozen before the match."""
    if not is_retro(game_id):
        return config
    return {**config, "hi_score": high_score(conn, game_id)}


def _create_replay(conn, game_id: str, seed: int, themes: list[dict], replay_key: str, config: dict) -> dict:
    replay = simulate_game(game_id, seed, _run_config(conn, game_id, config))
    if themes:
        replay = dataclasses.replace(replay, themes=themes)
    return save_gameplay_replay(conn, replay_key, replay)


def _clip_dir(profile: str, dest_dir: Path | None) -> Path:
    """Where a rendered clip lands: the chosen storage folder, or the pool's
    own profile directory when no destination was picked."""
    return Path(dest_dir) if dest_dir is not None else Path(settings.data_root) / "gameplay" / "clips" / profile


def _enqueue_clip(conn, *, game_id: str, ordinal: int, request_id: str,
                  width: int, height: int, fps: int, quality: int,
                  config: dict | None, dest_dir: Path | None) -> int | None:
    profile, themes = active_profile(conn, width, height, fps, game_id=game_id, quality=quality)
    seed = int(hashlib.sha256(f"pool:{game_id}:{profile}:{request_id}:{ordinal}".encode()).hexdigest()[:12], 16)
    key = f"pool:{game_id}:{profile}:{request_id}:{ordinal}"
    saved = _create_replay(conn, game_id, seed, themes, key, config or {})
    duration = float(saved["duration_seconds"])
    # The replay is keyed by (game, profile, request, ordinal), so a retried batch
    # slice resolves to the same replay. Reuse its clip instead of inserting a
    # second row, and skip clips that already finished rendering.
    existing = conn.execute(
        "SELECT id, status FROM gameplay_clip WHERE replay_id=? AND profile_key=? ORDER BY id LIMIT 1",
        (saved["id"], profile)).fetchone()
    if existing is not None and existing["status"] == "available":
        return None
    if existing is not None:
        clip_id = int(existing["id"])
    else:
        path = str(_clip_dir(profile, dest_dir) / f"{saved['id']}.mp4")
        clip_id = create_clip(conn, profile, saved["id"], duration, path, status="rendering",
                              game_id=game_id,
                              render_profile={"game_id": game_id, "width": width, "height": height,
                                              "fps": fps, "quality": quality})
    return store.enqueue(conn, "gameplay_clip",
                         payload={"clip_id": clip_id, "resolution": [width, height], "fps": fps,
                                  "quality": quality, "game_id": game_id},
                         dedupe_key=f"gameplay_clip:clip={clip_id}", max_attempts=3)


def enqueue_generation(conn, *, width: int, height: int, fps: int, count: int = 1,
                       game_id: str = "snake_arena", quality: int = 23,
                       config: dict | None = None, dest_dir: Path | None = None) -> list[int]:
    request_id = uuid.uuid4().hex
    ids = []
    for ordinal in range(max(0, count)):
        job_id = _enqueue_clip(conn, game_id=game_id, ordinal=ordinal, request_id=request_id,
                               width=width, height=height, fps=fps, quality=quality,
                               config=config, dest_dir=dest_dir)
        if job_id is not None:
            ids.append(job_id)
    return ids


def plan_batch(*, game_ids: list[str], count: int) -> dict:
    """Validate the target games and freeze a resumable batch plan.

    Cheap on purpose: no replay is simulated here, so a 1000-clip request stays
    a sub-second API call. The heavy work is expanded later by the worker."""
    games = list(dict.fromkeys(game_ids or ["snake_arena"]))
    for game_id in games:
        get_game(game_id)
    return {"request_id": uuid.uuid4().hex, "game_ids": games, "requested": max(0, int(count))}


def batch_game_for_ordinal(game_ids: list[str], request_id: str, ordinal: int) -> str:
    """Pick a game for one batch ordinal.

    Round-robin from a request-random start, so a batch is spread evenly over the
    selected games (differ by at most one) instead of leaving a game empty on a
    small batch. Deterministic, so chunked expansion and a retry after a restart
    assign the same game to the same ordinal."""
    start = int(hashlib.sha256(request_id.encode()).hexdigest()[:8], 16) % len(game_ids)
    return game_ids[(start + ordinal) % len(game_ids)]


def enqueue_batch(conn, *, width: int, height: int, fps: int, count: int,
                  game_ids: list[str], quality: int = 23,
                  config: dict | None = None, dest_dir: Path | None = None) -> dict:
    """Queue one resumable ``gameplay_batch`` job for ``count`` random clips.

    Only planning happens here. The handler simulates the replays and creates the
    clip rows on the worker, in bounded slices, so the API never blocks on
    simulating (or rendering) a 1000-video request."""
    plan = plan_batch(game_ids=game_ids, count=count)
    per_game = {game_id: 0 for game_id in plan["game_ids"]}
    for ordinal in range(plan["requested"]):
        per_game[batch_game_for_ordinal(plan["game_ids"], plan["request_id"], ordinal)] += 1
    payload = {"request_id": plan["request_id"], "game_ids": plan["game_ids"],
               "requested": plan["requested"], "offset": 0,
               "resolution": [width, height], "fps": fps, "quality": quality,
               "dest_dir": str(dest_dir) if dest_dir is not None else None}
    if config:
        payload["config"] = config
    job_id = store.enqueue(conn, "gameplay_batch", payload=payload,
                           dedupe_key=f"gameplay_batch:{plan['request_id']}", max_attempts=3)
    return {"requested": plan["requested"], "queued": plan["requested"],
            "game_ids": plan["game_ids"], "per_game": per_game, "job_id": job_id,
            "dest_path": str(dest_dir) if dest_dir is not None else None, "offset": 0}


def expand_batch_slice(conn, *, game_ids: list[str], request_id: str, offset: int, count: int,
                       width: int, height: int, fps: int, quality: int = 23,
                       config: dict | None = None, dest_dir: Path | None = None) -> dict:
    """Create the clips for a slice of a batch: ordinals [offset, offset+count).

    Runs on the worker (each replay simulation costs 0.1-2s), which is why the
    batch handler calls it in bounded chunks instead of the request handler."""
    created: list[int] = []
    end = offset + max(0, int(count))
    for ordinal in range(offset, end):
        game_id = batch_game_for_ordinal(game_ids, request_id, ordinal)
        job_id = _enqueue_clip(conn, game_id=game_id, ordinal=ordinal, request_id=request_id,
                               width=width, height=height, fps=fps, quality=quality,
                               config=config, dest_dir=dest_dir)
        if job_id is not None:
            created.append(job_id)
    return {"created": len(created), "next_offset": end}


def maintain_pool(conn, *, width: int, height: int, fps: int,
                  target_seconds: int | None = None, max_enqueue: int = 1,
                  game_id: str = "snake_arena", quality: int = 23,
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
                           fps: int, game_id: str = "snake_arena", game_ids: list[str] | None = None,
                           quality: int = 23, config: dict | None = None) -> list[dict]:
    """Create or reserve a complete retry-stable clip set for one patch.

    When ``game_ids`` contains more than one entry, clips are generated in that
    order repeatedly. This makes one patch video rotate through the selected
    games instead of selecting a single game for the entire video.
    """
    if required_seconds <= 0:
        raise ValueError("required gameplay duration must be positive")
    selected_games = list(dict.fromkeys(game_ids or [game_id]))
    if not selected_games:
        raise ValueError("gameplay rotation requires at least one game")
    for selected_game in selected_games:
        get_game(selected_game)
    conn.execute("BEGIN IMMEDIATE")
    try:
        expected_profiles = {
            selected_game: active_profile(
                conn, width, height, fps, game_id=selected_game, quality=quality
            )[0]
            for selected_game in selected_games
        }
        existing = conn.execute(
            """SELECT * FROM gameplay_clip WHERE reserved_patch_id=?
               AND status IN ('reserved','consumed') ORDER BY id""", (patch_id,)).fetchall()
        if existing:
            compatible = all(
                row["game_id"] == selected_games[index % len(selected_games)]
                and row["profile_key"] == expected_profiles[row["game_id"]]
                for index, row in enumerate(existing)
                if row["game_id"] in expected_profiles
            ) and all(row["game_id"] in expected_profiles for row in existing)
            if not compatible:
                # A patch can be republished after its resolution/fps/quality or
                # game rotation changes. Keeping its old reservation would mix
                # render profiles in one concat (for example 60fps + 30fps).
                # Detach the whole sequence so rotation restarts deterministically.
                ids = [row["id"] for row in existing]
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"""UPDATE gameplay_clip
                           SET status=CASE WHEN status='reserved' THEN 'available' ELSE status END,
                               reserved_patch_id=NULL, reservation_token=NULL,
                               updated_at=CURRENT_TIMESTAMP
                         WHERE id IN ({placeholders})""",
                    ids,
                )
                existing = []
        if existing:
            total = sum(float(row["duration_seconds"]) for row in existing)
        else:
            total = 0.0
        token = existing[0]["reservation_token"] if existing else uuid.uuid4().hex
        if not existing and len(selected_games) == 1:
            profile = expected_profiles[game_id]
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
        index = len(existing)
        while total < required_seconds:
            current_game = selected_games[index % len(selected_games)]
            profile, themes = active_profile(conn, width, height, fps, game_id=current_game, quality=quality)
            key = f"patch:{patch_id}:{current_game}:{profile}:{index}"
            seed = int(hashlib.sha256(key.encode()).hexdigest()[:12], 16)
            replay = simulate_game(current_game, seed, _run_config(conn, current_game, config or {}))
            if themes:
                replay = dataclasses.replace(replay, themes=themes)
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
                    (profile, saved["id"], replay.duration_seconds, path, patch_id, token, current_game,
                      json.dumps({"game_id": current_game, "width": width, "height": height,
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


__all__ = ["active_profile", "enqueue_generation", "enqueue_batch", "expand_batch_slice",
           "plan_batch", "batch_game_for_ordinal", "maintain_pool",
           "ensure_patch_coverage", "pool_status"]
