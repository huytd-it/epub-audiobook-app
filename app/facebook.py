"""Facebook Page video/Reels upload qua Graph API (Short Video Studio + Socials hub).

Mỗi Page là một dòng social_account (platform='facebook', external_id = page_id) giữ
Page Access Token dán tay; fallback .env FACEBOOK_PAGE_ID / FACEBOOK_PAGE_ACCESS_TOKEN
khi chưa có Page nào. Đăng ngay (published), không lên lịch riêng từng kênh. Reels
<=90s tự nhận dạng khi đủ điều kiện.

Cần duyệt app + quyền pages_manage_posts/pages_read_engagement — xem README.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import requests

from app import egress, social_accounts
from app.config import settings

logger = logging.getLogger(__name__)

UPLOAD_TIMEOUT = 300


def is_configured() -> bool:
    return bool((settings.facebook_page_id and settings.facebook_page_access_token))


def get_credentials(conn: sqlite3.Connection, account_id: int | None = None) -> dict | None:
    """Credentials của Page được chỉ định, hoặc Page mặc định khi account_id là None."""
    account = social_accounts.resolve(conn, "facebook", account_id)
    if account is not None:
        return {"id": account["id"],
                "page_id": account["external_id"],
                "page_name": account["display_name"],
                "page_access_token": account["access_token"]}
    if account_id is None and is_configured():
        return {"id": None,
                "page_id": settings.facebook_page_id,
                "page_name": None,
                "page_access_token": settings.facebook_page_access_token}
    return None


def save_credentials(conn: sqlite3.Connection, *, page_id: str, page_name: str | None,
                     page_access_token: str) -> int:
    """Thêm một Page, hoặc cập nhật token khi page_id đã có. Trả về social_account id."""
    return social_accounts.upsert(conn, "facebook", external_id=page_id,
                                  display_name=page_name, access_token=page_access_token)


def delete_credentials(conn: sqlite3.Connection, account_id: int | None = None) -> None:
    if account_id is None:
        social_accounts.delete_all(conn, "facebook")
    else:
        social_accounts.delete(conn, account_id)


def _api_base() -> str:
    return f"https://graph.facebook.com/{settings.facebook_api_version or 'v21.0'}"


def fetch_page(*, page_id: str, token: str, http: requests.Session | None = None) -> dict:
    """Thông tin Page ({id, name}) — cũng là cách kiểm tra token còn dùng được."""
    resp = (http or requests).get(
        f"{_api_base()}/{page_id}",
        params={"fields": "id,name", "access_token": token},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def list_page_videos(*, page_id: str, token: str, limit: int = 25,
                     http: requests.Session | None = None) -> list[dict]:
    """Video/Reels gần đây của Page, mới nhất trước."""
    resp = (http or requests).get(
        f"{_api_base()}/{page_id}/videos",
        params={"fields": "id,title,description,created_time,length,permalink_url,picture",
                "limit": max(1, min(limit, 100)), "access_token": token},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("data") or []


def upload_reel(*, page_id: str, token: str, video_path: str,
                caption: str, story_link: str = "",
                http: requests.Session | None = None) -> str:
    """Upload video lên Page (Reels khi dọc ngắn). Trả về id (video/post id).

    Dùng endpoint /{page_id}/videos với upload_phase=start/transfer/finish cho
    file lớn; ở đây dùng resumable đơn giản qua requests. Caption chung + link
    text (FB cho link clickable trong mô tả). `http` là session đã gắn proxy của
    tài khoản (xem app/egress.py); cả ba pha phải ra cùng một IP.
    """
    http = http or requests
    path = Path(video_path)
    if not path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")
    description = caption.strip()
    if story_link.strip():
        description = f"{description}\n{story_link.strip()}".strip()
    # Phase start
    start = http.post(
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
            transfer = http.post(
                f"{_api_base()}/{video_id}",
                data={"access_token": token, "upload_phase": "transfer",
                      "upload_session_id": upload_session_id,
                      "start_offset": 0},
                files={"video_file_chunk": (path.name, fh)},
                timeout=UPLOAD_TIMEOUT,
            )
        transfer.raise_for_status()
        finish = http.post(
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
        resp = http.post(
            f"{_api_base()}/{page_id}/videos",
            data={"access_token": token, "description": description[:2000]},
            files={"source": (path.name, fh, "video/mp4")},
            timeout=UPLOAD_TIMEOUT,
        )
    resp.raise_for_status()
    return str(resp.json().get("id") or resp.json().get("post_id") or "")


def publish_short_video(conn: sqlite3.Connection, video_path: str, caption: str,
                        story_link: str = "", account_id: int | None = None) -> str:
    creds = get_credentials(conn, account_id)
    if not creds:
        raise ValueError("Facebook chưa kết nối (thiếu Page Access Token)")
    with egress.session(conn, "facebook", creds["id"]) as http:
        return upload_reel(page_id=creds["page_id"] or settings.facebook_page_id,
                           token=creds["page_access_token"],
                           video_path=video_path, caption=caption,
                           story_link=story_link, http=http)
