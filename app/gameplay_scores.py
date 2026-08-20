"""Scoring and ranking for the retro gameplay family.

Every simulated retro clip is one arcade run: it lands in ``gameplay_score`` the moment the
replay is written, carries the tier the video actually shows, and is flagged ``rendered``
once the clip encodes. Ranking across different games compares each run against the best
run of *its own* game (a Tetris score and a Breakout score are not the same currency), so
the overall board uses a 0–1000 rating instead of the raw number.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.gameplay_models import GameplayReplay
from app.gameplay_retro import is_retro, player_tag, rank_tier

BOARD_LIMIT = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def high_score(conn: sqlite3.Connection, game_id: str) -> int:
    row = conn.execute("SELECT MAX(score) FROM gameplay_score WHERE game_id=?", (game_id,)).fetchone()
    return int(row[0] or 0)


def record_score(conn: sqlite3.Connection, replay_id: int, replay: GameplayReplay, *,
                 commit: bool = True) -> dict | None:
    """Log one run. Idempotent: a replay row owns at most one score row."""
    if not is_retro(replay.game_id):
        return None
    result = replay.result or {}
    metrics = dict(result.get("metrics") or {})
    score = int(result.get("score") or metrics.get("score") or 0)
    reference = int(replay.payload.get("hi_score") or 0)
    conn.execute(
        """INSERT OR IGNORE INTO gameplay_score
           (replay_id, game_id, seed, player_tag, score, total_score, level, games, deaths,
            duration_seconds, rank_tier, metrics_json, rendered, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,?)""",
        (replay_id, replay.game_id, replay.seed,
         str(result.get("player") or player_tag(replay.seed)), score,
         int(metrics.get("total_score") or score), int(metrics.get("level") or 1),
         int(metrics.get("games") or 1), int(metrics.get("deaths") or 0),
         float(replay.duration_seconds), rank_tier(score, reference),
         json.dumps(metrics, ensure_ascii=False), _now()))
    if commit:
        conn.commit()
    row = conn.execute("SELECT * FROM gameplay_score WHERE replay_id=?", (replay_id,)).fetchone()
    return dict(row) if row else None


def mark_rendered(conn: sqlite3.Connection, replay_id: int, *, commit: bool = True) -> bool:
    cur = conn.execute(
        "UPDATE gameplay_score SET rendered=1, rendered_at=? WHERE replay_id=? AND rendered=0",
        (_now(), replay_id))
    if commit:
        conn.commit()
    return bool(cur.rowcount)


_SELECT = """
    SELECT s.*, r.replay_key,
           CAST(ROUND(1000.0 * s.score / MAX(1, b.best)) AS INTEGER) AS rating
    FROM gameplay_score s
    JOIN (SELECT game_id, MAX(score) AS best FROM gameplay_score GROUP BY game_id) b
      ON b.game_id = s.game_id
    LEFT JOIN gameplay_replay r ON r.id = s.replay_id
"""


def leaderboard(conn: sqlite3.Connection, game_id: str | None = None,
                limit: int = BOARD_LIMIT) -> list[dict]:
    """Top runs, ranked by raw score inside one game and by rating across the catalog."""
    limit = max(1, min(int(limit), 100))
    if game_id:
        rows = conn.execute(f"{_SELECT} WHERE s.game_id=? ORDER BY s.score DESC, s.id LIMIT ?",
                            (game_id, limit)).fetchall()
    else:
        rows = conn.execute(f"{_SELECT} ORDER BY rating DESC, s.score DESC, s.id LIMIT ?",
                            (limit,)).fetchall()
    entries = []
    for position, row in enumerate(rows, start=1):
        entry = dict(row)
        entry["position"] = position
        entry["metrics"] = json.loads(entry.pop("metrics_json") or "{}")
        entries.append(entry)
    return entries


def standings(conn: sqlite3.Connection) -> list[dict]:
    """Per-game cabinet summary: best run, average and how many runs are on the board."""
    rows = conn.execute(
        """SELECT game_id, COUNT(*) AS runs, MAX(score) AS best,
                  CAST(ROUND(AVG(score)) AS INTEGER) AS average,
                  SUM(rendered) AS rendered, SUM(deaths) AS deaths,
                  MAX(level) AS top_level, MAX(created_at) AS last_run_at
           FROM gameplay_score GROUP BY game_id ORDER BY best DESC""").fetchall()
    summary = []
    for row in rows:
        entry = dict(row)
        holder = conn.execute(
            "SELECT player_tag FROM gameplay_score WHERE game_id=? ORDER BY score DESC, id LIMIT 1",
            (entry["game_id"],)).fetchone()
        entry["champion"] = holder["player_tag"] if holder else ""
        summary.append(entry)
    return summary


__all__ = ["BOARD_LIMIT", "high_score", "leaderboard", "mark_rendered", "record_score",
           "standings"]
