"""CRUD cho Short Video Studio: shorts + short_uploads + credentials FB/TikTok."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.models import Short, ShortUpload

SHORT_STATUSES = {"draft", "rendering", "ready", "publishing", "published", "failed"}
# ffmpeg = pipeline hiện có. remotion = audio/TTS vẫn ffmpeg, chỉ lớp đồ hoạ dọc
# do Remotion render (short_remotion.py gọi `npx remotion render`).
SHORT_RENDERERS = {"ffmpeg", "remotion"}
UPLOAD_PLATFORMS = {"fb", "tiktok", "youtube"}
UPLOAD_STATUSES = {"pending", "processing", "done", "failed"}
SHORT_RESOLUTIONS = {"1080x1920", "1080x1080", "1920x1080"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _short_from_row(row) -> Short:
    return Short(**{k: row[k] for k in row.keys()})


def _upload_from_row(row) -> ShortUpload:
    return ShortUpload(**{k: row[k] for k in row.keys()})


def create_short(conn: sqlite3.Connection, *, book_id: int, script_text: str = "",
                 script_source: str = "manual", duration_target: int = 75,
                 voice_id: str | None = None, music_id: int | None = None,
                 resolution: str = "1080x1920",
                 renderer: str = "ffmpeg") -> Short:
    if script_source not in {"ai", "manual"}:
        raise ValueError("script_source must be ai|manual")
    if resolution not in SHORT_RESOLUTIONS:
        raise ValueError("invalid short resolution (1 khổ/lần)")
    if renderer not in SHORT_RENDERERS:
        raise ValueError("invalid renderer (ffmpeg|remotion)")
    duration_target = max(60, min(90, int(duration_target or 75)))
    now = _now()
    cur = conn.execute(
        """INSERT INTO shorts (book_id, script_text, script_source, duration_target,
                               voice_id, music_id, resolution, render_config_json,
                               caption, story_link, status, renderer, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, '{}', '', '', 'draft', ?, ?, ?)""",
        (book_id, script_text, script_source, duration_target, voice_id,
         music_id, resolution, renderer, now, now),
    )
    conn.commit()
    return get_short(conn, cur.lastrowid)


def get_short(conn: sqlite3.Connection, short_id: int) -> Short | None:
    row = conn.execute("SELECT * FROM shorts WHERE id = ?", (short_id,)).fetchone()
    return _short_from_row(row) if row else None


def list_shorts(conn: sqlite3.Connection, *, book_id: int | None = None,
                limit: int = 100) -> list[Short]:
    if book_id is not None:
        rows = conn.execute(
            "SELECT * FROM shorts WHERE book_id = ? ORDER BY created_at DESC LIMIT ?",
            (book_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM shorts ORDER BY created_at DESC LIMIT ?", (limit,),
        ).fetchall()
    return [_short_from_row(r) for r in rows]


def update_short(conn: sqlite3.Connection, short_id: int, **fields) -> Short | None:
    allowed = {"script_text", "script_source", "duration_target", "voice_id",
               "music_id", "resolution", "render_config_json", "video_path",
               "caption", "story_link", "status", "renderer"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_short(conn, short_id)
    if "script_source" in updates and updates["script_source"] not in {"ai", "manual"}:
        raise ValueError("script_source must be ai|manual")
    if "resolution" in updates and updates["resolution"] not in SHORT_RESOLUTIONS:
        raise ValueError("invalid short resolution (1 khổ/lần)")
    if "status" in updates and updates["status"] not in SHORT_STATUSES:
        raise ValueError("invalid short status")
    if "renderer" in updates and updates["renderer"] not in SHORT_RENDERERS:
        raise ValueError("invalid renderer (ffmpeg|remotion)")
    if "duration_target" in updates:
        updates["duration_target"] = max(60, min(90, int(updates["duration_target"] or 75)))
    updates["updated_at"] = _now()
    conn.execute(
        f"UPDATE shorts SET {', '.join(f'{k}=?' for k in updates)} WHERE id = ?",
        (*updates.values(), short_id),
    )
    conn.commit()
    return get_short(conn, short_id)


def delete_short(conn: sqlite3.Connection, short_id: int) -> bool:
    cur = conn.execute("DELETE FROM shorts WHERE id = ?", (short_id,))
    conn.commit()
    return cur.rowcount > 0


def list_uploads(conn: sqlite3.Connection, short_id: int) -> list[ShortUpload]:
    rows = conn.execute(
        "SELECT * FROM short_uploads WHERE short_id = ? ORDER BY platform",
        (short_id,),
    ).fetchall()
    return [_upload_from_row(r) for r in rows]


def ensure_uploads(conn: sqlite3.Connection, short_id: int) -> list[ShortUpload]:
    """Tạo 3 dòng fb|tiktok|youtube nếu chưa có (idempotent)."""
    now = _now()
    for platform in ("fb", "tiktok", "youtube"):
        conn.execute(
            """INSERT INTO short_uploads (short_id, platform, status, created_at)
               VALUES (?, ?, 'pending', ?)
               ON CONFLICT(short_id, platform) DO NOTHING""",
            (short_id, platform, now),
        )
    conn.commit()
    return list_uploads(conn, short_id)


def set_upload_status(conn: sqlite3.Connection, short_id: int, platform: str,
                      status: str, *, platform_video_id: str | None = None,
                      error_message: str | None = None) -> ShortUpload | None:
    if platform not in UPLOAD_PLATFORMS:
        raise ValueError("platform must be fb|tiktok|youtube")
    if status not in UPLOAD_STATUSES:
        raise ValueError("invalid upload status")
    conn.execute(
        """UPDATE short_uploads SET status=?, platform_video_id=COALESCE(?, platform_video_id),
                  error_message=? WHERE short_id=? AND platform=?""",
        (status, platform_video_id, error_message, short_id, platform),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM short_uploads WHERE short_id=? AND platform=?",
        (short_id, platform),
    ).fetchone()
    return _upload_from_row(row) if row else None


def set_upload_account(conn: sqlite3.Connection, short_id: int, platform: str,
                       account_id: int | None) -> None:
    """Chọn tài khoản (social_account.id) sẽ đăng short này lên một kênh; None = mặc định."""
    if platform not in UPLOAD_PLATFORMS:
        raise ValueError("platform must be fb|tiktok|youtube")
    conn.execute("UPDATE short_uploads SET account_id=? WHERE short_id=? AND platform=?",
                 (account_id, short_id, platform))
    conn.commit()


def get_upload(conn: sqlite3.Connection, short_id: int, platform: str) -> ShortUpload | None:
    row = conn.execute(
        "SELECT * FROM short_uploads WHERE short_id=? AND platform=?",
        (short_id, platform),
    ).fetchone()
    return _upload_from_row(row) if row else None


def reset_upload(conn: sqlite3.Connection, short_id: int, platform: str) -> ShortUpload | None:
    return set_upload_status(conn, short_id, platform, "pending", error_message=None)


def get_render_config(short: Short) -> dict:
    try:
        cfg = json.loads(short.render_config_json or "{}")
        return cfg if isinstance(cfg, dict) else {}
    except (ValueError, TypeError):
        return {}
