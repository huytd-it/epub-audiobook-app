"""Testable clip-pool planning and deterministic clip creation."""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import settings
from app.gameplay_config import profile_key
from app.gameplay_repository import create_clip, list_fighters, list_themes, pool_status, save_replay
from app.gameplay_simulation import simulate_match
from app.jobqueue import store


def active_profile(conn, width: int, height: int, fps: int) -> tuple[str, list[dict]]:
    themes = [{"id": row["id"], "version": row["version"], "name": row["name"],
               "asset_dir": row.get("asset_dir"), "manifest_json": row.get("manifest_json", "{}")}
              for row in list_themes(conn, enabled_only=True)]
    return profile_key(width, height, fps, themes), themes


def enqueue_generation(conn, *, width: int, height: int, fps: int, count: int = 1) -> list[int]:
    profile, themes = active_profile(conn, width, height, fps)
    ids = []
    for offset in range(max(0, count)):
        nonce = conn.execute("SELECT COALESCE(MAX(id),0)+1 FROM gameplay_replay").fetchone()[0] + offset
        seed = int(hashlib.sha256(f"{profile}:{nonce}".encode()).hexdigest()[:12], 16)
        replay = simulate_match(seed, list_fighters(conn), themes)
        saved = save_replay(conn, f"pool:{profile}:{seed}", replay)
        path = str(Path(settings.data_root) / "gameplay" / "clips" / profile / f"{saved['id']}.mp4")
        clip_id = create_clip(conn, profile, saved["id"], replay.duration_seconds, path, status="rendering")
        job_id = store.enqueue(conn, "gameplay_clip",
                               payload={"clip_id": clip_id, "resolution": [width, height], "fps": fps},
                               dedupe_key=f"gameplay_clip:clip={clip_id}", max_attempts=3)
        if job_id is not None:
            ids.append(job_id)
    return ids


def maintain_pool(conn, *, width: int, height: int, fps: int,
                  target_seconds: int | None = None, max_enqueue: int = 1) -> dict:
    target = settings.gameplay_pool_target_seconds if target_seconds is None else target_seconds
    profile, _ = active_profile(conn, width, height, fps)
    available = conn.execute(
        "SELECT COALESCE(SUM(duration_seconds),0) FROM gameplay_clip WHERE profile_key=? AND status IN ('available','rendering')",
        (profile,),).fetchone()[0]
    shortage = max(0.0, target - float(available))
    count = min(max_enqueue, int((shortage + 239) // 240))
    jobs = enqueue_generation(conn, width=width, height=height, fps=fps, count=count) if count else []
    return {"profile_key": profile, "available_seconds": available, "target_seconds": target, "job_ids": jobs}


def ensure_patch_coverage(conn, patch_id: int, required_seconds: float, *, width: int, height: int, fps: int) -> list[dict]:
    """Reserve pool inventory, then deterministically create only the missing coverage."""
    from app.gameplay_repository import reserve_clips

    existing = conn.execute(
        "SELECT * FROM gameplay_clip WHERE reserved_patch_id=? AND status IN ('reserved','consumed') ORDER BY id",
        (patch_id,),).fetchall()
    if existing:
        return [dict(row) for row in existing]
    profile, themes = active_profile(conn, width, height, fps)
    reserved = reserve_clips(conn, profile, patch_id, required_seconds)
    if reserved:
        return reserved
    fighters = list_fighters(conn)
    total = 0.0
    index = 0
    while total < required_seconds:
        seed = int(hashlib.sha256(f"patch:{patch_id}:{profile}:{index}".encode()).hexdigest()[:12], 16)
        replay = simulate_match(seed, fighters, themes)
        saved = save_replay(conn, f"patch:{patch_id}:{profile}:{index}", replay)
        path = str(Path(settings.data_root) / "gameplay" / "clips" / profile / f"{saved['id']}.mp4")
        create_clip(conn, profile, saved["id"], replay.duration_seconds, path,
                    status="reserved", patch_id=patch_id)
        total += replay.duration_seconds
        index += 1
    return [dict(row) for row in conn.execute(
        "SELECT * FROM gameplay_clip WHERE reserved_patch_id=? AND status='reserved' ORDER BY id", (patch_id,))]
