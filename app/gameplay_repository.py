"""SQLite persistence for gameplay themes, replay logs, statistics, and clip leases."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.gameplay_config import BUILTIN_THEME_ID, BUILTIN_THEME_VERSION
from app.gameplay_models import GameplayReplay
from app.gameplay_scores import mark_rendered, record_score


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_catalog(conn: sqlite3.Connection) -> None:
    now = _now()
    manifest = {"palette": ["#070b24", "#16f2ff", "#ff3dcb", "#b9ff36"]}
    conn.execute(
        """INSERT OR IGNORE INTO gameplay_theme
           (id, version, name, enabled, builtin, manifest_json, created_at, updated_at)
           VALUES (?, ?, 'Neon Geometry', 1, 1, ?, ?, ?)""",
        (BUILTIN_THEME_ID, BUILTIN_THEME_VERSION, json.dumps(manifest), now, now),
    )
    from app.gameplay_registry import list_games
    for index, game in enumerate(list_games()):
        conn.execute(
            """INSERT OR IGNORE INTO gameplay_game
               (game_id,enabled,family,display_order,config_json,updated_at)
               VALUES (?,1,?,?, '{}',?)""", (game["id"], game["family"], index, now))
    conn.commit()


def list_themes(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict]:
    where = "WHERE enabled=1" if enabled_only else ""
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM gameplay_theme {where} ORDER BY builtin DESC, name, version DESC")]


def save_gameplay_replay(conn: sqlite3.Connection, replay_key: str, replay: GameplayReplay, *, commit: bool = True) -> dict:
    raw = replay.to_dict()
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    now = _now()
    conn.execute(
        """INSERT OR IGNORE INTO gameplay_replay
           (replay_key, seed, duration_seconds, roster_json, themes_json, map_json,
            events_json, top3_json, winner_key, game_id, schema_version, simulation_version,
            ruleset_version, renderer_version, payload_json, result_json, content_sha256, created_at)
           VALUES (?, ?, ?, '[]', ?, '{}', '[]', '[]', '', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (replay_key, replay.seed, replay.duration_seconds, json.dumps(replay.themes, ensure_ascii=False),
         replay.game_id, replay.schema_version,
         replay.simulation_version, replay.ruleset_version, replay.renderer_version,
         json.dumps(replay.payload, ensure_ascii=False), json.dumps(replay.result, ensure_ascii=False),
         digest, now),
    )
    row = conn.execute("SELECT * FROM gameplay_replay WHERE replay_key=?", (replay_key,)).fetchone()
    if row is None:
        raise RuntimeError(f"gameplay replay {replay_key!r} was rejected by the database")
    record_score(conn, int(row["id"]), replay, commit=False)
    if commit:
        conn.commit()
    return dict(row)


def load_replay(conn: sqlite3.Connection, replay_id: int) -> GameplayReplay:
    row = conn.execute("SELECT * FROM gameplay_replay WHERE id=?", (replay_id,)).fetchone()
    if row is None:
        raise ValueError(f"replay {replay_id} not found")
    return GameplayReplay(int(row["schema_version"] or 3), row["game_id"], row["seed"],
                          row["duration_seconds"], 20, row["simulation_version"] or "1",
                          row["ruleset_version"] or "1", row["renderer_version"] or "1",
                          json.loads(row["payload_json"] or "{}"),
                          json.loads(row["result_json"] or "{}"),
                          json.loads(row["themes_json"] or "[]"))


def apply_replay_stats(conn: sqlite3.Connection, replay_id: int) -> bool:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM gameplay_replay WHERE id=?", (replay_id,)).fetchone()
        if row is None or row["stats_applied"]:
            conn.commit()
            return False
        mark_rendered(conn, replay_id, commit=False)
        conn.execute("UPDATE gameplay_replay SET stats_applied=1 WHERE id=? AND stats_applied=0", (replay_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def create_clip(conn: sqlite3.Connection, profile: str, replay_id: int, duration: float,
                file_path: str | None, *, status: str = "available", patch_id: int | None = None,
                game_id: str | None = None, render_profile: dict | None = None) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO gameplay_clip
           (profile_key, replay_id, duration_seconds, file_path, status, reserved_patch_id,
            game_id, render_profile_json, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (profile, replay_id, duration, file_path, status, patch_id, game_id,
         json.dumps(render_profile) if render_profile else None, now, now))
    conn.commit()
    return int(cur.lastrowid)


def reserve_clips(conn: sqlite3.Connection, profile: str, patch_id: int,
                  required_seconds: float) -> list[dict]:
    """Atomically reserve enough clips. Existing patch reservation makes retry stable."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = conn.execute(
            "SELECT * FROM gameplay_clip WHERE reserved_patch_id=? AND status IN ('reserved','consumed') ORDER BY id",
            (patch_id,),).fetchall()
        if existing:
            conn.commit()
            return [dict(r) for r in existing]
        selected = conn.execute(
            """SELECT * FROM gameplay_clip WHERE profile_key=? AND status='available'
               ORDER BY id""", (profile,),).fetchall()
        total = 0.0
        ids = []
        for row in selected:
            if not row["file_path"] or not Path(row["file_path"]).is_file():
                conn.execute("UPDATE gameplay_clip SET status='failed', error_message='clip file missing', updated_at=? WHERE id=?",
                             (_now(), row["id"]))
                continue
            ids.append(row["id"])
            total += float(row["duration_seconds"])
            if total >= required_seconds:
                break
        if total < required_seconds:
            conn.rollback()
            return []
        token = uuid.uuid4().hex
        placeholders = ",".join("?" for _ in ids)
        conn.execute(
            f"""UPDATE gameplay_clip SET status='reserved', reserved_patch_id=?, reservation_token=?, updated_at=?
                WHERE id IN ({placeholders}) AND status='available'""",
            (patch_id, token, _now(), *ids),
        )
        rows = conn.execute(f"SELECT * FROM gameplay_clip WHERE id IN ({placeholders}) ORDER BY id", ids).fetchall()
        conn.commit()
        return [dict(r) for r in rows]
    except Exception:
        conn.rollback()
        raise


def consume_reserved(conn: sqlite3.Connection, patch_id: int, reservation_token: str | None = None) -> list[str]:
    """Consume a patch lease; schema-3 callers are fenced by their frozen token."""
    where = "reserved_patch_id=? AND status IN ('reserved','consumed')"
    params: list[object] = [patch_id]
    if reservation_token is not None:
        where += " AND reservation_token=?"
        params.append(reservation_token)
    rows = conn.execute(f"SELECT file_path FROM gameplay_clip WHERE {where}", params).fetchall()
    if reservation_token is not None:
        owned = conn.execute(
            "SELECT COUNT(*) FROM gameplay_clip WHERE reserved_patch_id=? AND status IN ('reserved','consumed')",
            (patch_id,)).fetchone()[0]
        if owned and len(rows) != owned:
            raise ValueError("gameplay reservation token mismatch")
    update_where = "reserved_patch_id=? AND status='reserved'"
    update_params: list[object] = [_now(), _now(), patch_id]
    if reservation_token is not None:
        update_where += " AND reservation_token=?"
        update_params.append(reservation_token)
    conn.execute(f"UPDATE gameplay_clip SET status='consumed', consumed_at=?, updated_at=? WHERE {update_where}",
                 update_params)
    conn.commit()
    return [r["file_path"] for r in rows if r["file_path"]]


def recover_reserved_clips(conn: sqlite3.Connection) -> int:
    """Release only orphan reservations; live patch jobs remain fenced to their patch."""
    cur = conn.execute(
        """UPDATE gameplay_clip SET status='available', reserved_patch_id=NULL,
           reservation_token=NULL, updated_at=? WHERE status='reserved' AND NOT EXISTS (
             SELECT 1 FROM job WHERE job.patch_id=gameplay_clip.reserved_patch_id
             AND job.job_type='patch_video' AND job.status IN ('pending','running','cancelling'))""", (_now(),))
    conn.commit()
    return cur.rowcount


def pool_status(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        """SELECT profile_key, game_id, status,
           COUNT(*) AS clip_count, COALESCE(SUM(duration_seconds),0) AS duration_seconds
           FROM gameplay_clip GROUP BY profile_key, game_id, status
           ORDER BY game_id, profile_key, status""")]
