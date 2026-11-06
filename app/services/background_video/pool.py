"""SQLite persistence for the keyword-driven background video pool."""
from __future__ import annotations

import json
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def bg_root() -> Path:
    raw = settings.bg_video_root.strip() if settings.bg_video_root else ""
    root = Path(raw) if raw else Path(settings.data_root) / "backgrounds"
    root.mkdir(parents=True, exist_ok=True)
    return root


def keyword_dir(keyword: str) -> Path:
    d = bg_root() / (keyword or settings.bg_video_default_keyword)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _probe_duration(path: str) -> float:
    try:
        result = subprocess.run(
            [
                settings.get_ffprobe_path(), "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1", path,
            ],
            capture_output=True, text=True, check=True, timeout=10,
        )
        return float(result.stdout.strip() or 0)
    except Exception:
        return 0.0


@dataclass
class ClipCandidate:
    keyword: str
    platform: str
    source_url: str
    author: str = ""
    title: str = ""
    tags: list[str] | None = None
    license_note: str = ""
    duration_seconds: float = 0.0
    width: int = 0
    height: int = 0
    fps: int = 0
    p_hash: str = ""
    # httpx streaming callback: writes into target path lazily (for big files).
    download_to: object | None = None  # callable(Path) -> int
    file_bytes: bytes = b""


def save_clip(conn: sqlite3.Connection, clip: ClipCandidate) -> dict:
    """Persist a raw clip into data/backgrounds/<keyword>/<clip_id>.mp4 + SQLite."""
    if not clip.source_url:
        raise ValueError("source_url is required")
    existed = conn.execute(
        "SELECT * FROM background_clip WHERE source_url=?", (clip.source_url,)
    ).fetchone()
    if existed is not None:
        return dict(existed)

    clip_id = uuid.uuid4().hex
    dest_dir = keyword_dir(clip.keyword)
    dest = dest_dir / f"{clip_id}.mp4"

    if clip.download_to is not None:
        size = int(clip.download_to(dest))
        if not dest.exists() or dest.stat().st_size == 0:
            raise RuntimeError(f"empty file downloaded from {clip.source_url}")
    elif clip.file_bytes:
        dest.write_bytes(clip.file_bytes)
        size = len(clip.file_bytes)
    else:
        raise ValueError("no bytes to save (file_bytes or download_to required)")

    # A source can "succeed" while returning something that is not a video at
    # all (an HTML error page, or a stub/truncated file with only an ftyp
    # header). Keeping it hands ffmpeg an input it aborts on with
    # AVERROR_INVALIDDATA during a later render, so reject it here instead -
    # always, even when the caller already reports a duration.
    probed = _probe_duration(str(dest))
    if probed <= 0:
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"downloaded file is not a decodable video: {clip.source_url}")

    duration = clip.duration_seconds or probed
    now = _now()
    conn.execute(
        """INSERT INTO background_clip
           (id,keyword,platform,source_url,author,title,path,duration_seconds,
            width,height,fps,p_hash,usage_count,last_used_at,tags_json,
            license_note,status,created_at,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?,?,?)""",
        (clip_id, clip.keyword, clip.platform, clip.source_url, clip.author,
         clip.title, str(dest), duration, clip.width, clip.height, clip.fps,
         clip.p_hash, 0, json.dumps(clip.tags or [], ensure_ascii=False),
         clip.license_note, "available", now, now),
    )
    conn.commit()
    saved = dict(conn.execute("SELECT * FROM background_clip WHERE id=?", (clip_id,)).fetchone())
    saved["file_size_bytes"] = dest.stat().st_size if dest.exists() else 0
    return saved


def list_clips(
    conn: sqlite3.Connection,
    *,
    keyword: str | None = None,
    platform: str | None = None,
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    where, params = [], []
    if keyword:
        where.append("keyword=?"); params.append(keyword)
    if platform:
        where.append("platform=?"); params.append(platform)
    if status:
        where.append("status=?"); params.append(status)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM background_clip {clause} ORDER BY keyword, usage_count, created_at LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )]


def pool_stats(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """SELECT keyword,
                  COUNT(*) AS clip_count,
                  COALESCE(SUM(duration_seconds),0) AS duration_seconds,
                  COALESCE(SUM(usage_count),0) AS total_uses
           FROM background_clip
           WHERE status='available'
           GROUP BY keyword
           ORDER BY keyword"""
    ).fetchall()
    total_clips = 0
    total_duration = 0.0
    by_keyword = {}
    for r in rows:
        by_keyword[r["keyword"]] = {
            "clip_count": int(r["clip_count"]),
            "duration_seconds": float(r["duration_seconds"]),
            "total_uses": int(r["total_uses"]),
        }
        total_clips += int(r["clip_count"])
        total_duration += float(r["duration_seconds"])
    return {"by_keyword": by_keyword, "total_clips": total_clips, "total_duration_seconds": total_duration}


def pick_clips(
    conn: sqlite3.Connection,
    keyword: str,
    *,
    count: int = 1,
    seed: int | None = None,
    exclude_ids: set[str] | None = None,
) -> list[dict]:
    """Least-used-first pick within a keyword pool, avoiding repeats per book."""
    exclude = exclude_ids or set()
    rows = list_clips(conn, keyword=keyword, status="available", limit=1000)
    rows = [r for r in rows if r["id"] not in exclude]
    if not rows:
        return []
    rows.sort(key=lambda r: (r["usage_count"], r["id"]))
    if seed is not None:
        import hashlib

        def rank(r: dict) -> tuple[int, int]:
            digest = hashlib.sha256(f"{seed}:{r['id']}".encode()).hexdigest()
            return (r["usage_count"], int(digest[:8], 16))

        rows.sort(key=rank)
    picked = rows[: max(1, count)]
    mark_used(conn, [r["id"] for r in picked])
    return [dict(conn.execute("SELECT * FROM background_clip WHERE id=?", (r["id"],)).fetchone()) for r in picked]


def mark_used(conn: sqlite3.Connection, clip_ids: list[str]) -> None:
    now = _now()
    for cid in clip_ids:
        conn.execute(
            "UPDATE background_clip SET usage_count=usage_count+1, last_used_at=?, updated_at=? WHERE id=?",
            (now, now, cid),
        )
    conn.commit()


def write_manifest(conn: sqlite3.Connection, keyword: str) -> dict:
    rows = list_clips(conn, keyword=keyword, status="available", limit=5000)
    manifest = {
        "keyword": keyword,
        "generated_at": _now(),
        "clips": [
            {"id": r["id"], "path": r["path"], "duration_seconds": r["duration_seconds"],
             "platform": r["platform"], "source_url": r["source_url"],
             "author": r["author"], "title": r["title"],
             "usage_count": r["usage_count"], "p_hash": r["p_hash"]}
            for r in rows
        ],
    }
    dest = keyword_dir(keyword) / "manifest.json"
    dest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest
