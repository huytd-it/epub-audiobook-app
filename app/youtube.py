"""YouTube Data API v3 integration: OAuth2 flow, video upload, token management."""
from __future__ import annotations

import http.client
import json
import logging
import os
import random
import re
import socket
import sqlite3
import ssl
import time
import uuid
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

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


# ---------------------------------------------------------------------------
# Request pacing and retry
#
# The batch operations further down issue one HTTP request per item, so editing a
# few dozen videos is a burst of a few dozen back-to-back calls. YouTube answers a
# burst that arrives too fast with 403 rateLimitExceeded/userRateLimitExceeded,
# which used to abort the operation half-applied. Every API call goes through
# _execute: it paces requests apart and retries exactly those transient refusals
# with exponential backoff and jitter.
#
# quotaExceeded/dailyLimitExceeded are deliberately NOT retried - the daily quota
# does not come back inside a retry window, so retrying it only spends more
# requests against a door that stays shut until the quota resets.
# ---------------------------------------------------------------------------

RETRYABLE_REASONS = frozenset({
    "rateLimitExceeded", "userRateLimitExceeded", "backendError", "internalError",
})
MAX_ATTEMPTS = 5
# Trần tổng số lần thử lại trong MỘT lần truyền file lên YouTube (xem process_upload).
MAX_TRANSFER_RETRIES = 20
BASE_RETRY_DELAY = 1.0
MAX_RETRY_DELAY = 32.0

# A floor on the gap between any two API requests from this process. Bulk
# operations otherwise fire as fast as the network allows, which is what trips the
# per-user rate limit to begin with; 20 requests/second is far below YouTube's
# ceiling and costs a large batch only a few seconds. Tests set this to 0.
MIN_REQUEST_INTERVAL = 0.05

_last_request_at = 0.0
_pace_lock = threading.Lock()


def _pace() -> None:
    """Block just long enough to keep requests MIN_REQUEST_INTERVAL apart."""
    global _last_request_at
    if MIN_REQUEST_INTERVAL <= 0:
        return
    with _pace_lock:
        wait = _last_request_at + MIN_REQUEST_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _error_reason(exc: Exception) -> str:
    """The `reason` string of a googleapiclient HttpError, or '' when it has none."""
    content = getattr(exc, "content", None)
    if not content:
        return ""
    try:
        body = json.loads(content)
        return body.get("error", {}).get("errors", [{}])[0].get("reason", "") or ""
    except (ValueError, TypeError, IndexError, AttributeError):
        return ""


def _error_detail(exc: Exception) -> str:
    """The `error.message` of a googleapiclient HttpError, or '' when it has none."""
    content = getattr(exc, "content", None)
    if not content:
        return ""
    try:
        body = json.loads(content)
        error = body.get("error", {})
        message = error.get("message") or ""
        if not message:
            message = (error.get("errors") or [{}])[0].get("message", "") or ""
        return message
    except (ValueError, TypeError, IndexError, AttributeError):
        return ""


def describe_error(exc: Exception) -> str:
    """Một dòng mô tả lỗi đủ để chẩn đoán mà không cần mở app.log.

    `str(HttpError)` của googleapiclient là một khối dài lặp cả URL lẫn JSON thô;
    khi nó rơi vào cột error_message của bảng youtube_uploads thì UI chỉ hiện được
    vài chục ký tự đầu — đúng phần vô nghĩa. Ở đây gộp lại thành
    "HTTP 403 quotaExceeded: The request cannot be completed... (HttpError)", giữ
    nguyên các từ khóa mà youtube_upload._is_fatal dò để phân loại lỗi chí tử."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    reason = _error_reason(exc)
    detail = _error_detail(exc) or str(exc).strip()
    head = " ".join(part for part in (f"HTTP {status}" if status else "", reason) if part)
    kind = type(exc).__name__
    if head:
        return f"{head}: {detail} ({kind})"[:1900]
    return f"{kind}: {detail}"[:1900] if detail else kind


def _is_retryable(exc: Exception) -> bool:
    """True for throttling and transient server errors, false for quota exhaustion."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status is None:
        return False
    status = int(status)
    if status == 429 or 500 <= status < 600:
        return True
    if status == 403:
        return _error_reason(exc) in RETRYABLE_REASONS
    return False


# Đứt mạng giữa chừng là chuyện bình thường với một file vài trăm MB. Upload
# resumable giữ nguyên resumable_uri nên gọi lại next_chunk() sẽ đi tiếp từ byte
# đang dở chứ không truyền lại từ đầu — nên những lỗi này đáng retry, khác hẳn
# với 403 quotaExceeded.
_TRANSIENT_TRANSFER_ERRORS: tuple[type[BaseException], ...] = (
    ConnectionError, TimeoutError, ssl.SSLError, http.client.HTTPException, socket.error,
)
# OSError bao cả FileNotFoundError/PermissionError — file biến mất giữa chừng thì
# thử lại bao nhiêu lần cũng thế, nên loại chúng ra khỏi nhánh retry.
_PERMANENT_TRANSFER_ERRORS: tuple[type[BaseException], ...] = (
    FileNotFoundError, PermissionError, IsADirectoryError, NotADirectoryError,
)


def is_winsock_error(exc: BaseException) -> bool:
    """True cho OSError mang mã Winsock (10000-11999) trên Windows.

    `sock.connect()` bị firewall/AV chặn ném PermissionError [WinError 10013] —
    cùng lớp Python với PermissionError của filesystem, nhưng bản chất là sự cố
    mạng của máy chứ không phải file hỏng. Không tách ra thì nó rơi vào
    _PERMANENT_TRANSFER_ERRORS và một lần chặn nhất thời giết luôn lần upload.
    """
    winerror = getattr(exc, "winerror", None)
    return isinstance(winerror, int) and 10000 <= winerror < 12000


def _is_retryable_transfer(exc: Exception) -> bool:
    """True cho lỗi mạng/5xx giữa chừng một lần upload resumable."""
    if is_winsock_error(exc):
        return True
    if isinstance(exc, _PERMANENT_TRANSFER_ERRORS):
        return False
    if _is_retryable(exc):
        return True
    if getattr(getattr(exc, "resp", None), "status", None) is not None:
        return False      # HTTP error đã được _is_retryable phân loại: không thử lại
    return isinstance(exc, _TRANSIENT_TRANSFER_ERRORS)


def _execute(request):
    """Run one googleapiclient request, retrying throttling/5xx with backoff.

    `request` is the object returned by a resource method (e.g.
    service.playlistItems().list(...)); this calls .execute() on it.
    """
    for attempt in range(MAX_ATTEMPTS):
        _pace()
        try:
            return request.execute()
        except Exception as exc:
            if attempt == MAX_ATTEMPTS - 1 or not _is_retryable(exc):
                raise
            delay = min(BASE_RETRY_DELAY * (2 ** attempt), MAX_RETRY_DELAY)
            delay += random.uniform(0, delay / 2)
            logger.warning(
                "YouTube API throttled (%s); retrying in %.1fs (attempt %d/%d)",
                _error_reason(exc) or exc, delay, attempt + 1, MAX_ATTEMPTS,
            )
            time.sleep(delay)


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
    ch_resp = _execute(youtube.channels().list(part="snippet", mine=True))
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


class UploadInProgress(RuntimeError):
    """Another worker already owns this upload's transfer.

    Raised instead of starting a second real upload when the job queue
    double-dispatches the same upload_id (e.g. the reaper reclaimed a job
    that was still genuinely running). The caller should treat this as
    retryable, not as a failed upload — the row that is actually
    transferring will resolve to 'done' or 'failed' on its own."""


def process_upload(
    conn: sqlite3.Connection,
    upload_id: int,
    *,
    progress_cb: Callable[[dict], None] | None = None,
) -> dict:
    """Upload a video for an existing youtube_uploads row.

    Updates the existing row from pending → uploading → done.
    Never creates a second row.
    Returns {youtube_video_id, status}.

    `progress_cb` nhận từng event có cấu trúc của lần truyền này —
    start / progress / retry / done / error (xem _emit bên dưới). Job queue dùng
    nó để đổ tiến độ và lỗi vào nhật ký job; gọi trực tiếp thì bỏ trống cũng được.
    Callback ném lỗi không được phép làm hỏng lần upload đang chạy.
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

    # Atomic claim: if the job queue double-dispatches this upload_id (the
    # job was reaped as stale while a first worker was still genuinely
    # uploading), the loser must not start a second real transfer to
    # YouTube. Only the caller that flips status away from
    # 'uploading'/'done' is allowed to proceed.
    claimed = conn.execute(
        "UPDATE youtube_uploads SET status='uploading', upload_progress=0 "
        "WHERE id=? AND status NOT IN ('uploading', 'done')",
        (upload_id,),
    )
    conn.commit()
    if claimed.rowcount == 0:
        current = conn.execute(
            "SELECT status, youtube_video_id FROM youtube_uploads WHERE id=?", (upload_id,)
        ).fetchone()
        if current is not None and current["status"] == "done":
            return {"youtube_video_id": current["youtube_video_id"], "status": "done"}
        raise UploadInProgress(f"upload {upload_id} đang được worker khác xử lý")

    def _emit(event: dict) -> None:
        if progress_cb is None:
            return
        try:
            progress_cb({"upload_id": upload_id, **event})
        except Exception:      # nhật ký hỏng thì kệ, không được kéo theo lần upload
            logger.warning("progress_cb của upload %s ném lỗi", upload_id, exc_info=True)

    try:
        bytes_total = video_file.stat().st_size
    except OSError:
        bytes_total = 0
    started_at = time.monotonic()

    try:
        youtube = get_youtube_service(conn)
        tags = json.loads(row["tags"]) if row["tags"] else []
        title = (row["title"] or "")[:100]
        description = (row["description"] or "")[:5000]
        privacy_status = row.get("privacy_status", "private")
        # Ô "Sử dụng AI" trong YouTube Studio. Ưu tiên giá trị đã lưu cho từng bản ghi
        # (sửa hàng loạt qua export/import), fallback về setting toàn cục cho các dòng cũ.
        if "altered_content" in row:
            altered_content = bool(row.get("altered_content", 1))
        else:
            altered_content = bool(settings.youtube_declare_altered_content)

        body = {
            "snippet": {
                "title": title,
                "description": description,
                "tags": (tags or [])[:30],
                "categoryId": "26",
            },
            "status": {
                "privacyStatus": privacy_status,
                "selfDeclaredMadeForKids": not bool(row.get("not_for_kids", 1)),
                "containsSyntheticMedia": altered_content,
            },
        }

        media = MediaFileUpload(
            str(video_file),
            mimetype="video/mp4",
            resumable=True,
            chunksize=_UPLOAD_CHUNK_SIZE,
        )

        _emit({
            "type": "start",
            "file": str(video_file),
            "file_name": video_file.name,
            "bytes_total": bytes_total,
            "chunk_size": _UPLOAD_CHUNK_SIZE,
            "title": title,
            "privacy_status": privacy_status,
            "tags": (tags or [])[:30],
            "description_chars": len(description),
            "made_for_kids": not bool(row.get("not_for_kids", 1)),
            "altered_content": altered_content,
            "category_id": "26",
        })

        req = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

        response = None
        bytes_done = 0
        transient_failures = 0      # tổng số lần thử lại của cả lần truyền này
        consecutive_failures = 0    # số lần hỏng liên tiếp, reset sau mỗi khối đi lọt
        while response is None:
            try:
                status, response = req.next_chunk()
            except Exception as exc:
                # Một chunk hỏng chưa phải là cả lần upload hỏng: request giữ
                # resumable_uri nên next_chunk() kế tiếp đi tiếp từ chỗ đang dở.
                #
                # Đếm hai lần cố ý. `consecutive` là thứ quyết định bỏ cuộc — một
                # file 2 GB truyền cả tiếng đồng hồ thì vài lần rớt mạng rải rác là
                # bình thường, không đáng hủy cả lần upload chỉ vì lần thứ năm.
                # `transient_failures` là trần tổng, để một kết nối chập chờn kiểu
                # rớt-rồi-đi-được-một-khối không quay vòng mãi mãi.
                consecutive_failures += 1
                transient_failures += 1
                if (consecutive_failures >= MAX_ATTEMPTS
                        or transient_failures >= MAX_TRANSFER_RETRIES
                        or not _is_retryable_transfer(exc)):
                    raise
                delay = min(BASE_RETRY_DELAY * (2 ** (consecutive_failures - 1)), MAX_RETRY_DELAY)
                delay += random.uniform(0, delay / 2)
                described = describe_error(exc)
                logger.warning(
                    "YouTube upload %s gián đoạn (%s); thử lại sau %.1fs (lần %d/%d)",
                    upload_id, described, delay, consecutive_failures, MAX_ATTEMPTS,
                )
                _emit({
                    "type": "retry", "attempt": consecutive_failures, "max_attempts": MAX_ATTEMPTS,
                    "total_retries": transient_failures, "max_total_retries": MAX_TRANSFER_RETRIES,
                    "delay": round(delay, 1), "error": described,
                    "bytes_done": bytes_done, "bytes_total": bytes_total,
                })
                time.sleep(delay)
                continue
            consecutive_failures = 0
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
                # resumable_progress là byte thật đã lên server; status.progress() chỉ là
                # tỉ lệ. Ưu tiên byte thật, và suy ra từ tỉ lệ khi API không đưa (test giả).
                bytes_done = getattr(status, "resumable_progress", None)
                if not isinstance(bytes_done, int):
                    bytes_done = int(pct / 100 * bytes_total)
                elapsed = time.monotonic() - started_at
                speed = bytes_done / elapsed if elapsed > 0 else 0.0
                remaining = max(0, bytes_total - bytes_done)
                _emit({
                    "type": "progress", "percent": pct,
                    "bytes_done": bytes_done, "bytes_total": bytes_total,
                    "elapsed": elapsed, "speed_bps": speed,
                    "eta_seconds": (remaining / speed) if speed > 0 else None,
                })
                logger.info("YouTube upload %s: %d%%", upload_id, int(pct))

        youtube_video_id = response.get("id", "")
        conn.execute(
            "UPDATE youtube_uploads SET youtube_video_id=?, status='done', upload_progress=100, uploaded_at=?, error_message=NULL WHERE id=?",
            (youtube_video_id, _now_iso(), upload_id),
        )
        conn.commit()
        elapsed = time.monotonic() - started_at
        _emit({
            "type": "done", "youtube_video_id": youtube_video_id,
            "bytes_total": bytes_total, "elapsed": elapsed,
            "speed_bps": (bytes_total / elapsed) if elapsed > 0 else 0.0,
            "retries": transient_failures,
        })
        logger.info("YouTube upload %s done: %s", upload_id, youtube_video_id)
        return {"youtube_video_id": youtube_video_id, "status": "done"}

    except Exception as exc:
        described = describe_error(exc)
        conn.execute(
            "UPDATE youtube_uploads SET status='failed', error_message=? WHERE id=?",
            (described, upload_id),
        )
        conn.commit()
        _emit({
            "type": "error", "error": described,
            "error_class": type(exc).__name__,
            "http_status": getattr(getattr(exc, "resp", None), "status", None),
            "reason": _error_reason(exc),
            "bytes_total": bytes_total,
            "elapsed": time.monotonic() - started_at,
        })
        logger.exception("YouTube upload %s failed", upload_id)
        return {"youtube_video_id": None, "status": "failed", "error": described}


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
    not_for_kids: bool = True,
    ai_labels_enabled: bool = False,
    altered_content: bool | None = None,
) -> int:
    """Create a pending youtube_uploads record. Returns upload_id.

    The actual upload is done by the caller (worker or route).
    `altered_content` is the per-upload disclosure for YouTube's
    "Sử dụng AI" / containsSyntheticMedia. None -> global default
    `youtube_declare_altered_content` (env YOUTUBE_DECLARE_ALTERED_CONTENT).
    """
    if render_source_type not in {"book", "patch", "standalone", "external"}:
        raise ValueError("invalid render_source_type")
    if privacy_status is None:
        privacy_status = settings.youtube_default_privacy
    if altered_content is None:
        altered_content = bool(settings.youtube_declare_altered_content)
    metadata_snapshot = json.dumps({"automation": {"youtube": {
        "playlist_mode": "existing", "playlist_id": playlist_id,
    }}}) if playlist_id else None
    now = _now_iso()
    cursor = conn.execute(
        """INSERT INTO youtube_uploads
           (video_id, video_path, title, description, tags, privacy_status, status,
            metadata_snapshot, render_source_type, render_source_id, not_for_kids,
            ai_labels_enabled, altered_content, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?)""",
        (video_id, video_path, title, description, json.dumps(tags or []), privacy_status,
         metadata_snapshot, render_source_type, render_source_id,
         1 if not_for_kids else 0, 1 if ai_labels_enabled else 0,
         1 if altered_content else 0, now),
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


def _upload_has_playlist(row: dict) -> bool:
    """Whether an upload row is (or will be) assigned to a playlist.

    `playlist_id` is filled in once the auto-playlist step actually runs;
    `metadata_snapshot` carries the *intended* playlist before that.
    """
    if row.get("playlist_id"):
        return True
    raw = row.get("metadata_snapshot")
    if not raw:
        return False
    try:
        snapshot = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(snapshot, dict):
        return False
    config = snapshot.get("automation", {}).get("youtube", {})
    return bool(isinstance(config, dict) and config.get("playlist_id"))


_UPLOAD_SORT_COLUMNS = {"created_at", "title", "status", "privacy_status"}


def list_uploads(
    conn: sqlite3.Connection,
    *,
    limit: int = 1000,
    search: str = "",
    status: str = "",
    privacy_status: str = "",
    has_playlist: str = "",
    not_for_kids: str = "",
    ai_labels_enabled: str = "",
    altered_content: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "created_at",
    order: str = "desc",
) -> list[dict]:
    """List upload-queue rows with optional search/filter/sort.

    `has_playlist` ('yes'/'no') is applied in Python via `_upload_has_playlist`
    since playlist intent can live in either the `playlist_id` column or
    `metadata_snapshot`; every other filter is a plain SQL predicate.
    """
    sort_col = sort if sort in _UPLOAD_SORT_COLUMNS else "created_at"
    sort_dir = "ASC" if order == "asc" else "DESC"

    where: list[str] = []
    params: list = []
    if search:
        where.append("(title LIKE ? OR description LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    if status:
        where.append("status = ?")
        params.append(status)
    if privacy_status:
        where.append("privacy_status = ?")
        params.append(privacy_status)
    if not_for_kids in ("0", "1"):
        where.append("not_for_kids = ?")
        params.append(int(not_for_kids))
    if ai_labels_enabled in ("0", "1"):
        where.append("ai_labels_enabled = ?")
        params.append(int(ai_labels_enabled))
    if altered_content in ("0", "1"):
        # Cột mới: DB chưa migrate thì SELECT * không có nó. Trước 1s sau khi upgrade,
        # xử lý như filter rỗng thay vì ném lỗi.
        try:
            exists = conn.execute("SELECT altered_content FROM youtube_uploads LIMIT 0")
            exists.close()
        except Exception:
            pass
        else:
            where.append("altered_content = ?")
            params.append(int(altered_content))
    if date_from:
        where.append("created_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("created_at <= ?")
        params.append(date_to)

    sql = "SELECT * FROM youtube_uploads"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += f" ORDER BY {sort_col} {sort_dir} LIMIT ?"
    params.append(limit)

    rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    if has_playlist in ("yes", "no"):
        want = has_playlist == "yes"
        rows = [r for r in rows if _upload_has_playlist(r) == want]
    return rows


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


def set_upload_scheduled_publish(conn: sqlite3.Connection, upload_id: int, scheduled_publish_at: str | None) -> None:
    """Set or clear the scheduled publish time for an upload."""
    conn.execute(
        "UPDATE youtube_uploads SET scheduled_publish_at=? WHERE id=?",
        (scheduled_publish_at, upload_id),
    )
    conn.commit()


def set_upload_ai_labels(conn: sqlite3.Connection, upload_id: int, ai_labels: list[str]) -> None:
    """Set AI-generated labels for an upload, merge into tags, and mark the
    AI-labels feature as enabled for this upload."""
    labels_json = json.dumps(ai_labels)
    row = conn.execute("SELECT tags FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    if row is None:
        return
    existing_tags = json.loads(row["tags"]) if row["tags"] else []
    # Merge AI labels into tags (deduplicate)
    merged = list(dict.fromkeys(existing_tags + ai_labels))
    conn.execute(
        "UPDATE youtube_uploads SET ai_labels=?, tags=?, ai_labels_enabled=1 WHERE id=?",
        (labels_json, json.dumps(merged), upload_id),
    )
    conn.commit()


_UPLOAD_EDITABLE_FIELDS = {
    "title", "description", "tags", "privacy_status", "not_for_kids", "ai_labels_enabled",
    "altered_content",
}


def update_upload_fields(conn: sqlite3.Connection, upload_id: int, **fields) -> dict | None:
    """Generic column updater for a youtube_uploads row.

    Only keys in `_UPLOAD_EDITABLE_FIELDS` are written; `tags` is JSON-encoded
    and the two boolean flags are coerced to 0/1. Returns the updated row, or
    None if `upload_id` doesn't exist.
    """
    updates = {k: v for k, v in fields.items() if k in _UPLOAD_EDITABLE_FIELDS and v is not None}
    if updates:
        if "tags" in updates:
            updates["tags"] = json.dumps(updates["tags"])
        for boolean_field in ("not_for_kids", "ai_labels_enabled", "altered_content"):
            if boolean_field in updates:
                updates[boolean_field] = 1 if updates[boolean_field] else 0
        set_clause = ", ".join(f"{k}=?" for k in updates)
        params = list(updates.values()) + [upload_id]
        conn.execute(f"UPDATE youtube_uploads SET {set_clause} WHERE id=?", params)
        conn.commit()
    row = conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    return dict(row) if row else None


def generate_ai_labels(title: str, description: str = "") -> list[str]:
    """Generate AI labels/tags from video title and description.

    Uses simple keyword extraction as a lightweight alternative to a full NLP model.
    Returns a list of relevant tags.
    """
    import re
    text = f"{title} {description}".lower()
    # Common Vietnamese audiobook keywords
    keywords = {
        "truyện ngắn": ["truyện ngắn", "short story"],
        "tiểu thuyết": ["tiểu thuyết", "novel"],
        "trinh thám": ["trinh thám", "mystery", "detective"],
        "tình cảm": ["tình cảm", "romance", "love"],
        "hài hước": ["hài hước", "comedy", "funny"],
        "kinh dị": ["kinh dị", "horror", "scary"],
        "viễn tưởng": ["viễn tưởng", "sci-fi", "fantasy"],
        "lịch sử": ["lịch sử", "history"],
        "đời sống": ["đời sống", "lifestyle"],
        "tâm lý": ["tâm lý", "psychology"],
        "sách nói": ["sách nói", "audiobook"],
        "audio book": ["audiobook", "audio book"],
        "podcast": ["podcast"],
        "truyện cười": ["truyện cười", "jokes"],
        "cổ tích": ["cổ tích", "fairy tale"],
        "ngôn tình": ["ngôn tình", "romance novel"],
        "văn học": ["văn học", "literature"],
        "tiên hiệp": ["tiên hiệp", "xianxia"],
        "huyền huyễn": ["huyền huyễn", "fantasy"],
        "đam mỹ": ["đam mỹ", "bl", "boys love"],
    }
    found = []
    for label, triggers in keywords.items():
        for trigger in triggers:
            if trigger in text:
                found.append(label)
                break
    return found[:10]  # Cap at 10 labels


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


IMAGE_UPLOAD_LIMIT = 2 * 1024 * 1024


def _shrink_image_for_upload(source: Path, max_size: tuple[int, int]) -> Path | None:
    """Write a JPEG copy of `source` under the 2MB API cap.

    Returns the temp file, or None when the original already fits and can be
    uploaded as-is. The caller owns the temp file and must unlink it.
    """
    if source.stat().st_size <= IMAGE_UPLOAD_LIMIT:
        return None
    import tempfile
    from PIL import Image

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as output:
        temporary_path = Path(output.name)
    try:
        with Image.open(source) as image:
            image = image.convert("RGB")
            image.thumbnail(max_size)
            quality = 85
            image.save(temporary_path, "JPEG", quality=quality, optimize=True)
            while temporary_path.stat().st_size > IMAGE_UPLOAD_LIMIT and quality > 10:
                quality -= 10
                image.save(temporary_path, "JPEG", quality=quality, optimize=True)
    except Exception:
        # The caller only unlinks what it was handed back, so a half-written
        # temp file has to be cleaned up here.
        temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _image_media(upload_path: Path, temporary_path: Path | None):
    mimetype = "image/jpeg" if upload_path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    if temporary_path:
        # MediaFileUpload keeps its file handle open for the lifetime of the
        # object, which on Windows makes the unlink below fail with WinError 32.
        # These images are capped at 2MB, so upload the temp file from memory.
        import io

        return MediaIoBaseUpload(io.BytesIO(temporary_path.read_bytes()), mimetype=mimetype)
    return MediaFileUpload(str(upload_path), mimetype=mimetype)


def set_thumbnail(conn: sqlite3.Connection, youtube_video_id: str, thumbnail_path: str) -> None:
    """Set the custom thumbnail for a published video."""
    _require_google_imports()
    service = get_youtube_service(conn)
    upload_path = Path(thumbnail_path)
    temporary_path = None
    try:
        temporary_path = _shrink_image_for_upload(upload_path, (1280, 720))
        if temporary_path:
            upload_path = temporary_path
        media = _image_media(upload_path, temporary_path)
        _execute(service.thumbnails().set(videoId=youtube_video_id, media_body=media))
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
        resp = _execute(service.playlists().list(**params))
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
    default_language: str = "vi",
) -> dict:
    """Create a new playlist. Returns the API response dict."""
    _require_google_imports()
    service = get_youtube_service(conn)
    body = {
        "snippet": {"title": title, "description": description, "defaultLanguage": default_language or "vi"},
        "status": {"privacyStatus": privacy},
    }
    return _execute(service.playlists().insert(part="snippet,status", body=body))


# ---------------------------------------------------------------------------
# Podcast: một playlist được YouTube đánh dấu là podcast, kèm ảnh bìa vuông 1:1
# (playlistImages, type "hero"). Cả hai đều là thiết lập cấp "chương trình" nên
# chỉ cần đẩy lên khi cấu hình hoặc ảnh bìa đổi — xem sync_playlist_podcast.
# ---------------------------------------------------------------------------


def get_playlist(conn: sqlite3.Connection, playlist_id: str) -> dict | None:
    """Fetch one playlist (snippet + status), or None when it is gone."""
    _require_google_imports()
    service = get_youtube_service(conn)
    resp = _execute(service.playlists().list(part="snippet,status", id=playlist_id, maxResults=1))
    items = resp.get("items") or []
    return items[0] if items else None


def set_playlist_podcast(conn: sqlite3.Connection, playlist_id: str, enabled: bool = True) -> dict:
    """Turn YouTube's podcast flag on/off for a playlist. Returns the API response.

    playlists.update replaces the parts it is given, so the current snippet is
    read back first — sending status alone would blank the title/description.
    """
    _require_google_imports()
    playlist = get_playlist(conn, playlist_id)
    if playlist is None:
        raise ValueError(f"playlist {playlist_id} not found")
    snippet = playlist.get("snippet") or {}
    status = playlist.get("status") or {}
    body = {
        "id": playlist_id,
        "snippet": {
            "title": snippet.get("title", ""),
            "description": snippet.get("description", ""),
        },
        "status": {
            "privacyStatus": status.get("privacyStatus", "private"),
            "podcastStatus": "enabled" if enabled else "disabled",
        },
    }
    service = get_youtube_service(conn)
    return _execute(service.playlists().update(part="snippet,status", body=body))


def update_playlist_title(conn: sqlite3.Connection, playlist_id: str, title: str, description: str | None = None) -> dict:
    """Update a playlist's title (and optionally description), preserving other fields."""
    _require_google_imports()
    playlist = get_playlist(conn, playlist_id)
    if playlist is None:
        raise ValueError(f"playlist {playlist_id} not found")
    snippet = playlist.get("snippet") or {}
    status = playlist.get("status") or {}
    body = {
        "id": playlist_id,
        "snippet": {
            "title": title[:150],
            "description": description if description is not None else snippet.get("description", ""),
        },
        "status": {"privacyStatus": status.get("privacyStatus", "private")},
    }
    # preserve podcast flag if present
    if status.get("podcastStatus"):
        body["status"]["podcastStatus"] = status["podcastStatus"]
    service = get_youtube_service(conn)
    return _execute(service.playlists().update(part="snippet,status", body=body))


def update_video_metadata(conn: sqlite3.Connection, video_id: str, title: str,
                          description: str | None = None) -> dict:
    """Rename a video already live on the channel (and optionally re-describe it).

    videos.update replaces the whole snippet part it is given (categoryId included),
    so the current snippet is read back first - sending just a title would blank
    everything else.
    """
    _require_google_imports()
    service = get_youtube_service(conn)
    resp = _execute(service.videos().list(part="snippet", id=video_id, maxResults=1))
    items = resp.get("items") or []
    if not items:
        raise ValueError(f"video {video_id} not found")
    snippet = items[0]["snippet"]
    snippet["title"] = title[:100]
    if description is not None:
        snippet["description"] = description[:5000]
    body = {"id": video_id, "snippet": snippet}
    return _execute(service.videos().update(part="snippet", body=body))


def get_playlist_cover(conn: sqlite3.Connection, playlist_id: str) -> dict | None:
    """The playlist's current hero image resource, or None."""
    _require_google_imports()
    service = get_youtube_service(conn)
    resp = _execute(service.playlistImages().list(part="snippet", parent=playlist_id, maxResults=5))
    items = resp.get("items") or []
    return items[0] if items else None


def set_playlist_cover(conn: sqlite3.Connection, playlist_id: str, image_path: str) -> dict:
    """Upload the square podcast cover for a playlist (insert or replace)."""
    _require_google_imports()
    upload_path = Path(image_path)
    if not upload_path.is_file():
        raise FileNotFoundError(f"Podcast cover not found: {image_path}")
    service = get_youtube_service(conn)
    existing = get_playlist_cover(conn, playlist_id)
    temporary_path = None
    try:
        temporary_path = _shrink_image_for_upload(upload_path, (1280, 1280))
        if temporary_path:
            upload_path = temporary_path
        media = _image_media(upload_path, temporary_path)
        body: dict = {"snippet": {"playlistId": playlist_id, "type": "hero"}}
        if existing and existing.get("id"):
            body["id"] = existing["id"]
            return _execute(service.playlistImages().update(part="snippet", body=body, media_body=media))
        return _execute(service.playlistImages().insert(part="snippet", body=body, media_body=media))
    finally:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)


def _file_sha(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def get_podcast_state(conn: sqlite3.Connection, book_id: int, playlist_id: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM youtube_podcast_state WHERE book_id=? AND playlist_id=?",
        (book_id, playlist_id),
    ).fetchone()
    return _dict(row)


def sync_playlist_podcast(
    conn: sqlite3.Connection,
    book_id: int,
    playlist_id: str,
    *,
    enabled: bool,
    cover_path: str | None = None,
    force: bool = False,
) -> dict:
    """Push podcast flag + cover art to a playlist, skipping unchanged work.

    Every published episode would otherwise re-send the same two calls, so the
    last applied state is recorded per (book, playlist) and compared first.
    `force` re-sends regardless — that is what the "apply now" button uses.
    Returns {playlist_id, podcast, cover, changed}.
    """
    if not playlist_id:
        raise ValueError("playlist_id is required")
    state = get_podcast_state(conn, book_id, playlist_id) or {}
    want_status = "enabled" if enabled else "disabled"
    cover_sha = ""
    cover_file = Path(cover_path) if cover_path else None
    if enabled and cover_file and cover_file.is_file():
        cover_sha = _file_sha(cover_file)

    result = {"playlist_id": playlist_id, "podcast": "unchanged", "cover": "unchanged", "changed": False}

    if not enabled:
        result["cover"] = "skipped"
    elif cover_file is None:
        result["cover"] = "disabled"
    elif not cover_sha:
        result["cover"] = "missing"
    elif force or state.get("cover_sha") != cover_sha:
        set_playlist_cover(conn, playlist_id, str(cover_file))
        result["cover"] = "uploaded"
        result["changed"] = True

    # YouTube rejects podcastStatus=enabled until a playlist image exists.
    # Upload/replace the hero art first, then enable the podcast flag.
    if force or state.get("podcast_status") != want_status:
        set_playlist_podcast(conn, playlist_id, enabled)
        result["podcast"] = want_status
        result["changed"] = True

    conn.execute(
        """INSERT INTO youtube_podcast_state (book_id, playlist_id, podcast_status, cover_sha, updated_at)
           VALUES (?,?,?,?,?)
           ON CONFLICT(book_id, playlist_id) DO UPDATE SET
               podcast_status=excluded.podcast_status,
               cover_sha=excluded.cover_sha,
               updated_at=excluded.updated_at""",
        (book_id, playlist_id, want_status, cover_sha or state.get("cover_sha", ""), _now_iso()),
    )
    conn.commit()
    return result


def playlist_contains_video(conn: sqlite3.Connection, playlist_id: str, youtube_video_id: str) -> bool:
    """Check if a video is already in a playlist."""
    _require_google_imports()
    service = get_youtube_service(conn)
    page_token = None
    while True:
        params = {"part": "snippet", "playlistId": playlist_id, "maxResults": 50}
        if page_token:
            params["pageToken"] = page_token
        resp = _execute(service.playlistItems().list(**params))
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
    response = _add_playlist_item(service, playlist_id, youtube_video_id, position=position)
    # Keep the channel-videos cache's membership column honest (sync_channel_videos
    # no longer re-scans playlists, so this mirror is where the knowledge lives).
    _cache_add_video_to_playlists(conn, youtube_video_id, playlist_id)
    _commit_if_real(conn)
    return response


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
        "description": snippet.get("description", "") or "",
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
    resp = _execute(service.playlistItems().list(**params))
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
    return _execute(service.playlistItems().insert(part="snippet", body=body))


def _delete_playlist_item(service, playlist_item_id: str) -> dict:
    return _execute(service.playlistItems().delete(id=playlist_item_id))


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
    return _execute(service.playlistItems().update(part="snippet", body=body))


def remove_playlist_item(conn: sqlite3.Connection, playlist_id: str, playlist_item_id: str) -> dict:
    """Remove an item from a playlist by its playlistItem.id. Returns the API response.

    Mirrors the removal into the channel-videos cache when `playlist_id` is known.
    The playlist-item-only DELETE route passes "" for the playlist id, so the cache
    just waits for the next sync there.
    """
    _require_google_imports()
    service = get_youtube_service(conn)
    response = _delete_playlist_item(service, playlist_item_id)
    if playlist_id:
        video_id = (response.get("snippet") or {}).get("resourceId", {}).get("videoId", "")
        if video_id:
            _cache_remove_video_from_playlist(conn, video_id, playlist_id)
        _commit_if_real(conn)
    return response


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
    _cache_remove_video_from_playlist(conn, youtube_video_id, playlist_id)
    _commit_if_real(conn)
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
            _cache_add_video_to_playlists(conn, video_id, playlist_id)
            _batch_add(result, video_id, "succeeded")
        except Exception as exc:
            _batch_add(result, video_id, "failed", str(exc)[:500])
    _commit_if_real(conn)
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
            # `key` is the video id only when we resolved it above (the by-video-id
            # branch); the by-item-id branch doesn't know the video id, so the cache
            # just won't reflect that removal until the next full sync.
            if playlist_id and video_ids:
                _cache_remove_video_from_playlist(conn, key, playlist_id)
            _batch_add(result, key, "succeeded")
        except Exception as exc:
            _batch_add(result, key, "failed", str(exc)[:500])
    _commit_if_real(conn)
    return result


def _select_source_items(service, source_playlist_id: str, video_ids: list[str] | None,
                         items: list[dict] | None = None) -> list[dict]:
    if items is None:
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
    source_items: list[dict] | None = None,
) -> dict:
    """Copy items from one playlist into another. Returns a standardized batch result.

    Videos already present in the target playlist are skipped as duplicates. When
    video_ids is given only those videos are copied.

    Pass `source_items` when the caller has already paged the source playlist (the
    route does, to map playlistItem ids to video ids) so it is not read twice.
    """
    result = _new_batch_result()
    _require_google_imports()
    service = get_youtube_service(conn)
    sources = _select_source_items(service, source_playlist_id, video_ids, source_items)
    existing = {i["video_id"] for i in _get_all_playlist_items(service, target_playlist_id)} if skip_duplicates else set()
    for item in sources:
        video_id = item["video_id"]
        if video_id in existing:
            _batch_add(result, video_id, "skipped", "duplicate: already in target playlist")
            continue
        try:
            _add_playlist_item(service, target_playlist_id, video_id)
            _cache_add_video_to_playlists(conn, video_id, target_playlist_id)
            _batch_add(result, video_id, "succeeded")
        except Exception as exc:
            _batch_add(result, video_id, "failed", str(exc)[:500])
    _commit_if_real(conn)
    return result


def move_playlist_items(
    conn: sqlite3.Connection,
    source_playlist_id: str,
    target_playlist_id: str,
    video_ids: list[str] | None = None,
    skip_duplicates: bool = True,
    source_items: list[dict] | None = None,
) -> dict:
    """Move items from one playlist to another (add-before-remove).

    Each video is added to the target first and only removed from the source after a
    successful add, so an add failure or a duplicate skip leaves the source item
    retained. Returns a standardized batch result.

    Pass `source_items` when the caller has already paged the source playlist so it
    is not read twice.
    """
    result = _new_batch_result()
    _require_google_imports()
    service = get_youtube_service(conn)
    sources = _select_source_items(service, source_playlist_id, video_ids, source_items)
    existing = {i["video_id"] for i in _get_all_playlist_items(service, target_playlist_id)} if skip_duplicates else set()
    for item in sources:
        video_id = item["video_id"]
        if video_id in existing:
            _batch_add(result, video_id, "skipped", "duplicate: already in target playlist; source retained")
            continue
        try:
            _add_playlist_item(service, target_playlist_id, video_id)
            _cache_add_video_to_playlists(conn, video_id, target_playlist_id)
        except Exception as exc:
            _batch_add(result, video_id, "failed", f"add failed; source retained: {str(exc)[:400]}")
            continue
        try:
            _delete_playlist_item(service, item["playlist_item_id"])
            _cache_remove_video_from_playlist(conn, video_id, source_playlist_id)
            _batch_add(result, video_id, "succeeded")
        except Exception as exc:
            _batch_add(result, video_id, "succeeded", f"added to target; source removal failed: {str(exc)[:300]}")
    _commit_if_real(conn)
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


SORT_MODES = ("natural", "episode", "manual")

_SORT_KEYS = {"natural": _natural_sort_key, "episode": _episode_sort_key}


def _sort_key_for(mode: str):
    """Return the title sort key for a sort mode (unknown modes fall back to natural)."""
    if str(mode or "").lower() == "manual":
        return None
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
    if key is None:
        # manual: giữ nguyên thứ tự hiện tại (không sắp xếp), chỉ đảo nếu desc
        if str(direction).lower() == "desc":
            return list(reversed(items))
        return list(items)
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


def _apply_order(service, playlist_id: str, items: list[dict], ordered: list[dict],
                 start: int, result: dict) -> dict:
    """Issue the playlistItems.update calls that turn `items` into `ordered`.

    `items` is the current order (its first element sits at absolute position
    `start`); `ordered` is the same items permuted into their target order. Target
    positions are assigned ascending from `start`.

    Each update costs 50 quota units, so the point here is to issue as few as
    possible. That needs the simulated order to be kept in step with the real one:
    moving an item to position P shifts every item between its old and new slot, so
    a snapshot taken before the loop goes stale after the very first update and the
    "already in place?" check against it would wave through updates that are
    no-ops. Re-shuffling `current` after each successful update the same way
    YouTube does keeps the check honest - a playlist that is already almost sorted
    now costs almost nothing instead of a full 50 units per item. A failed update
    leaves `current` alone, because that move did not happen.
    """
    current = [i["playlist_item_id"] for i in items]
    for offset, item in enumerate(ordered):
        item_id = item["playlist_item_id"]
        position = start + offset
        try:
            index = current.index(item_id)
        except ValueError:  # not on this page; nothing sensible to move it to
            _batch_add(result, item["video_id"], "failed", "item not found in playlist")
            continue
        if index == offset:
            _batch_add(result, item["video_id"], "skipped", f"already at position {position}")
            continue
        try:
            _update_playlist_item(service, item_id, playlist_id, position, item["video_id"])
            current.insert(offset, current.pop(index))
            _batch_add(result, item["video_id"], "succeeded", f"moved to position {position}")
        except Exception as exc:
            _batch_add(result, item["video_id"], "failed", str(exc)[:500])
    return result


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
    items: list[dict] | None = None,
) -> dict:
    """Apply a reorder to a playlist.

    order is an explicit list of video ids for arbitrary-position reordering; when
    None the playlist is title sorted (asc by default) using the given sort mode.
    Only items whose position actually changes are updated and failures do not stop
    the rest of the batch. Returns a standardized batch result.

    Pass `items` when the caller has already paged the playlist (the routes do, to
    map playlistItem ids to video ids) so this does not read the whole thing twice.
    """
    result = _new_batch_result()
    _require_google_imports()
    service = get_youtube_service(conn)
    if items is None:
        items = _get_all_playlist_items(service, playlist_id)
    return _apply_order(service, playlist_id, items,
                        _new_order(items, order, direction, mode), 0, result)


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
    items: list[dict] | None = None,
) -> dict:
    """Reorder the items that live on one page of a playlist.

    All target positions stay within the page's span, so no update ever has to move
    an item across a page boundary. Returns a standardized batch result.

    Pass `items` (the whole playlist, unfiltered) when the caller has already paged
    it, so this does not read the whole thing a second time.
    """
    result = _new_batch_result()
    _require_google_imports()
    service = get_youtube_service(conn)
    start = page_index * page_size
    span = set(range(start, start + len(order)))
    if items is None:
        items = _get_all_playlist_items(service, playlist_id)
    page_items = [i for i in items if i["position"] in span]
    if len(page_items) != len(order):
        for video_id in order:
            _batch_add(result, video_id, "failed", f"video not found on page {page_index}")
        return result
    return _apply_order(service, playlist_id, page_items,
                        _new_order(page_items, order, "asc"), start, result)


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
    resp = _execute(service.search().list(**params))
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


# ---------------------------------------------------------------------------
# Channel-videos cache (Videos-kênh tab)
#
# The tab needs to browse, search, filter, sort and paginate over *every* video on
# the channel - including ones in no playlist - which the YouTube Data API has no
# single cheap endpoint for (playlist membership has to be cross-referenced against
# every playlist separately). sync_channel_videos() does that cross-reference once
# and snapshots the result into youtube_channel_videos; every other function here
# reads/writes that local cache, so browsing is instant and free of API quota.
# ---------------------------------------------------------------------------

_ISO8601_DURATION_RE = re.compile(
    r"P(?:\d+Y)?(?:\d+M)?(?:\d+D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?"
)

_CHANNEL_VIDEO_SORT_COLUMNS = {
    "title": "title", "published_at": "published_at",
    "view_count": "view_count", "duration_sec": "duration_sec",
}


def _parse_iso8601_duration(text: str | None) -> int | None:
    """'PT1H2M3S' -> 3723 (seconds). None/unparseable input -> None."""
    if not text:
        return None
    match = _ISO8601_DURATION_RE.fullmatch(text.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _videos_by_id(service, video_ids: list[str], part: str) -> dict[str, dict]:
    """videos.list in batches of 50 (the API's per-call cap), keyed by video id.

    One request per 50 ids instead of one per id. Both spend the same 1 quota unit
    per call, but it is the *request count* that YouTube's per-user rate limit
    counts, and that is what a bulk edit of a hundred videos used to blow through.
    """
    found: dict[str, dict] = {}
    for start in range(0, len(video_ids), 50):
        chunk = video_ids[start:start + 50]
        resp = _execute(service.videos().list(part=part, id=",".join(chunk), maxResults=50))
        for item in resp.get("items", []):
            found[item["id"]] = item
    return found


def _playlists_by_id(service, playlist_ids: list[str]) -> dict[str, dict]:
    """playlists.list in batches of 50, keyed by playlist id. See _videos_by_id."""
    found: dict[str, dict] = {}
    for start in range(0, len(playlist_ids), 50):
        chunk = playlist_ids[start:start + 50]
        resp = _execute(service.playlists().list(
            part="snippet,status", id=",".join(chunk), maxResults=50))
        for item in resp.get("items", []):
            found[item["id"]] = item
    return found


def _video_details_by_id(service, video_ids: list[str]) -> dict[str, dict]:
    """The per-video fields the uploads-playlist listing doesn't carry.

    Full description, tags, privacy, category, duration and view count, keyed by
    video id.
    """
    details: dict[str, dict] = {}
    raw = _videos_by_id(service, video_ids, "snippet,status,contentDetails,statistics")
    for video_id, item in raw.items():
        snippet = item.get("snippet") or {}
        status = item.get("status") or {}
        content = item.get("contentDetails") or {}
        stats = item.get("statistics") or {}
        details[video_id] = {
            "description": snippet.get("description", "") or "",
            "tags": snippet.get("tags") or [],
            "privacy_status": status.get("privacyStatus", "private"),
            "category_id": snippet.get("categoryId"),
            "duration_sec": _parse_iso8601_duration(content.get("duration")),
            "view_count": int(stats["viewCount"]) if stats.get("viewCount") is not None else None,
        }
    return details


def sync_channel_videos(conn: sqlite3.Connection, channel_id: str) -> dict:
    """Refresh the local cache of every video on the channel plus its playlist membership.

    Pages the channel's uploads playlist for the video list and batches videos.list
    for the fields that listing omits. Playlist membership is NOT re-scanned from
    YouTube: with a playlist per book that meant one playlistItems.list per playlist
    per sync - the burst of sequential requests that tripped the per-user rate
    limit - and it is information the app already maintains in this same table,
    because every playlist mutation in this module mirrors into it
    (_cache_add_video_to_playlists / _cache_remove_video_from_playlist). Existing
    membership is carried forward and videos the channel no longer has are dropped;
    to rebuild membership from scratch, clear the playlist_ids column first
    (`UPDATE youtube_channel_videos SET playlist_ids='[]'`) and sync again.

    The cache table is fully replaced in one transaction so a video removed from the
    channel since the last sync disappears too.
    """
    _require_google_imports()
    service = get_youtube_service(conn)

    uploads_id = _channel_uploads_playlist_id(channel_id)
    base_items = (
        _get_all_playlist_items(service, uploads_id) if uploads_id
        else _search_videos_page(service, channel_id, "", 50, None)["items"]
    )
    by_id = {item["video_id"]: item for item in base_items if item.get("video_id")}
    video_ids = list(by_id.keys())
    details = _video_details_by_id(service, video_ids)

    # Carry forward what this app recorded about each video's playlists since the
    # last sync. Reads once, outside the row loop; everything not re-listed below
    # keeps its old membership untouched.
    known_membership: dict[str, list[str]] = {}
    for row in conn.execute("SELECT video_id, playlist_ids FROM youtube_channel_videos"):
        try:
            known_membership[row["video_id"]] = json.loads(row["playlist_ids"] or "[]")
        except (TypeError, ValueError):
            known_membership[row["video_id"]] = []

    now = _now_iso()
    rows = []
    for vid in video_ids:
        base = by_id[vid]
        extra = details.get(vid, {})
        rows.append((
            vid,
            base.get("title", "") or "",
            extra.get("description", ""),
            json.dumps(extra.get("tags") or []),
            extra.get("privacy_status", "private"),
            extra.get("category_id"),
            base.get("thumbnail"),
            extra.get("duration_sec"),
            extra.get("view_count"),
            base.get("published_at"),
            json.dumps(known_membership.get(vid, [])),
            now,
        ))

    conn.execute("DELETE FROM youtube_channel_videos")
    conn.executemany(
        "INSERT INTO youtube_channel_videos "
        "(video_id, title, description, tags, privacy_status, category_id, thumbnail, "
        " duration_sec, view_count, published_at, playlist_ids, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return {"synced": len(rows), "playlists_scanned": 0, "synced_at": now}


def channel_videos_sync_status(conn: sqlite3.Connection) -> dict:
    """Last sync time and cached row count, for the "Đồng bộ lần cuối" banner."""
    row = conn.execute(
        "SELECT COUNT(*) AS cnt, MAX(synced_at) AS synced_at FROM youtube_channel_videos"
    ).fetchone()
    return {"count": row["cnt"] if row else 0, "synced_at": row["synced_at"] if row else None}


def _row_to_channel_video(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["tags"] = json.loads(data.get("tags") or "[]")
    data["playlist_ids"] = json.loads(data.get("playlist_ids") or "[]")
    return data


def list_cached_channel_videos(
    conn: sqlite3.Connection,
    *,
    search: str = "",
    privacy_status: str = "",
    has_playlist: str = "",
    playlist_id: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "published_at",
    order: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """Search/filter/sort/paginate the cached channel-videos snapshot.

    Returns {items, total, page, page_size}. `has_playlist` is 'yes'/'no'; when
    `playlist_id` is set it also implies membership in that specific playlist.
    """
    sort_col = _CHANNEL_VIDEO_SORT_COLUMNS.get(sort, "published_at")
    sort_dir = "ASC" if order == "asc" else "DESC"

    where: list[str] = []
    params: list = []
    if search:
        where.append("(title LIKE ? OR description LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    if privacy_status:
        where.append("privacy_status = ?")
        params.append(privacy_status)
    if date_from:
        where.append("published_at >= ?")
        params.append(date_from)
    if date_to:
        where.append("published_at <= ?")
        params.append(date_to)
    if playlist_id:
        where.append("EXISTS (SELECT 1 FROM json_each(playlist_ids) WHERE value = ?)")
        params.append(playlist_id)
    elif has_playlist in ("yes", "no"):
        exists = "EXISTS (SELECT 1 FROM json_each(playlist_ids))"
        where.append(exists if has_playlist == "yes" else f"NOT {exists}")

    where_sql = f" WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(
        f"SELECT COUNT(*) FROM youtube_channel_videos{where_sql}", params
    ).fetchone()[0]

    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    offset = (page - 1) * page_size
    rows = conn.execute(
        f"SELECT * FROM youtube_channel_videos{where_sql} "
        f"ORDER BY {sort_col} {sort_dir} LIMIT ? OFFSET ?",
        [*params, page_size, offset],
    ).fetchall()
    return {
        "items": [_row_to_channel_video(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_cached_channel_videos(conn: sqlite3.Connection, video_ids: list[str]) -> list[dict]:
    """Cached rows for exactly these video ids, in no particular order."""
    if not video_ids:
        return []
    placeholders = ",".join("?" * len(video_ids))
    rows = conn.execute(
        f"SELECT * FROM youtube_channel_videos WHERE video_id IN ({placeholders})", video_ids
    ).fetchall()
    return [_row_to_channel_video(r) for r in rows]


def delete_channel_videos(conn: sqlite3.Connection, video_ids: list[str]) -> dict:
    """Permanently delete videos from YouTube (videos.delete) and drop them from the cache.

    Irreversible on YouTube's side - the route this backs requires the caller to
    confirm explicitly before calling it.
    """
    result = _new_batch_result()
    if not video_ids:
        return result
    _require_google_imports()
    service = get_youtube_service(conn)
    for video_id in video_ids:
        try:
            _execute(service.videos().delete(id=video_id))
            conn.execute("DELETE FROM youtube_channel_videos WHERE video_id=?", (video_id,))
            _batch_add(result, video_id, "succeeded")
        except Exception as exc:
            _batch_add(result, video_id, "failed", str(exc)[:500])
    conn.commit()
    return result


def bulk_update_channel_videos(conn: sqlite3.Connection, updates: list[dict]) -> dict:
    """Apply per-video metadata edits to live YouTube videos.

    Each item in `updates` is {video_id, title?, description?, tags?, privacy_status?} -
    omitted fields are left alone. videos.update replaces the whole snippet/status part
    it is given, so the current one is read back first - for the whole batch in one
    videos.list per 50 videos rather than one per video. Successful edits also refresh
    the matching cache row so the table reflects the change without a full re-sync.
    """
    result = _new_batch_result()
    if not updates:
        return result
    _require_google_imports()
    service = get_youtube_service(conn)
    existing = _videos_by_id(service, [u["video_id"] for u in updates], "snippet,status")
    for item in updates:
        video_id = item["video_id"]
        try:
            found = existing.get(video_id)
            if not found:
                _batch_add(result, video_id, "failed", "video not found")
                continue
            snippet, status = found["snippet"], found["status"]
            if item.get("title") is not None:
                snippet["title"] = item["title"][:100]
            if item.get("description") is not None:
                snippet["description"] = item["description"][:5000]
            if item.get("tags") is not None:
                snippet["tags"] = item["tags"]
            if item.get("privacy_status"):
                status["privacyStatus"] = item["privacy_status"]
            if settings.youtube_declare_altered_content:
                # videos.list không trả containsSyntheticMedia, nên cái status đọc ngược về
                # ở trên luôn thiếu nó; ghi đè nguyên khối status mà không set lại là xoá
                # mất phần khai báo "Sử dụng AI" chỉ vì sửa cái tiêu đề.
                status["containsSyntheticMedia"] = True
            body = {"id": video_id, "snippet": snippet, "status": status}
            _execute(service.videos().update(part="snippet,status", body=body))
            conn.execute(
                "UPDATE youtube_channel_videos SET title=?, description=?, tags=?, privacy_status=? "
                "WHERE video_id=?",
                (snippet.get("title", ""), snippet.get("description", ""),
                 json.dumps(snippet.get("tags") or []), status.get("privacyStatus", "private"), video_id),
            )
            _batch_add(result, video_id, "succeeded")
        except Exception as exc:
            _batch_add(result, video_id, "failed", str(exc)[:500])
    conn.commit()
    return result


def _cache_add_video_to_playlists(conn: sqlite3.Connection | None, video_id: str, playlist_id: str) -> None:
    """Best-effort: keep the channel-videos cache's playlist membership in sync.

    A no-op (never raises) when there is no real connection - unit tests exercise the
    batch playlist functions with conn=None since they monkeypatch get_youtube_service
    to ignore it - or when the cache write itself fails for any reason; this mirror is
    a convenience for the Videos-kênh tab, never load-bearing for the playlist op itself.
    """
    if conn is None:
        return
    try:
        row = conn.execute("SELECT playlist_ids FROM youtube_channel_videos WHERE video_id=?",
                           (video_id,)).fetchone()
        if row is None:
            return
        ids = set(json.loads(row["playlist_ids"] or "[]"))
        ids.add(playlist_id)
        conn.execute("UPDATE youtube_channel_videos SET playlist_ids=? WHERE video_id=?",
                     (json.dumps(sorted(ids)), video_id))
    except sqlite3.Error:
        logger.debug("channel-videos cache update failed (add)", exc_info=True)


def _cache_remove_video_from_playlist(conn: sqlite3.Connection | None, video_id: str, playlist_id: str) -> None:
    """Best-effort mirror of `_cache_add_video_to_playlists`; see its docstring."""
    if conn is None:
        return
    try:
        row = conn.execute("SELECT playlist_ids FROM youtube_channel_videos WHERE video_id=?",
                           (video_id,)).fetchone()
        if row is None:
            return
        ids = set(json.loads(row["playlist_ids"] or "[]"))
        ids.discard(playlist_id)
        conn.execute("UPDATE youtube_channel_videos SET playlist_ids=? WHERE video_id=?",
                     (json.dumps(sorted(ids)), video_id))
    except sqlite3.Error:
        logger.debug("channel-videos cache update failed (remove)", exc_info=True)


def _commit_if_real(conn: sqlite3.Connection | None) -> None:
    """conn.commit(), skipped for the conn=None stand-in the playlist unit tests use."""
    if conn is not None:
        conn.commit()


def delete_playlist(conn: sqlite3.Connection, playlist_id: str) -> dict:
    """Delete a playlist from the channel. Does not delete the videos it contained."""
    _require_google_imports()
    service = get_youtube_service(conn)
    _execute(service.playlists().delete(id=playlist_id))
    rows = conn.execute("SELECT video_id FROM youtube_channel_videos").fetchall()
    for row in rows:
        _cache_remove_video_from_playlist(conn, row["video_id"], playlist_id)
    conn.commit()
    return {"deleted": playlist_id}


def bulk_update_playlists(
    conn: sqlite3.Connection,
    playlist_ids: list[str],
    *,
    privacy_status: str | None = None,
    title_prefix: str = "",
    title_suffix: str = "",
    description_template: str | None = None,
) -> dict:
    """Apply the same privacy/title-affix/description change to several playlists.

    `title_prefix`/`title_suffix` are prepended/appended to each playlist's current
    title (not a replacement); `description_template` replaces the description
    outright when given. Every other field the playlist has is preserved, which is
    why each playlist is read back first - batched 50 per playlists.list rather than
    one call (and one rebuilt service object) per playlist.
    """
    result = _new_batch_result()
    if not playlist_ids:
        return result
    _require_google_imports()
    service = get_youtube_service(conn)
    existing = _playlists_by_id(service, playlist_ids)
    for playlist_id in playlist_ids:
        try:
            playlist = existing.get(playlist_id)
            if playlist is None:
                _batch_add(result, playlist_id, "failed", "playlist not found")
                continue
            snippet = playlist.get("snippet") or {}
            status = playlist.get("status") or {}
            new_title = f"{title_prefix}{snippet.get('title', '')}{title_suffix}"[:150]
            body = {
                "id": playlist_id,
                "snippet": {
                    "title": new_title,
                    "description": description_template if description_template is not None
                    else snippet.get("description", ""),
                },
                "status": {"privacyStatus": privacy_status or status.get("privacyStatus", "private")},
            }
            if status.get("podcastStatus"):
                body["status"]["podcastStatus"] = status["podcastStatus"]
            _execute(service.playlists().update(part="snippet,status", body=body))
            _batch_add(result, playlist_id, "succeeded")
        except Exception as exc:
            _batch_add(result, playlist_id, "failed", str(exc)[:500])
    return result


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


def apply_book_podcast(conn: sqlite3.Connection, book_id: int | None, playlist_id: str) -> dict | None:
    """Best-effort podcast sync after an episode lands in the book's playlist.

    Never raises: the video is already public at this point, so a rejected
    podcast flag must not turn a finished publish into a failure. Does nothing
    unless the book actually asked for a podcast.
    """
    if not book_id or not playlist_id:
        return None
    try:
        from app import image_overlay, repository
        from app.production_defaults import get_effective_youtube_config

        book = repository.get_book(conn, book_id)
        if book is None:
            return None
        podcast = get_effective_youtube_config(conn, book).get("podcast") or {}
        if not podcast.get("enabled"):
            return None
        cover_path = None
        if podcast.get("upload_cover", True):
            from app.production_defaults import get_effective_branding_config
            patches = repository.list_patches(conn, book_id)
            branding = get_effective_branding_config(conn, book)
            cover_path = image_overlay.ensure_podcast_cover(book, image_overlay.pick_cover_patch(patches), branding=branding)
        return sync_playlist_podcast(conn, book_id, playlist_id, enabled=True, cover_path=cover_path)
    except Exception:
        logger.warning(
            "podcast sync failed for book %s / playlist %s", book_id, playlist_id, exc_info=True
        )
        return None


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
                    apply_book_podcast(conn, _resolve_book_id(conn, upload_id), playlist_id)
                    # Tự động sắp xếp playlist theo episode sau khi thêm video mới
                    try:
                        book_id_for_sort = _resolve_book_id(conn, upload_id)
                        if book_id_for_sort:
                            from app import repository as _repo
                            _book_for_sort = _repo.get_book(conn, book_id_for_sort)
                            if _book_for_sort is not None:
                                from app.production_defaults import get_effective_youtube_config as _get_yt
                                _cfg = _get_yt(conn, _book_for_sort)
                                should_sort = bool(_cfg.get("auto_sort_episode")) or _cfg.get("playlist_sort_mode") == "episode"
                                if should_sort:
                                    sort_playlist(conn, playlist_id, direction="asc", mode="episode")
                    except Exception:
                        logger.warning("auto sort episode failed for playlist %s", playlist_id, exc_info=True)
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
