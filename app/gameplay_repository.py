"""SQLite persistence for gameplay themes, replay logs, statistics, and clip leases."""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.gameplay_config import BUILTIN_THEME_ID, BUILTIN_THEME_VERSION
from app.gameplay_models import Fighter, Replay

_NAMES = (
    "Axel", "Blaze", "Cruz", "Dash", "Echo", "Flint", "Gray", "Hawk",
    "Ivan", "Jett", "Knox", "Leon", "Mako", "Nash", "Orion", "Pax",
    "Quinn", "Rex", "Sage", "Troy", "Uma", "Vex", "Wade", "Xeno",
    "York", "Zane", "Arlo", "Bram", "Cleo", "Duke", "Enzo", "Finn",
    "Gale", "Hugo", "Iris", "Jax", "Kira", "Luca", "Mira", "Nova",
    "Odin", "Pia", "Rhea", "Sven", "Tala", "Uri", "Vera", "Wolf",
)


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
    classes = ("tank", "assassin", "ranger")
    for index, name in enumerate(_NAMES):
        key = name.lower()
        conn.execute(
            """INSERT OR IGNORE INTO gameplay_fighter
               (fighter_key, name, class_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)""",
            (key, name, classes[index % 3], now, now),
        )
    conn.commit()


def list_fighters(conn: sqlite3.Connection) -> list[Fighter]:
    return [Fighter(r["fighter_key"], r["name"], r["class_name"]) for r in conn.execute(
        "SELECT fighter_key, name, class_name FROM gameplay_fighter ORDER BY id")]


def list_themes(conn: sqlite3.Connection, *, enabled_only: bool = False) -> list[dict]:
    where = "WHERE enabled=1" if enabled_only else ""
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM gameplay_theme {where} ORDER BY builtin DESC, name, version DESC")]


def save_replay(conn: sqlite3.Connection, replay_key: str, replay: Replay) -> dict:
    now = _now()
    conn.execute(
        """INSERT OR IGNORE INTO gameplay_replay
           (replay_key, seed, duration_seconds, roster_json, themes_json, map_json,
            events_json, top3_json, winner_key, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (replay_key, replay.seed, replay.duration_seconds, json.dumps(replay.roster),
         json.dumps(replay.themes), json.dumps(replay.map), json.dumps(replay.events),
         json.dumps(replay.top3), replay.winner_key, now),
    )
    row = conn.execute("SELECT * FROM gameplay_replay WHERE replay_key=?", (replay_key,)).fetchone()
    conn.commit()
    return dict(row)


def load_replay(conn: sqlite3.Connection, replay_id: int) -> Replay:
    row = conn.execute("SELECT * FROM gameplay_replay WHERE id=?", (replay_id,)).fetchone()
    if row is None:
        raise ValueError(f"replay {replay_id} not found")
    return Replay(row["seed"], row["duration_seconds"], json.loads(row["roster_json"]),
                  json.loads(row["themes_json"]), json.loads(row["map_json"]),
                  json.loads(row["events_json"]), json.loads(row["top3_json"]), row["winner_key"])


def apply_replay_stats(conn: sqlite3.Connection, replay_id: int) -> bool:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM gameplay_replay WHERE id=?", (replay_id,)).fetchone()
        if row is None or row["stats_applied"]:
            conn.commit()
            return False
        roster = json.loads(row["roster_json"])
        for fighter in roster:
            conn.execute(
                """UPDATE gameplay_fighter SET matches=matches+1,
                   wins=wins+?, eliminations=eliminations+?, updated_at=? WHERE fighter_key=?""",
                (1 if fighter["key"] == row["winner_key"] else 0,
                 int(fighter.get("eliminations", 0)), _now(), fighter["key"]),
            )
        conn.execute("UPDATE gameplay_replay SET stats_applied=1 WHERE id=? AND stats_applied=0", (replay_id,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def create_clip(conn: sqlite3.Connection, profile: str, replay_id: int, duration: float,
                file_path: str | None, *, status: str = "available", patch_id: int | None = None) -> int:
    now = _now()
    cur = conn.execute(
        """INSERT INTO gameplay_clip
           (profile_key, replay_id, duration_seconds, file_path, status, reserved_patch_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (profile, replay_id, duration, file_path, status, patch_id, now, now))
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


def consume_reserved(conn: sqlite3.Connection, patch_id: int) -> list[str]:
    rows = conn.execute(
        "SELECT file_path FROM gameplay_clip WHERE reserved_patch_id=? AND status IN ('reserved','consumed')", (patch_id,)).fetchall()
    conn.execute(
        "UPDATE gameplay_clip SET status='consumed', consumed_at=?, updated_at=? WHERE reserved_patch_id=? AND status='reserved'",
        (_now(), _now(), patch_id),)
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
        """SELECT profile_key, status, COUNT(*) AS clip_count,
           COALESCE(SUM(duration_seconds),0) AS duration_seconds
           FROM gameplay_clip GROUP BY profile_key, status ORDER BY profile_key, status""")]
