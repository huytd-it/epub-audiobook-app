"""TikTok Content Posting API cho Short Video Studio.

MVP: FILE_UPLOAD (upload binary rồi publish) với access token lưu DB (mirror
youtube_credentials) + fallback .env TIKTOK_ACCESS_TOKEN. PULL_FROM_URL giữ
làm dự phòng khi file đã public. Đăng ngay (draft/private theo mặc định),
không lịch riêng từng kênh.

Lưu ý: caption TikTok/Reels thường KHÔNG clickable — story_link vẫn lưu dạng
text, fallback bio/comment do user tự làm (phase 1 giữ text link).
Cần duyệt app + OAuth scope video.upload — xem README.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.config import settings

logger = logging.getLogger(__name__)

_API_BASE = "https://open.tiktokapis.com/v2"
UPLOAD_TIMEOUT = 300

# Giới hạn TikTok phổ biến (giữ ở code để fail-fast trước khi gọi API).
MAX_DURATION_SEC = 600
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_configured() -> bool:
    return bool(settings.tiktok_access_token or settings.tiktok_client_key)


def get_credentials(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM tiktok_credentials ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        return dict(row)
    if settings.tiktok_access_token:
        return {"open_id": "", "display_name": None,
                "access_token": settings.tiktok_access_token,
                "refresh_token": "", "token_expiry": ""}
    return None


def save_credentials(conn: sqlite3.Connection, *, open_id: str = "",
                     display_name: str | None = None, access_token: str,
                     refresh_token: str = "", token_expiry: str = "") -> None:
    now = _now()
    existing = conn.execute("SELECT id FROM tiktok_credentials LIMIT 1").fetchone()
    if existing:
        conn.execute(
            """UPDATE tiktok_credentials SET open_id=?, display_name=?,
               access_token=?, refresh_token=?, token_expiry=?, updated_at=?
               WHERE id=?""",
            (open_id, display_name, access_token, refresh_token,
             token_expiry, now, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO tiktok_credentials
               (open_id, display_name, access_token, refresh_token,
                token_expiry, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (open_id, display_name, access_token, refresh_token,
             token_expiry, now, now),
        )
    conn.commit()


def delete_credentials(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM tiktok_credentials")
    conn.commit()


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def upload_file(*, token: str, video_path: str, caption: str,
                story_link: str = "") -> str:
    """FILE_UPLOAD: init -> upload binary -> publish. Trả về publish_id/video_id."""
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    size = path.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError("Video vượt giới hạn TikTok (4GB)")
    text = caption.strip()
    if story_link.strip():
        # TikTok không cho link clickable trong caption — vẫn giữ text.
        text = f"{text} {story_link.strip()}".strip()[:2200]
    init = requests.post(
        f"{_API_BASE}/post/publish/video/init/",
        headers={**_headers(token), "Content-Type": "application/json"},
        json={"post_info": {"title": text[:2200], "privacy_level": "SELF_ONLY"},
              "source_info": {"source": "FILE_UPLOAD",
                              "video_size": size,
                              "chunk_size": 10 * 1024 * 1024,
                              "total_chunk_count": 1}},
        timeout=60,
    )
    init.raise_for_status()
    data = (init.json().get("data") or {})
    upload_url = data.get("upload_url") or ""
    publish_id = data.get("publish_id") or ""
    if upload_url:
        with path.open("rb") as fh:
            put = requests.put(upload_url,
                               data=fh,
                               headers={"Content-Type": "video/mp4"},
                               timeout=UPLOAD_TIMEOUT)
        put.raise_for_status()
    return str(publish_id or data.get("video_id") or "")


def publish_short_video(conn: sqlite3.Connection, video_path: str, caption: str,
                        story_link: str = "") -> str:
    creds = get_credentials(conn)
    if not creds:
        raise ValueError("TikTok chưa kết nối (thiếu access token)")
    return upload_file(token=creds["access_token"], video_path=video_path,
                       caption=caption, story_link=story_link)
