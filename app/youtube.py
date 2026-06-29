"""YouTube Data API v3 integration: OAuth2 flow, video upload, token management."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    _GOOGLE_IMPORTS_OK = True
except ModuleNotFoundError:
    _GOOGLE_IMPORTS_OK = False

from app.config import settings

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
_API_SERVICE_NAME = "youtube"
_API_VERSION = "v3"
_UPLOAD_CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB


def _require_google_imports() -> None:
    if not _GOOGLE_IMPORTS_OK:
        raise ModuleNotFoundError(
            "Missing Google API packages. Install: pip install google-auth google-auth-oauthlib google-api-python-client"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_configured() -> bool:
    return bool(settings.youtube_client_id and settings.youtube_client_secret)


def get_creds_from_db(conn: sqlite3.Connection) -> dict | None:
    """Return the stored YouTube credentials row, or None."""
    row = conn.execute(
        "SELECT * FROM youtube_credentials ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    return dict(row)


def save_credentials(
    conn: sqlite3.Connection,
    access_token: str,
    refresh_token: str,
    token_expiry: str,
    channel_id: str | None = None,
    channel_name: str | None = None,
) -> None:
    """Upsert YouTube credentials (single-row table)."""
    existing = conn.execute("SELECT id FROM youtube_credentials LIMIT 1").fetchone()
    now = _now_iso()
    if existing:
        conn.execute(
            """UPDATE youtube_credentials
               SET access_token=?, refresh_token=?, token_expiry=?,
                   channel_id=?, channel_name=?, updated_at=?
               WHERE id=?""",
            (access_token, refresh_token, token_expiry,
             channel_id, channel_name, now, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO youtube_credentials
               (access_token, refresh_token, token_expiry, channel_id, channel_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (access_token, refresh_token, token_expiry,
             channel_id, channel_name, now, now),
        )
    conn.commit()


def delete_credentials(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM youtube_credentials")
    conn.commit()


def _build_credentials(row: dict) -> Credentials:
    _require_google_imports()
    return Credentials(
        token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.youtube_client_id,
        client_secret=settings.youtube_client_secret,
        scopes=_SCOPES,
    )


def _refresh_if_needed(conn: sqlite3.Connection, creds_row: dict) -> Credentials:
    """Build Credentials, refresh if expired, and persist new tokens."""
    _require_google_imports()
    creds = _build_credentials(creds_row)
    if creds.expired or not creds.valid:
        try:
            creds.refresh(Request())
        except Exception:
            logger.exception("YouTube token refresh failed")
            raise
        expiry_str = creds.expiry.isoformat() if creds.expiry else creds_row["token_expiry"]
        save_credentials(
            conn,
            access_token=creds.token or "",
            refresh_token=creds.refresh_token or creds_row["refresh_token"],
            token_expiry=expiry_str,
            channel_id=creds_row.get("channel_id"),
            channel_name=creds_row.get("channel_name"),
        )
    return creds


def get_youtube_service(conn: sqlite3.Connection):
    """Return an authorized YouTube API service object."""
    _require_google_imports()
    creds_row = get_creds_from_db(conn)
    if creds_row is None:
        raise ValueError("YouTube not connected. Please connect first.")
    creds = _refresh_if_needed(conn, creds_row)
    return build(_API_SERVICE_NAME, _API_VERSION, credentials=creds)


def get_authorization_url(redirect_uri: str) -> str:
    """Generate the Google OAuth2 consent screen URL."""
    _require_google_imports()
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=_SCOPES,
    )
    flow.redirect_uri = redirect_uri
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )
    return url


def exchange_code(code: str, redirect_uri: str) -> dict:
    """Exchange authorization code for tokens. Returns channel info."""
    _require_google_imports()
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.youtube_client_id,
                "client_secret": settings.youtube_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=_SCOPES,
    )
    flow.redirect_uri = redirect_uri
    flow.fetch_token(code=code)
    creds = flow.credentials

    # Get channel info
    youtube = build(_API_SERVICE_NAME, _API_VERSION, credentials=creds)
    ch_resp = youtube.channels().list(part="snippet", mine=True).execute()
    channel_id = ""
    channel_name = ""
    if ch_resp.get("items"):
        ch = ch_resp["items"][0]
        channel_id = ch["id"]
        channel_name = ch["snippet"]["title"]

    expiry_str = creds.expiry.isoformat() if creds.expiry else ""
    return {
        "access_token": creds.token or "",
        "refresh_token": creds.refresh_token or "",
        "token_expiry": expiry_str,
        "channel_id": channel_id,
        "channel_name": channel_name,
    }


def upload_video(
    conn: sqlite3.Connection,
    video_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy_status: str = "private",
) -> dict:
    """Upload a video to YouTube. Returns {youtube_video_id, status}.

    Creates a youtube_uploads record and updates it as the upload progresses.
    """
    _require_google_imports()
    video_file = Path(video_path)
    if not video_file.exists():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    now = _now_iso()
    cursor = conn.execute(
        """INSERT INTO youtube_uploads
           (video_path, title, description, tags, privacy_status, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'uploading', ?)""",
        (str(video_file), title, description, json.dumps(tags or []), privacy_status, now),
    )
    upload_id = cursor.lastrowid
    conn.commit()

    try:
        youtube = get_youtube_service(conn)
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": (tags or [])[:30],
                "categoryId": "26",  # Howto & Style (common for audiobook/educational)
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }

        media = MediaFileUpload(
            str(video_file),
            mimetype="video/mp4",
            resumable=True,
            chunksize=_UPLOAD_CHUNK_SIZE,
        )

        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        while response is None:
            status, response = req.next_chunk()
            if status:
                pct = int(status.progress() * 100)
                logger.info("YouTube upload %s: %d%%", upload_id, pct)

        youtube_video_id = response.get("id", "")
        conn.execute(
            """UPDATE youtube_uploads
               SET youtube_video_id=?, status='done', uploaded_at=?, error_message=NULL
               WHERE id=?""",
            (youtube_video_id, _now_iso(), upload_id),
        )
        conn.commit()
        logger.info("YouTube upload %s done: %s", upload_id, youtube_video_id)
        return {"upload_id": upload_id, "youtube_video_id": youtube_video_id, "status": "done"}

    except Exception as exc:
        conn.execute(
            "UPDATE youtube_uploads SET status='failed', error_message=? WHERE id=?",
            (str(exc), upload_id),
        )
        conn.commit()
        logger.exception("YouTube upload %s failed", upload_id)
        return {"upload_id": upload_id, "status": "failed", "error": str(exc)}


def enqueue_upload(
    conn: sqlite3.Connection,
    video_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy_status: str | None = None,
) -> int:
    """Create a pending youtube_uploads record. Returns upload_id.

    The actual upload is done by the caller (worker or route).
    """
    if privacy_status is None:
        privacy_status = settings.youtube_default_privacy
    now = _now_iso()
    cursor = conn.execute(
        """INSERT INTO youtube_uploads
           (video_path, title, description, tags, privacy_status, status, created_at)
           VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
        (video_path, title, description, json.dumps(tags or []), privacy_status, now),
    )
    conn.commit()
    return cursor.lastrowid


def list_uploads(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM youtube_uploads ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_pending_uploads(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM youtube_uploads WHERE status='pending' ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_upload_done(conn: sqlite3.Connection, upload_id: int, youtube_video_id: str) -> None:
    conn.execute(
        "UPDATE youtube_uploads SET youtube_video_id=?, status='done', uploaded_at=? WHERE id=?",
        (youtube_video_id, _now_iso(), upload_id),
    )
    conn.commit()


def mark_upload_failed(conn: sqlite3.Connection, upload_id: int, error: str) -> None:
    conn.execute(
        "UPDATE youtube_uploads SET status='failed', error_message=? WHERE id=?",
        (error, upload_id),
    )
    conn.commit()
