"""TikTok Content Posting API cho Short Video Studio + Socials hub.

Mỗi tài khoản là một dòng social_account (platform='tiktok', external_id = open_id)
giữ access token dán tay; fallback .env TIKTOK_ACCESS_TOKEN khi chưa có tài khoản
nào. FILE_UPLOAD (upload binary rồi publish); PULL_FROM_URL giữ làm dự phòng khi file
đã public. Đăng ngay (draft/private theo mặc định), không lịch riêng từng kênh.

Lưu ý: caption TikTok/Reels thường KHÔNG clickable — story_link vẫn lưu dạng
text, fallback bio/comment do user tự làm (phase 1 giữ text link).
Cần duyệt app + OAuth scope video.upload — xem README.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

import requests

from app import egress, social_accounts
from app.config import settings

logger = logging.getLogger(__name__)

_API_BASE = "https://open.tiktokapis.com/v2"
UPLOAD_TIMEOUT = 300

# Giới hạn TikTok phổ biến (giữ ở code để fail-fast trước khi gọi API).
MAX_DURATION_SEC = 600
MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024


def is_configured() -> bool:
    return bool(settings.tiktok_access_token or settings.tiktok_client_key)


def get_credentials(conn: sqlite3.Connection, account_id: int | None = None) -> dict | None:
    """Credentials của tài khoản được chỉ định, hoặc mặc định khi account_id là None."""
    account = social_accounts.resolve(conn, "tiktok", account_id)
    if account is not None:
        return {"id": account["id"],
                "open_id": account["external_id"],
                "display_name": account["display_name"],
                "access_token": account["access_token"],
                "refresh_token": account["refresh_token"],
                "token_expiry": account["token_expiry"]}
    if account_id is None and settings.tiktok_access_token:
        return {"id": None, "open_id": "", "display_name": None,
                "access_token": settings.tiktok_access_token,
                "refresh_token": "", "token_expiry": ""}
    return None


def save_credentials(conn: sqlite3.Connection, *, open_id: str = "",
                     display_name: str | None = None, access_token: str,
                     refresh_token: str = "", token_expiry: str = "") -> int:
    """Thêm một tài khoản, hoặc cập nhật token khi open_id đã có. Trả về social_account id.

    open_id rỗng (token dán tay không kèm open_id) dùng chung một dòng, đúng như bảng
    một-tài-khoản trước đây; muốn nhiều tài khoản thì phải nhập open_id để phân biệt.
    """
    return social_accounts.upsert(
        conn, "tiktok", external_id=open_id, display_name=display_name,
        access_token=access_token, refresh_token=refresh_token, token_expiry=token_expiry,
    )


def delete_credentials(conn: sqlite3.Connection, account_id: int | None = None) -> None:
    if account_id is None:
        social_accounts.delete_all(conn, "tiktok")
    else:
        social_accounts.delete(conn, account_id)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def upload_file(*, token: str, video_path: str, caption: str,
                story_link: str = "", http: requests.Session | None = None) -> str:
    """FILE_UPLOAD: init -> upload binary -> publish. Trả về publish_id/video_id.

    `http` là session đã gắn proxy của tài khoản (xem app/egress.py).
    """
    http = http or requests
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
    init = http.post(
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
            put = http.put(upload_url,
                           data=fh,
                           headers={"Content-Type": "video/mp4"},
                           timeout=UPLOAD_TIMEOUT)
        put.raise_for_status()
    return str(publish_id or data.get("video_id") or "")


def publish_short_video(conn: sqlite3.Connection, video_path: str, caption: str,
                        story_link: str = "", account_id: int | None = None) -> str:
    creds = get_credentials(conn, account_id)
    if not creds:
        raise ValueError("TikTok chưa kết nối (thiếu access token)")
    with egress.session(conn, "tiktok", creds["id"]) as http:
        return upload_file(token=creds["access_token"], video_path=video_path,
                           caption=caption, story_link=story_link, http=http)
