"""Facebook Page video/Reels upload qua Graph API (Short Video Studio).

MVP: Page Access Token lưu DB (mirror youtube_credentials) + fallback .env
FACEBOOK_PAGE_ID / FACEBOOK_PAGE_ACCESS_TOKEN. Đăng ngay (published), không
lên lịch riêng từng kênh. Reels <=90s tự nhận dạng khi đủ điều kiện.

Cần duyệt app + quyền pages_manage_posts/pages_read_engagement — xem README.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import requests

from app.config import settings

logger = logging.getLogger(__name__)

UPLOAD_TIMEOUT = 300


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_configured() -> bool:
    return bool((settings.facebook_page_id and settings.facebook_page_access_token))


def get_credentials(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM facebook_credentials ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is not None:
        return dict(row)
    if is_configured():
        return {"page_id": settings.facebook_page_id,
                "page_name": None,
                "page_access_token": settings.facebook_page_access_token}
    return None


def save_credentials(conn: sqlite3.Connection, *, page_id: str, page_name: str | None,
                     page_access_token: str) -> None:
    now = _now()
    existing = conn.execute("SELECT id FROM facebook_credentials LIMIT 1").fetchone()
    if existing:
        conn.execute(
            """UPDATE facebook_credentials SET page_id=?, page_name=?,
               page_access_token=?, updated_at=? WHERE id=?""",
            (page_id, page_name, page_access_token, now, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO facebook_credentials
               (page_id, page_name, page_access_token, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)""",
            (page_id, page_name, page_access_token, now, now),
        )
    conn.commit()


def delete_credentials(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM facebook_credentials")
    conn.commit()


def _api_base() -> str:
    return f"https://graph.facebook.com/{settings.facebook_api_version or 'v21.0'}"


def upload_reel(*, page_id: str, token: str, video_path: str,
                caption: str, story_link: str = "") -> str:
    """Upload video lên Page (Reels khi dọc ngắn). Trả về id (video/post id).

    Dùng endpoint /{page_id}/videos với upload_phase=start/transfer/finish cho
    file lớn; ở đây dùng resumable đơn giản qua requests. Caption chung + link
    text (FB cho link clickable trong mô tả).
    """
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    description = caption.strip()
    if story_link.strip():
        description = f"{description}\n{story_link.strip()}".strip()
    # Phase start
    start = requests.post(
        f"{_api_base()}/{page_id}/videos",
        data={"access_token": token, "upload_phase": "start",
              "file_size": path.stat().st_size},
        timeout=60,
    )
    start.raise_for_status()
    payload = start.json()
    upload_session_id = payload.get("upload_session_id") or payload.get("id")
    video_id = payload.get("video_id") or payload.get("id")
    if upload_session_id and video_id:
        # transfer
        with path.open("rb") as fh:
            transfer = requests.post(
                f"{_api_base()}/{video_id}",
                data={"access_token": token, "upload_phase": "transfer",
                      "upload_session_id": upload_session_id,
                      "start_offset": 0},
                files={"video_file_chunk": (path.name, fh)},
                timeout=UPLOAD_TIMEOUT,
            )
        transfer.raise_for_status()
        finish = requests.post(
            f"{_api_base()}/{video_id}",
            data={"access_token": token, "upload_phase": "finish",
                  "upload_session_id": upload_session_id,
                  "description": description[:2000]},
            timeout=60,
        )
        finish.raise_for_status()
        return str(video_id)
    # Fallback: single-shot upload cho file nhỏ / Graph cũ
    with path.open("rb") as fh:
        resp = requests.post(
            f"{_api_base()}/{page_id}/videos",
            data={"access_token": token, "description": description[:2000]},
            files={"source": (path.name, fh, "video/mp4")},
            timeout=UPLOAD_TIMEOUT,
        )
    resp.raise_for_status()
    return str(resp.json().get("id") or resp.json().get("post_id") or "")


def publish_short_video(conn: sqlite3.Connection, video_path: str, caption: str,
                        story_link: str = "") -> str:
    creds = get_credentials(conn)
    if not creds:
        raise ValueError("Facebook chưa kết nối (thiếu Page Access Token)")
    return upload_reel(page_id=creds["page_id"] or settings.facebook_page_id,
                       token=creds["page_access_token"],
                       video_path=video_path, caption=caption,
                       story_link=story_link)
