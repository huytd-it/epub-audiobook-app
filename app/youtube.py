"""YouTube Data API v3 integration: OAuth2 flow, video upload, token management."""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path

# Reusing the same OAuth client for both YouTube and Google Drive (see .env.example) means
# Google's token response often includes every scope ever granted to that client for this
# account, not just the one this flow requested. oauthlib treats that as an error by
# default ("Scope has changed") unless this is set - this is the standard, documented way
# to allow it.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload, MediaIoBaseUpload
    _GOOGLE_IMPORTS_OK = True
except ModuleNotFoundError:
    _GOOGLE_IMPORTS_OK = False

try:
    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError
except ModuleNotFoundError:
    RefreshError = ()
    HttpError = ()

from app.config import settings
from app import db

logger = logging.getLogger(__name__)

PLAYLIST_LEASE_SECONDS = 300
PLAYLIST_HEARTBEAT_SECONDS = 30
_MEMORY_PLAYLIST_LOCKS = {}
_MEMORY_PLAYLIST_LOCKS_GUARD = threading.Lock()


class _PlaylistHeartbeat:
    def __init__(self, stop_event, thread, conn):
        self._stop_event, self._thread, self._conn = stop_event, thread, conn
        self.error = None
    def stop(self): self._stop_event.set()
    def join(self):
        if self._thread: self._thread.join()
    def close(self):
        if self._conn: self._conn.close()


def _start_playlist_heartbeat(conn, book_id, channel_id, owner):
    database = conn.execute("PRAGMA database_list").fetchone()[2]
    if not database or database == ":memory:":
        return _PlaylistHeartbeat(threading.Event(), None, None)
    heartbeat_conn = db.connect(database)
    stop_event = threading.Event()
    def heartbeat():
        try:
            while not stop_event.is_set():
                heartbeat_conn.execute("UPDATE youtube_playlist_map SET updated_at=? WHERE book_id=? AND channel_id=? AND playlist_id='__creating__' AND mode=?", (_now_iso(), book_id, channel_id, f"auto-create:{owner}")); heartbeat_conn.commit()
                if stop_event.wait(PLAYLIST_HEARTBEAT_SECONDS): break
        except Exception as exc:
            handle.error = exc
    handle = _PlaylistHeartbeat(stop_event, None, heartbeat_conn)
    thread = threading.Thread(target=heartbeat, daemon=True)
    handle._thread = thread
    thread.start()
    return handle

_SCOPES = [
    "https://www.googleapis.com/auth/youtube",
    "https://www.googleapis.com/auth/youtube.upload",
]
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


def _dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


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
        # PKCE needs the same code_verifier at both the auth-url step and the token-exchange
        # step, but those happen in two separate HTTP requests with no shared Flow instance
        # (no server-side session here) - so auto-generating one here would just get lost by
        # the time exchange_code() runs, causing "invalid_grant: Missing code verifier". Not
        # needed anyway since this is a confidential client (has a client_secret).
        autogenerate_code_verifier=False,
    )
    flow.redirect_uri = redirect_uri
    url, _ = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        # select_account forces Google to always show the account chooser, even if the
        # browser already has an active session for a single Google account (otherwise it
        # silently reuses that account without letting the user pick a different one).
        prompt="select_account consent",
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
        autogenerate_code_verifier=False,
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


def process_upload(conn: sqlite3.Connection, upload_id: int) -> dict:
    """Upload a video for an existing youtube_uploads row.

    Updates the existing row from pending → uploading → done.
    Never creates a second row.
    Returns {youtube_video_id, status}.
    """
    _require_google_imports()
    row = dict(conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone() or {})
    if not row:
        raise ValueError(f"upload {upload_id} not found")
    if row["status"] == "done":
        return {"youtube_video_id": row["youtube_video_id"], "status": "done"}

    video_file = Path(row["video_path"])
    if not video_file.exists():
        raise FileNotFoundError(f"Video file not found: {row['video_path']}")

    conn.execute(
        "UPDATE youtube_uploads SET status='uploading', upload_progress=0 WHERE id=?",
        (upload_id,),
    )
    conn.commit()

    try:
        youtube = get_youtube_service(conn)
        tags = json.loads(row["tags"]) if row["tags"] else []
        title = (row["title"] or "")[:100]
        description = (row["description"] or "")[:5000]
        privacy_status = row.get("privacy_status", "private")

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": (tags or [])[:30],
                "categoryId": "26",
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
                # Persisted every chunk so the /youtube page can poll it. This runs on the
                # worker's own connection (see UploadWorker._execution_connection), never the
                # shared one, so a multi-minute upload does not hold up any request.
                pct = status.progress() * 100
                conn.execute(
                    "UPDATE youtube_uploads SET upload_progress=? WHERE id=?",
                    (pct, upload_id),
                )
                conn.commit()
                logger.info("YouTube upload %s: %d%%", upload_id, int(pct))

        youtube_video_id = response.get("id", "")
        conn.execute(
            "UPDATE youtube_uploads SET youtube_video_id=?, status='done', upload_progress=100, uploaded_at=?, error_message=NULL WHERE id=?",
            (youtube_video_id, _now_iso(), upload_id),
        )
        conn.commit()
        logger.info("YouTube upload %s done: %s", upload_id, youtube_video_id)
        return {"youtube_video_id": youtube_video_id, "status": "done"}

    except Exception as exc:
        conn.execute(
            "UPDATE youtube_uploads SET status='failed', error_message=? WHERE id=?",
            (str(exc), upload_id),
        )
        conn.commit()
        logger.exception("YouTube upload %s failed", upload_id)
        return {"youtube_video_id": None, "status": "failed", "error": str(exc)}


def upload_video(
    conn: sqlite3.Connection,
    video_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy_status: str = "private",
) -> dict:
    """Upload a video to YouTube. Compatibility wrapper: enqueues then processes.

    Keeps the same signature and return shape as before.
    Returns {upload_id, youtube_video_id, status}.
    """
    upload_id = enqueue_upload(conn, video_path, title, description, tags, privacy_status)
    validation = validate_upload_file(conn, upload_id)
    if not validation.valid:
        return {"upload_id": upload_id, "youtube_video_id": None, "status": "failed",
                "error": f"{validation.error_code}: {validation.message}"}
    result = process_upload(conn, upload_id)
    result["upload_id"] = upload_id
    return result


def validate_upload_file(conn: sqlite3.Connection, upload_id: int):
    from app.video_integrity import validate_video, validation_report_json

    row = conn.execute("SELECT video_path FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    if row is None:
        raise ValueError(f"upload {upload_id} not found")
    mark_validation_started(conn, upload_id)
    result = validate_video(row["video_path"])
    report = validation_report_json(result)
    if result.valid:
        mark_validation_valid(conn, upload_id, report_json=report)
    else:
        mark_validation_failed(conn, upload_id, result.error_code or "validation_failed",
                               result.message, report_json=report)
        mark_upload_failed(conn, upload_id, f"{result.error_code}: {result.message}")
    return result


def enqueue_upload(
    conn: sqlite3.Connection,
    video_path: str,
    title: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy_status: str | None = None,
    video_id: int | None = None,
    playlist_id: str = "",
    *,
    render_source_type: str = "external",
    render_source_id: int | None = None,
) -> int:
    """Create a pending youtube_uploads record. Returns upload_id.

    The actual upload is done by the caller (worker or route).
    """
    if render_source_type not in {"book", "patch", "standalone", "external"}:
        raise ValueError("invalid render_source_type")
    if privacy_status is None:
        privacy_status = settings.youtube_default_privacy
    metadata_snapshot = json.dumps({"automation": {"youtube": {
        "playlist_mode": "existing", "playlist_id": playlist_id,
    }}}) if playlist_id else None
    now = _now_iso()
    cursor = conn.execute(
        """INSERT INTO youtube_uploads
           (video_id, video_path, title, description, tags, privacy_status, status,
            metadata_snapshot, render_source_type, render_source_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)""",
        (video_id, video_path, title, description, json.dumps(tags or []), privacy_status,
         metadata_snapshot, render_source_type, render_source_id, now),
    )
    conn.commit()
    return cursor.lastrowid


def mark_validation_started(conn: sqlite3.Connection, upload_id: int) -> None:
    conn.execute(
        "UPDATE youtube_uploads SET validation_status='validating', validation_error_code=NULL, validation_error_message=NULL, validation_report_json=NULL WHERE id=?",
        (upload_id,),
    )
    conn.commit()


def mark_validation_valid(conn: sqlite3.Connection, upload_id: int,
                          *, report_json: str | None = None) -> None:
    conn.execute(
        "UPDATE youtube_uploads SET validation_status='valid', validation_error_code=NULL, validation_error_message=NULL, validation_report_json=?, validated_at=? WHERE id=?",
        (report_json, _now_iso(), upload_id),
    )
    conn.commit()


def mark_validation_failed(conn: sqlite3.Connection, upload_id: int, code: str, message: str,
                           *, report_json: str | None = None) -> None:
    conn.execute(
        "UPDATE youtube_uploads SET validation_status='failed', validation_error_code=?, validation_error_message=?, validation_report_json=?, validated_at=? WHERE id=?",
        (code, message[-2000:], report_json, _now_iso(), upload_id),
    )
    conn.commit()


def list_uploads(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM youtube_uploads ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def delete_upload(conn: sqlite3.Connection, upload_id: int) -> bool:
    """Delete a youtube upload record. Returns True if deleted."""
    cur = conn.execute("DELETE FROM youtube_uploads WHERE id=?", (upload_id,))
    conn.commit()
    return cur.rowcount > 0


def delete_uploads(conn: sqlite3.Connection, upload_ids: list[int]) -> int:
    """Delete multiple youtube upload records. Returns count deleted."""
    if not upload_ids:
        return 0
    placeholders = ",".join("?" * len(upload_ids))
    cur = conn.execute(f"DELETE FROM youtube_uploads WHERE id IN ({placeholders})", upload_ids)
    conn.commit()
    return cur.rowcount


def reset_upload_status(conn: sqlite3.Connection, upload_ids: list[int]) -> int:
    """Reset failed uploads to pending status for retry. Returns count reset."""
    if not upload_ids:
        return 0
    placeholders = ",".join("?" * len(upload_ids))
    cur = conn.execute(
        f"UPDATE youtube_uploads SET status='pending', error_message=NULL WHERE id IN ({placeholders}) AND status='failed'",
        upload_ids,
    )
    conn.commit()
    return cur.rowcount


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


# ---------------------------------------------------------------------------
# Thumbnail and playlist post-processing
# ---------------------------------------------------------------------------


def set_thumbnail(conn: sqlite3.Connection, youtube_video_id: str, thumbnail_path: str) -> None:
    """Set the custom thumbnail for a published video."""
    _require_google_imports()
    service = get_youtube_service(conn)
    upload_path = Path(thumbnail_path)
    temporary_path = None
    try:
        if upload_path.stat().st_size > 2 * 1024 * 1024:
            import tempfile
            from PIL import Image

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as output:
                temporary_path = Path(output.name)
            with Image.open(upload_path) as image:
                image = image.convert("RGB")
                image.thumbnail((1280, 720))
                quality = 85
                image.save(temporary_path, "JPEG", quality=quality, optimize=True)
                while temporary_path.stat().st_size > 2 * 1024 * 1024 and quality > 10:
                    quality -= 10
                    image.save(temporary_path, "JPEG", quality=quality, optimize=True)
            upload_path = temporary_path
        mimetype = "image/jpeg" if upload_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
        if temporary_path:
            # MediaFileUpload keeps its file handle open for the lifetime of the
            # object, which on Windows makes the unlink below fail with WinError 32.
            # Thumbnails are capped at 2MB, so upload the temp file from memory.
            import io

            media = MediaIoBaseUpload(io.BytesIO(temporary_path.read_bytes()), mimetype=mimetype)
        else:
            media = MediaFileUpload(str(upload_path), mimetype=mimetype)
        service.thumbnails().set(videoId=youtube_video_id, media_body=media).execute()
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


def list_playlists(conn: sqlite3.Connection, max_results: int = 50) -> list[dict]:
    """List the authenticated user's playlists."""
    _require_google_imports()
    service = get_youtube_service(conn)
    items = []
    page_token = None
    while True:
        params = {"part": "snippet", "mine": True, "maxResults": max_results}
        if page_token:
            params["pageToken"] = page_token
        resp = service.playlists().list(**params).execute()
        for item in resp.get("items", []):
            snippet = item.get("snippet") or {}
            item["title"] = snippet.get("title", "")
            item["description"] = snippet.get("description", "")
            items.append(item)
        page_token = resp.get("nextPageToken")
        if not page_token:
            return items


def create_playlist(
    conn: sqlite3.Connection,
    title: str,
    description: str = "",
    privacy: str = "private",
) -> dict:
    """Create a new playlist. Returns the API response dict."""
    _require_google_imports()
    service = get_youtube_service(conn)
    body = {
        "snippet": {"title": title, "description": description},
        "status": {"privacyStatus": privacy},
    }
    return service.playlists().insert(part="snippet,status", body=body).execute()


def playlist_contains_video(conn: sqlite3.Connection, playlist_id: str, youtube_video_id: str) -> bool:
    """Check if a video is already in a playlist."""
    _require_google_imports()
    service = get_youtube_service(conn)
    page_token = None
    while True:
        params = {"part": "snippet", "playlistId": playlist_id, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = service.playlistItems().list(**params).execute()
        for item in resp.get("items", []):
            if item.get("snippet", {}).get("resourceId", {}).get("videoId") == youtube_video_id:
                return True
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return False


def add_video_to_playlist(
    conn: sqlite3.Connection,
    playlist_id: str,
    youtube_video_id: str,
    position: int | None = None,
) -> dict:
    """Add a video to a playlist. Returns the API response dict.

    position is an optional zero-based insert position; when omitted the video is
    appended at the end of the playlist.
    """
    _require_google_imports()
    service = get_youtube_service(conn)
    return _add_playlist_item(service, playlist_id, youtube_video_id, position=position)


# ---------------------------------------------------------------------------
# Playlist management
#
# All batch operations return a standardized result:
#   {requested, succeeded, skipped, failed, items}
# where `items` is a list of {key, status, message} with status in
# succeeded/skipped/failed. Every call that builds a service object once and
# reuses it across the batch to avoid re-authorizing per item.
# ---------------------------------------------------------------------------


def _new_batch_result() -> dict:
    """Create an empty standardized batch-operation result."""
    return {"requested": 0, "succeeded": 0, "skipped": 0, "failed": 0, "items": []}


def _batch_add(result: dict, key: str, status: str, message: str = "") -> dict:
    """Record one item in a batch result and bump the matching counter."""
    if status not in ("succeeded", "skipped", "failed"):
        raise ValueError(f"invalid batch status: {status}")
    result["requested"] += 1
    result[status] += 1
    result["items"].append({"key": key, "status": status, "message": message})
    return result


def _normalize_playlist_item(item: dict, playlist_id: str | None = None) -> dict:
    """Flatten a raw playlistItems.list resource into the normalized shape."""
    snippet = item.get("snippet") or {}
    resource = snippet.get("resourceId") or {}
    content = item.get("contentDetails") or {}
    thumbnails = snippet.get("thumbnails") or {}
    thumbnail = next(
        (thumbnails[size].get("url") for size in ("medium", "high", "default") if size in thumbnails),
        None,
    )
    return {
        "playlist_item_id": item.get("id", ""),
        "playlist_id": snippet.get("playlistId") or playlist_id or "",
        "video_id": resource.get("videoId") or content.get("videoId") or "",
        "title": snippet.get("title", "") or "",
        "thumbnail": thumbnail,
        "position": snippet.get("position", 0),
        "published_at": content.get("videoPublishedAt"),
    }


def _list_playlist_items_page(
    service,
    playlist_id: str,
    max_results: int = 50,
    page_token: str | None = None,
    part: str = "snippet,contentDetails",
) -> dict:
    """One playlistItems.list call, normalized.

    Returns {items, next_page_token, prev_page_token, total} with `total` taken from
    the API's pageInfo.totalResults when available.
    """
    params = {"part": part, "playlistId": playlist_id, "maxResults": max_results}
    if page_token:
        params["pageToken"] = page_token
    resp = service.playlistItems().list(**params).execute()
    items = [_normalize_playlist_item(item, playlist_id) for item in resp.get("items", [])]
    return {
        "items": items,
        "next_page_token": resp.get("nextPageToken"),
        "prev_page_token": resp.get("prevPageToken"),
        "total": (resp.get("pageInfo") or {}).get("totalResults", len(items)),
    }


def list_playlist_items(
    conn: sqlite3.Connection,
    playlist_id: str,
    max_results: int = 50,
    page_token: str | None = None,
) -> dict:
    """List the videos in a playlist.

    Returns {items, next_page_token, prev_page_token, total} where each item is
    normalized to {playlist_item_id, playlist_id, video_id, title, thumbnail,
    position, published_at}.
    """
    _require_google_imports()
    service = get_youtube_service(conn)
    return _list_playlist_items_page(service, playlist_id, max_results=max_results, page_token=page_token)


def _get_all_playlist_items(service, playlist_id: str, max_results: int = 50) -> list[dict]:
    """Page through an entire playlist, returning a flat normalized item list."""
    items = []
    page_token = None
    while True:
        page = _list_playlist_items_page(service, playlist_id, max_results=max_results, page_token=page_token)
        items.extend(page["items"])
        page_token = page["next_page_token"]
        if not page_token:
            return items


def get_all_playlist_items(conn: sqlite3.Connection, playlist_id: str) -> list[dict]:
    """Return every item in a playlist as a flat normalized list (paginates)."""
    _require_google_imports()
    service = get_youtube_service(conn)
    return _get_all_playlist_items(service, playlist_id)


def _find_playlist_item(service, playlist_id: str, video_id: str) -> dict | None:
    """Locate the normalized item for a video, paging until found (or exhausted)."""
    page_token = None
    while True:
        page = _list_playlist_items_page(service, playlist_id, page_token=page_token)
        for item in page["items"]:
            if item["video_id"] == video_id:
                return item
        page_token = page["next_page_token"]
        if not page_token:
            return None


def find_playlist_item(conn: sqlite3.Connection, playlist_id: str, video_id: str) -> dict | None:
    """Return the normalized item for a video in a playlist, or None."""
    _require_google_imports()
    service = get_youtube_service(conn)
    return _find_playlist_item(service, playlist_id, video_id)


def _find_playlist_item_by_id(service, playlist_id: str, playlist_item_id: str) -> dict | None:
    """Locate an item by its playlistItem.id, paging until found (or exhausted)."""
    page_token = None
    while True:
        page = _list_playlist_items_page(service, playlist_id, page_token=page_token)
        for item in page["items"]:
            if item["playlist_item_id"] == playlist_item_id:
                return item
        page_token = page["next_page_token"]
        if not page_token:
            return None


def _add_playlist_item(service, playlist_id: str, video_id: str, position: int | None = None) -> dict:
    body = {
        "snippet": {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        },
    }
    if position is not None:
        body["snippet"]["position"] = int(position)
    return service.playlistItems().insert(part="snippet", body=body).execute()


def _delete_playlist_item(service, playlist_item_id: str) -> dict:
    return service.playlistItems().delete(id=playlist_item_id).execute()


def _update_playlist_item(
    service,
    playlist_item_id: str,
    playlist_id: str,
    position: int,
    video_id: str,
) -> dict:
    body = {
        "id": playlist_item_id,
        "snippet": {
            "playlistId": playlist_id,
            "position": int(position),
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        },
    }
    return service.playlistItems().update(part="snippet", body=body).execute()


def remove_playlist_item(conn: sqlite3.Connection, playlist_id: str, playlist_item_id: str) -> dict:
    """Remove an item from a playlist by its playlistItem.id. Returns the API response."""
    _require_google_imports()
    service = get_youtube_service(conn)
    return _delete_playlist_item(service, playlist_item_id)


def remove_video_from_playlist(conn: sqlite3.Connection, playlist_id: str, youtube_video_id: str) -> bool:
    """Remove a video from a playlist (resolving its playlist item id first).

    Returns True when an item was removed, False when the video was not present.
    """
    _require_google_imports()
    service = get_youtube_service(conn)
    item = _find_playlist_item(service, playlist_id, youtube_video_id)
    if item is None:
        return False
    _delete_playlist_item(service, item["playlist_item_id"])
    return True


def update_playlist_item_position(
    conn: sqlite3.Connection,
    playlist_id: str,
    playlist_item_id: str,
    position: int,
    video_id: str | None = None,
) -> dict:
    """Move a playlist item to a new zero-based position. Returns the API response.

    The update needs the item's video id; when omitted it is resolved with one extra
    playlistItems.list call.
    """
    _require_google_imports()
    service = get_youtube_service(conn)
    if video_id is None:
        item = _find_playlist_item_by_id(service, playlist_id, playlist_item_id)
        if item is None:
            raise ValueError(f"playlist item {playlist_item_id} not found in playlist {playlist_id}")
        video_id = item["video_id"]
    return _update_playlist_item(service, playlist_item_id, playlist_id, position, video_id)


def move_playlist_item(
    conn: sqlite3.Connection,
    playlist_id: str,
    playlist_item_id: str,
    new_position: int,
    video_id: str | None = None,
) -> dict:
    """Move a playlist item to an arbitrary position (alias of update_playlist_item_position)."""
    return update_playlist_item_position(conn, playlist_id, playlist_item_id, new_position, video_id=video_id)


def bulk_add_to_playlist(
    conn: sqlite3.Connection,
    playlist_id: str,
    video_ids: list[str],
    position: int | None = None,
    skip_duplicates: bool = True,
) -> dict:
    """Add many videos to a playlist in one batch.

    Videos already in the playlist are skipped as duplicates ('skipped', not
    'failed'). Returns a standardized batch result.
    """
    result = _new_batch_result()
    if not video_ids:
        return result
    _require_google_imports()
    service = get_youtube_service(conn)
    existing: set[str] | None = None
    if skip_duplicates:
        existing = {i["video_id"] for i in _get_all_playlist_items(service, playlist_id)}
    for video_id in video_ids:
        if existing is not None and video_id in existing:
            _batch_add(result, video_id, "skipped", "duplicate: already in playlist")
            continue
        try:
            _add_playlist_item(service, playlist_id, video_id, position=position)
            if existing is not None:
                existing.add(video_id)
            _batch_add(result, video_id, "succeeded")
        except Exception as exc:
            _batch_add(result, video_id, "failed", str(exc)[:500])
    return result


def bulk_remove_from_playlist(
    conn: sqlite3.Connection,
    playlist_id: str,
    video_ids: list[str] | None = None,
    playlist_item_ids: list[str] | None = None,
) -> dict:
    """Remove many items from a playlist in one batch.

    Pass video_ids to remove by video (resolved to their playlist item ids) or
    playlist_item_ids to remove directly by playlistItem.id. Videos that are not in
    the playlist are skipped. Returns a standardized batch result.
    """
    result = _new_batch_result()
    if not video_ids and not playlist_item_ids:
        return result
    _require_google_imports()
    service = get_youtube_service(conn)
    targets: list[tuple[str, str]] = []
    if playlist_item_ids:
        targets = [(item_id, item_id) for item_id in playlist_item_ids]
    else:
        for video_id in video_ids or []:
            item = _find_playlist_item(service, playlist_id, video_id)
            if item is None:
                _batch_add(result, video_id, "skipped", "not in playlist")
                continue
            targets.append((video_id, item["playlist_item_id"]))
    for key, item_id in targets:
        try:
            _delete_playlist_item(service, item_id)
            _batch_add(result, key, "succeeded")
        except Exception as exc:
            _batch_add(result, key, "failed", str(exc)[:500])
    return result


def _select_source_items(service, source_playlist_id: str, video_ids: list[str] | None) -> list[dict]:
    items = _get_all_playlist_items(service, source_playlist_id)
    if video_ids is not None:
        wanted = set(video_ids)
        items = [i for i in items if i["video_id"] in wanted]
    return items


def copy_playlist_items(
    conn: sqlite3.Connection,
    source_playlist_id: str,
    target_playlist_id: str,
    video_ids: list[str] | None = None,
    skip_duplicates: bool = True,
) -> dict:
    """Copy items from one playlist into another. Returns a standardized batch result.

    Videos already present in the target playlist are skipped as duplicates. When
    video_ids is given only those videos are copied.
    """
    result = _new_batch_result()
    _require_google_imports()
    service = get_youtube_service(conn)
    sources = _select_source_items(service, source_playlist_id, video_ids)
    existing = {i["video_id"] for i in _get_all_playlist_items(service, target_playlist_id)} if skip_duplicates else set()
    for item in sources:
        video_id = item["video_id"]
        if video_id in existing:
            _batch_add(result, video_id, "skipped", "duplicate: already in target playlist")
            continue
        try:
            _add_playlist_item(service, target_playlist_id, video_id)
            _batch_add(result, video_id, "succeeded")
        except Exception as exc:
            _batch_add(result, video_id, "failed", str(exc)[:500])
    return result


def move_playlist_items(
    conn: sqlite3.Connection,
    source_playlist_id: str,
    target_playlist_id: str,
    video_ids: list[str] | None = None,
    skip_duplicates: bool = True,
) -> dict:
    """Move items from one playlist to another (add-before-remove).

    Each video is added to the target first and only removed from the source after a
    successful add, so an add failure or a duplicate skip leaves the source item
    retained. Returns a standardized batch result.
    """
    result = _new_batch_result()
    _require_google_imports()
    service = get_youtube_service(conn)
    sources = _select_source_items(service, source_playlist_id, video_ids)
    existing = {i["video_id"] for i in _get_all_playlist_items(service, target_playlist_id)} if skip_duplicates else set()
    for item in sources:
        video_id = item["video_id"]
        if video_id in existing:
            _batch_add(result, video_id, "skipped", "duplicate: already in target playlist; source retained")
            continue
        try:
            _add_playlist_item(service, target_playlist_id, video_id)
        except Exception as exc:
            _batch_add(result, video_id, "failed", f"add failed; source retained: {str(exc)[:400]}")
            continue
        try:
            _delete_playlist_item(service, item["playlist_item_id"])
            _batch_add(result, video_id, "succeeded")
        except Exception as exc:
            _batch_add(result, video_id, "succeeded", f"added to target; source removal failed: {str(exc)[:300]}")
    return result


def _natural_sort_key(title: str):
    """Case-insensitive, numeric-chunk sort key (e.g. 'Part 2' sorts before 'Part 10')."""
    chunks = re.split(r"(\d+)", (title or "").lower())
    key = []
    for chunk in chunks:
        if chunk.isdigit():
            key.append((1, int(chunk)))
        else:
            key.append((0, chunk))
    return tuple(key)


# Episode markers used by the upload title template and by hand-named videos:
# "<book> - Tập 3 - Chương 11-15", "Tập 3 - <book>", "Part 3", "EP.3", "#3".
# The separator in front of the marker keeps it from matching inside a word.
_EPISODE_RE = re.compile(
    r"(?:^|[\s\-–—:|(\[.])(?:episode|tập|tap|phần|phan|part|quyển|quyen|vol|ep|#)"
    r"\s*[.:#]?\s*(\d+)",
    re.IGNORECASE,
)

_SERIES_STRIP = " \t-–—:|·.,()[]#"

_SEGMENT_SPLIT_RE = re.compile(r"[-–—:|]")


def _episode_series(text: str, match: "re.Match") -> str:
    """The series name around an episode marker, whichever layout the title uses.

    "Dị Độ Lữ Xá - Tập 3 - Chương ..." puts it in front of the marker; the upload
    template's "Tập 3 - Dị Độ Lữ Xá" puts it right after. Both must yield the same
    series so one playlist holding both layouts still interleaves by episode.
    """
    before = text[:match.start()].strip(_SERIES_STRIP)
    if before:
        return before
    return _SEGMENT_SPLIT_RE.split(text[match.end():], 1)[0].strip(_SERIES_STRIP)


def _episode_sort_key(title: str):
    """Group by series name, then order by episode number.

    Only the series and the episode number decide the order - the chapter range,
    patch name and tags that trail the number vary per episode and would otherwise
    drive the comparison whenever the titles are not perfectly uniform. Titles with
    no episode marker fall back to the plain natural key and sort among themselves.
    """
    text = title or ""
    match = _EPISODE_RE.search(text)
    if not match:
        return (_natural_sort_key(text), (0, 0), _natural_sort_key(text))
    return (
        _natural_sort_key(_episode_series(text, match)),
        (1, int(match.group(1))),
        _natural_sort_key(text[match.end():]),
    )


SORT_MODES = ("natural", "episode")

_SORT_KEYS = {"natural": _natural_sort_key, "episode": _episode_sort_key}


def _sort_key_for(mode: str):
    """Return the title sort key for a sort mode (unknown modes fall back to natural)."""
    return _SORT_KEYS.get(str(mode or "natural").lower(), _natural_sort_key)


def _new_order(items: list[dict], order: list[str] | None, direction: str,
               mode: str = "natural") -> list[dict]:
    """Resolve the target item order.

    order is an explicit list of video ids in the desired order; items not listed
    keep their relative place at the end. When order is None the items are
    title-sorted in the given direction ('asc' or 'desc') using the sort mode
    ('natural' or 'episode'). The result is a permutation of `items` (stable for ties).
    """
    if order is not None:
        seen = set()
        deduped = []
        for video_id in order:
            if video_id not in seen:
                seen.add(video_id)
                deduped.append(video_id)
        order = deduped
        by_id = {i["video_id"]: i for i in items}
        ordered = [by_id[video_id] for video_id in order if video_id in by_id]
        placed = {i["video_id"] for i in ordered}
        ordered.extend(i for i in items if i["video_id"] not in placed)
        return ordered
    key = _sort_key_for(mode)
    return sorted(
        items,
        key=lambda i: key(i.get("title", "")),
        reverse=str(direction).lower() == "desc",
    )


def _compute_positions(
    items: list[dict],
    order: list[str] | None = None,
    direction: str = "asc",
    mode: str = "natural",
) -> list[tuple[dict, int]]:
    """Pair each item with its new zero-based position without calling the API."""
    return list(zip(_new_order(items, order, direction, mode), range(len(items))))


def reorder_playlist_preview(
    conn: sqlite3.Connection,
    playlist_id: str,
    order: list[str] | None = None,
    direction: str = "asc",
    mode: str = "natural",
) -> dict:
    """Compute a reorder without mutating the playlist.

    order is an explicit list of video ids for arbitrary-position reordering; when
    None the items are title sorted (asc/desc) using the given sort mode. Returns
    {items, ordered} where every item carries both current_position and new_position.
    Non-mutating: no playlistItems write is made.
    """
    _require_google_imports()
    service = get_youtube_service(conn)
    items = _get_all_playlist_items(service, playlist_id)
    current = {i["video_id"]: i["position"] for i in items}
    preview = []
    for item, new_position in _compute_positions(items, order, direction, mode):
        preview.append({
            **item,
            "current_position": current.get(item["video_id"], item["position"]),
            "new_position": new_position,
        })
    return {
        "items": preview,
        "ordered": [i["video_id"] for i in _new_order(items, order, direction, mode)],
    }


def reorder_playlist(
    conn: sqlite3.Connection,
    playlist_id: str,
    order: list[str] | None = None,
    direction: str = "asc",
    mode: str = "natural",
) -> dict:
    """Apply a reorder to a playlist.

    order is an explicit list of video ids for arbitrary-position reordering; when
    None the playlist is title sorted (asc by default) using the given sort mode.
    Only items whose position actually changes are updated and failures do not stop
    the rest of the batch. Returns a standardized batch result.
    """
    result = _new_batch_result()
    _require_google_imports()
    service = get_youtube_service(conn)
    items = _get_all_playlist_items(service, playlist_id)
    current = {i["video_id"]: i["position"] for i in items}
    for item, new_position in _compute_positions(items, order, direction, mode):
        if current.get(item["video_id"], item["position"]) == new_position:
            _batch_add(result, item["video_id"], "skipped", f"already at position {new_position}")
            continue
        try:
            _update_playlist_item(service, item["playlist_item_id"], playlist_id, new_position, item["video_id"])
            _batch_add(result, item["video_id"], "succeeded", f"moved to position {new_position}")
        except Exception as exc:
            _batch_add(result, item["video_id"], "failed", str(exc)[:500])
    return result


def sort_playlist_preview(conn: sqlite3.Connection, playlist_id: str, direction: str = "asc",
                          mode: str = "natural") -> dict:
    """Non-mutating preview of a title sort of a playlist ('natural' or 'episode')."""
    return reorder_playlist_preview(conn, playlist_id, order=None, direction=direction, mode=mode)


def sort_playlist(conn: sqlite3.Connection, playlist_id: str, direction: str = "asc",
                  mode: str = "natural") -> dict:
    """Sort a playlist by title order (asc/desc, 'natural' or 'episode'). Applies the
    new positions."""
    return reorder_playlist(conn, playlist_id, order=None, direction=direction, mode=mode)


def playlist_page_range(position: int, page_size: int = 50) -> tuple[int, int]:
    """Return the half-open position span [start, end) of the page holding a position.

    YouTube caps playlistItems.list at 50 per page; page N covers positions
    [N*page_size, (N+1)*page_size).
    """
    start = (int(position) // page_size) * page_size
    return start, start + page_size


def playlist_page_for(position: int, page_size: int = 50) -> int:
    """Return the zero-based page index a position belongs to."""
    return int(position) // page_size


def reorder_playlist_page(
    conn: sqlite3.Connection,
    playlist_id: str,
    page_index: int,
    order: list[str],
    page_size: int = 50,
) -> dict:
    """Reorder the items that live on one page of a playlist.

    All target positions stay within the page's span, so no update ever has to move
    an item across a page boundary. Returns a standardized batch result.
    """
    result = _new_batch_result()
    _require_google_imports()
    service = get_youtube_service(conn)
    start = page_index * page_size
    span = set(range(start, start + len(order)))
    items = [i for i in _get_all_playlist_items(service, playlist_id) if i["position"] in span]
    if len(items) != len(order):
        for video_id in order:
            _batch_add(result, video_id, "failed", f"video not found on page {page_index}")
        return result
    current = {i["video_id"]: i["position"] for i in items}
    for item, new_position in zip(_new_order(items, order, "asc"), range(start, start + len(order))):
        if current.get(item["video_id"]) == new_position:
            _batch_add(result, item["video_id"], "skipped", f"already at position {new_position}")
            continue
        try:
            _update_playlist_item(service, item["playlist_item_id"], playlist_id, new_position, item["video_id"])
            _batch_add(result, item["video_id"], "succeeded", f"moved to position {new_position}")
        except Exception as exc:
            _batch_add(result, item["video_id"], "failed", str(exc)[:500])
    return result


def _channel_uploads_playlist_id(channel_id: str) -> str | None:
    """Derive a channel's uploads playlist id: 'UC...' -> 'UU...'.

    Returns None when the channel id does not follow the 'UC' prefix convention, in
    which case callers fall back to search.
    """
    if isinstance(channel_id, str) and channel_id.startswith("UC"):
        return "UU" + channel_id[2:]
    return None


def _search_videos_page(
    service,
    channel_id: str | None,
    query: str,
    max_results: int,
    page_token: str | None,
) -> dict:
    """One search.list call, normalized (100 quota units per call)."""
    params = {"part": "snippet", "type": "video", "maxResults": max_results}
    if channel_id:
        params["channelId"] = channel_id
    else:
        params["forMine"] = True
    if query:
        params["q"] = query
    if page_token:
        params["pageToken"] = page_token
    resp = service.search().list(**params).execute()
    items = []
    for item in resp.get("items", []):
        video_id = (item.get("id") or {}).get("videoId", "")
        snippet = item.get("snippet") or {}
        thumbnails = snippet.get("thumbnails") or {}
        thumbnail = next(
            (thumbnails[size].get("url") for size in ("medium", "high", "default") if size in thumbnails),
            None,
        )
        items.append({
            "video_id": video_id,
            "title": snippet.get("title", ""),
            "channel_id": snippet.get("channelId", ""),
            "thumbnail": thumbnail,
            "published_at": snippet.get("publishedAt"),
        })
    return {
        "items": items,
        "next_page_token": resp.get("nextPageToken"),
        "prev_page_token": resp.get("prevPageToken"),
        "total": (resp.get("pageInfo") or {}).get("totalResults", len(items)),
    }


def search_channel_videos(
    conn: sqlite3.Connection,
    channel_id: str | None = None,
    query: str = "",
    max_results: int = 25,
    page_token: str | None = None,
) -> dict:
    """Search videos on the authenticated channel (or a given channel) by title.

    Backed by search.list, which costs 100 quota units per request - prefer
    list_channel_videos / find_channel_videos_by_title when paging the whole uploads
    playlist is acceptable. Returns {items, next_page_token, prev_page_token, total}
    with items normalized to {video_id, title, thumbnail, channel_id, published_at}.
    """
    _require_google_imports()
    service = get_youtube_service(conn)
    return _search_videos_page(service, channel_id, query, max_results, page_token)


def list_channel_videos(
    conn: sqlite3.Connection,
    channel_id: str,
    max_results: int = 50,
    page_token: str | None = None,
    title_query: str | None = None,
) -> dict:
    """List a channel's videos by title.

    Prefers paging through the channel's uploads playlist (1 quota unit per page)
    over search.list (100 units); falls back to search when the uploads playlist id
    cannot be derived. When title_query is given, only titles containing it are
    returned (client-side filter on the current page).
    """
    _require_google_imports()
    service = get_youtube_service(conn)
    uploads_id = _channel_uploads_playlist_id(channel_id)
    if uploads_id:
        page = _list_playlist_items_page(service, uploads_id, max_results=max_results, page_token=page_token)
    else:
        page = _search_videos_page(service, channel_id, "", max_results, page_token)
    if title_query:
        query = title_query.strip().lower()
        page["items"] = [i for i in page["items"] if query in (i.get("title") or "").lower()]
    return page


def find_channel_videos_by_title(
    conn: sqlite3.Connection,
    channel_id: str,
    query: str,
    limit: int = 50,
    max_pages: int = 10,
) -> dict:
    """Search a channel's videos by title with remote pagination.

    Uses the cheap uploads-playlist listing and keeps paging until `limit` matches
    are found or the playlist is exhausted. Falls back to search.list when the
    uploads playlist id cannot be derived. Returns {items, next_page_token, total}.
    """
    _require_google_imports()
    query = (query or "").strip().lower()
    uploads_id = _channel_uploads_playlist_id(channel_id)
    if not uploads_id or not query:
        return search_channel_videos(conn, channel_id=channel_id, query=query, max_results=min(limit, 50))
    service = get_youtube_service(conn)
    matches = []
    page_token = None
    pages = 0
    while pages < max_pages:
        page = _list_playlist_items_page(service, uploads_id, max_results=50, page_token=page_token)
        pages += 1
        for item in page["items"]:
            if query in (item.get("title") or "").lower():
                matches.append(item)
        page_token = page["next_page_token"]
        if not page_token or len(matches) >= limit:
            break
    return {
        "items": matches[:limit],
        "next_page_token": page_token,
        "prev_page_token": None,
        "total": len(matches),
    }


def _playlist_marker(book_id: int) -> str:
    return f"[epub-audiobook-app book:{book_id}]"


def resolve_book_playlist(
    conn: sqlite3.Connection,
    book_id: int,
    channel_id: str,
    template_values: dict[str, object],
    _depth: int = 0,
) -> str:
    """Find or create a playlist for a book. Returns playlist_id."""
    memory_lock = None
    if conn.execute("PRAGMA database_list").fetchone()[2] == ":memory:":
        with _MEMORY_PLAYLIST_LOCKS_GUARD:
            memory_lock = _MEMORY_PLAYLIST_LOCKS.setdefault(id(conn), threading.Lock())
        memory_lock.acquire()
    reclaimed = False
    for _ in range(20):
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute("SELECT playlist_id, updated_at FROM youtube_playlist_map WHERE book_id=? AND channel_id=?", (book_id, channel_id)).fetchone()
        if existing and existing["playlist_id"] != "__creating__":
            conn.commit(); return existing["playlist_id"]
        try:
            lease_time = datetime.fromisoformat(existing["updated_at"]) if existing and existing["updated_at"] else None
            if lease_time and lease_time.tzinfo is None:
                lease_time = lease_time.replace(tzinfo=timezone.utc)
            stale = existing and (not lease_time or (datetime.now(timezone.utc) - lease_time).total_seconds() > PLAYLIST_LEASE_SECONDS)
        except (ValueError, TypeError):
            stale = True
        if existing and not stale:
            conn.rollback(); time.sleep(0.05); continue
        owner = uuid.uuid4().hex
        if existing:
            conn.execute("DELETE FROM youtube_playlist_map WHERE book_id=? AND channel_id=?", (book_id, channel_id))
            reclaimed = True
        conn.execute("INSERT INTO youtube_playlist_map (book_id,channel_id,playlist_id,mode,created_at,updated_at) VALUES (?,?,?,?,?,?)", (book_id, channel_id, "__creating__", f"auto-create:{owner}", _now_iso(), _now_iso()))
        conn.commit(); break
    else:
        raise RuntimeError("playlist resolution timed out")
    heartbeat = _start_playlist_heartbeat(conn, book_id, channel_id, owner)
    try:
        heartbeat_error = getattr(heartbeat, "error", None)
        if heartbeat_error:
            raise RuntimeError("playlist heartbeat failed") from heartbeat_error
        title = template_values.get("_playlist_title_template", "{book_title}").format(**template_values)
        description = template_values.get("_playlist_description_template", "").format(**template_values)
        marker = _playlist_marker(book_id)
        if reclaimed:
            for playlist in list_playlists(conn):
                if marker in playlist.get("snippet", {}).get("description", "").replace("\r\n", "\n").split("\n"):
                    playlist_id = playlist.get("id")
                    adopted = conn.execute("UPDATE youtube_playlist_map SET playlist_id=?, updated_at=? WHERE book_id=? AND channel_id=? AND playlist_id='__creating__' AND mode=?", (playlist_id, _now_iso(), book_id, channel_id, f"auto-create:{owner}")).rowcount
                    if adopted:
                        conn.commit(); return playlist_id
                    winner = conn.execute("SELECT playlist_id FROM youtube_playlist_map WHERE book_id=? AND channel_id=?", (book_id, channel_id)).fetchone()
                    if winner and winner["playlist_id"] != "__creating__":
                        conn.commit(); return winner["playlist_id"]
                    conn.rollback(); continue
        if marker not in description:
            description = f"{description}\n{marker}" if description else marker
        if not 1 <= len(title) <= 150 or len(description) > 5000:
            conn.execute("DELETE FROM youtube_playlist_map WHERE book_id=? AND channel_id=? AND playlist_id='__creating__' AND mode=?", (book_id, channel_id, f"auto-create:{owner}")); conn.commit()
            raise ValueError("invalid playlist metadata length")
        privacy = template_values.get("_playlist_privacy", "private")
        try:
            playlist = create_playlist(conn, title, description, privacy)
        except Exception:
            conn.execute("DELETE FROM youtube_playlist_map WHERE book_id=? AND channel_id=? AND playlist_id='__creating__' AND mode=?", (book_id, channel_id, f"auto-create:{owner}")); conn.commit()
            raise
        playlist_id = playlist["id"]
        heartbeat_error = getattr(heartbeat, "error", None)
        if heartbeat_error:
            raise RuntimeError("playlist heartbeat failed") from heartbeat_error
        updated = conn.execute("UPDATE youtube_playlist_map SET playlist_id=?, updated_at=? WHERE book_id=? AND channel_id=? AND mode=? AND playlist_id='__creating__'", (playlist_id, _now_iso(), book_id, channel_id, f"auto-create:{owner}")).rowcount
        if not updated:
            winner = conn.execute("SELECT playlist_id FROM youtube_playlist_map WHERE book_id=? AND channel_id=?", (book_id, channel_id)).fetchone()
            if winner and winner["playlist_id"] != "__creating__":
                conn.commit(); return winner["playlist_id"]
            for remote in list_playlists(conn):
                if marker in remote.get("snippet", {}).get("description", "").replace("\r\n", "\n").split("\n"):
                    adopted = conn.execute("UPDATE youtube_playlist_map SET playlist_id=?, updated_at=? WHERE book_id=? AND channel_id=? AND playlist_id='__creating__' AND mode=?", (remote["id"], _now_iso(), book_id, channel_id, f"auto-create:{owner}")).rowcount
                    if adopted:
                        conn.commit(); return remote["id"]
                    winner = conn.execute("SELECT playlist_id FROM youtube_playlist_map WHERE book_id=? AND channel_id=?", (book_id, channel_id)).fetchone()
                    if winner and winner["playlist_id"] != "__creating__":
                        conn.commit(); return winner["playlist_id"]
                    conn.rollback(); raise RuntimeError("playlist claim ownership lost")
            raise RuntimeError("playlist claim ownership lost")
        conn.commit()
        return playlist_id
    finally:
        heartbeat.stop(); heartbeat.join(); heartbeat.close()
        if memory_lock:
            memory_lock.release()


def postprocess_upload(conn: sqlite3.Connection, upload_id: int) -> dict:
    """Set thumbnail and add to playlist for a completed upload.

    Each step is persisted independently for idempotent retry.
    Returns {status: "published"|"auth_required"|"failed", youtube_video_id}.
    """
    row = _dict(conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone())
    if row is None:
        raise ValueError(f"upload {upload_id} not found")
    if row["status"] != "done" or not row["youtube_video_id"]:
        raise ValueError(f"upload {upload_id} is not done")
    if row["thumbnail_status"] == "done" and row["playlist_status"] == "done":
        return {"status": "published", "youtube_video_id": row["youtube_video_id"]}

    try:
        youtube_video_id = row["youtube_video_id"]
        metadata = json.loads(row["metadata_snapshot"]) if row["metadata_snapshot"] else {}
        youtube_config = metadata.get("automation", {}).get("youtube", {})
        if "mode" in youtube_config:
            youtube_config = {**youtube_config, "playlist_mode": youtube_config["mode"], "playlist_id": youtube_config.get("playlist_id", "")}
        if youtube_config.get("playlist_mode") == "create":
            youtube_config = {**youtube_config, "playlist_mode": "auto-create", "playlist_id": ""}

        if row["thumbnail_status"] != "done":
            thumbnail_path = None
            pipeline = conn.execute(
                "SELECT thumbnail_path FROM patch_pipeline WHERE youtube_upload_id=?",
                (upload_id,),
            ).fetchone()
            if pipeline and pipeline["thumbnail_path"]:
                thumbnail_path = pipeline["thumbnail_path"]
            if not thumbnail_path:
                fallback = metadata.get("background_fallback")
                if fallback and Path(fallback).is_file():
                    thumbnail_path = fallback
            if thumbnail_path and Path(thumbnail_path).is_file():
                try:
                    set_thumbnail(conn, youtube_video_id, thumbnail_path)
                except Exception:
                    conn.execute(
                        "UPDATE youtube_uploads SET thumbnail_status='failed', thumbnail_error=? WHERE id=?",
                        (str(_exception_safe()), upload_id),
                    )
                    conn.commit()
                    raise
            elif thumbnail_path:
                raise FileNotFoundError(f"Thumbnail file not found: {thumbnail_path}")
            conn.execute(
                "UPDATE youtube_uploads SET thumbnail_status='done' WHERE id=?",
                (upload_id,),
            )
            conn.commit()

        if row["playlist_status"] != "done":
            playlist_mode = youtube_config.get("playlist_mode", "none")
            if playlist_mode != "none":
                # resolve_book_playlist may create a playlist and playlist_contains_video pages
                # through every item, so this stretch is slow enough to be worth showing. Marked
                # before the first API call so the UI can tell "not started" from "running".
                conn.execute(
                    "UPDATE youtube_uploads SET playlist_status='processing' WHERE id=?",
                    (upload_id,),
                )
                conn.commit()
                creds = get_creds_from_db(conn)
                channel_id = creds["channel_id"] if creds else ""
                playlist_id = youtube_config.get("playlist_id") or ""
                if playlist_mode == "auto-create" and channel_id:
                    book_id = _resolve_book_id(conn, upload_id)
                    snapshot = conn.execute("SELECT config_snapshot FROM patch_pipeline WHERE youtube_upload_id=?", (upload_id,)).fetchone()
                    frozen = json.loads(snapshot["config_snapshot"]).get("playlist_template_values", {}) if snapshot else {}
                    book_title = frozen.get("book_title") or "Audiobook"
                    playlist_title_tpl = youtube_config.get("title_template", "{book_title}")
                    playlist_desc_tpl = youtube_config.get("description_template", "")
                    template_values = {
                        "book_title": book_title,
                        "episode_number": frozen.get("episode_number", 1),
                        "chapter_start": frozen.get("chapter_start", ""),
                        "chapter_end": frozen.get("chapter_end", ""),
                        "patch_name": frozen.get("patch_name", ""),
                        "genre_tags": frozen.get("genre_tags", ""),
                        "_playlist_title_template": playlist_title_tpl or "{book_title}",
                        "_playlist_description_template": playlist_desc_tpl or "",
                        "_playlist_privacy": metadata.get("privacy_status", "private"),
                    }
                    if book_id:
                        playlist_id = resolve_book_playlist(conn, book_id, channel_id, template_values)
                if playlist_id:
                    if not playlist_contains_video(conn, playlist_id, youtube_video_id):
                        add_video_to_playlist(conn, playlist_id, youtube_video_id)
                    conn.execute(
                        "UPDATE youtube_uploads SET playlist_status='done', playlist_id=? WHERE id=?",
                        (playlist_id, upload_id),
                    )
                    conn.commit()
                else:
                    conn.execute(
                        "UPDATE youtube_uploads SET playlist_status='done' WHERE id=?",
                        (upload_id,),
                    )
                    conn.commit()
            else:
                conn.execute(
                    "UPDATE youtube_uploads SET playlist_status='done' WHERE id=?",
                    (upload_id,),
                )
                conn.commit()

        return {"status": "published", "youtube_video_id": youtube_video_id}

    except Exception as exc:
        logger.exception("postprocess_upload %s failed", upload_id)
        # Otherwise a crash mid-playlist leaves the row stuck showing "running" forever. Still
        # retryable: the resume path only skips work when the status is exactly 'done'.
        conn.execute(
            "UPDATE youtube_uploads SET playlist_status='failed' WHERE id=? AND playlist_status='processing'",
            (upload_id,),
        )
        conn.commit()
        status_code = getattr(getattr(exc, "resp", None), "status", None)
        if isinstance(exc, RefreshError) or (isinstance(exc, HttpError) and status_code == 401):
            auth_label = "auth_required"
        elif isinstance(exc, HttpError) and status_code == 403:
            reason = ""
            try:
                body = json.loads(exc.content) if hasattr(exc, 'content') and exc.content else {}
                reason = body.get("error", {}).get("errors", [{}])[0].get("reason", "")
            except (ValueError, TypeError, IndexError, AttributeError):
                pass
            if reason in ("quotaExceeded", "rateLimitExceeded", "dailyLimitExceeded", "userRateLimitExceeded"):
                auth_label = "failed"
            elif reason in ("authError", "forbidden", "insufficientPermissions", "youtubeSignupRequired"):
                auth_label = "auth_required"
            else:
                auth_label = "failed"
        else:
            auth_label = None
        if auth_label:
            conn.execute("UPDATE youtube_uploads SET error_message=? WHERE id=?", (f"{auth_label}: " + str(exc)[:1900], upload_id))
            conn.commit()
            return {"status": auth_label, "youtube_video_id": row["youtube_video_id"]}
        return {"status": "failed", "youtube_video_id": row["youtube_video_id"], "error": str(exc)}


def publish_completed_upload(conn: sqlite3.Connection, upload_id: int) -> dict:
    """Resume thumbnail and playlist work for an already uploaded video."""
    return postprocess_upload(conn, upload_id)


def _exception_safe() -> str:
    import traceback
    return traceback.format_exc()[-2000:]


def _resolve_book_id(conn: sqlite3.Connection, upload_id: int) -> int | None:
    row = conn.execute(
        """SELECT p.book_id FROM patch_pipeline pp
           JOIN patch p ON p.id=pp.patch_id
           WHERE pp.youtube_upload_id=?""",
        (upload_id,),
    ).fetchone()
    return row["book_id"] if row else None
