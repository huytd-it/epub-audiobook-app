"""Google Drive integration: OAuth2 flow, folder/file operations.

Scoped narrowly to the Colab/Kaggle chunk export round trip (see
app/drive_export.py and app/routes/drive.py) - this is not a general-purpose
Drive browser. Structure mirrors app/youtube.py, which already proved this
OAuth pattern in this codebase.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import Flow
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload
    _GOOGLE_IMPORTS_OK = True
except ModuleNotFoundError:
    _GOOGLE_IMPORTS_OK = False

from app.config import settings

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_API_SERVICE_NAME = "drive"
_API_VERSION = "v3"
_ROOT_FOLDER_NAME = "EPUB Audiobook Exports"


def _require_google_imports() -> None:
    if not _GOOGLE_IMPORTS_OK:
        raise ModuleNotFoundError(
            "Missing Google API packages. Install: pip install google-auth google-auth-oauthlib google-api-python-client"
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_configured() -> bool:
    return bool(settings.google_drive_client_id and settings.google_drive_client_secret)


def get_creds_from_db(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT * FROM google_drive_credentials ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def save_credentials(
    conn: sqlite3.Connection,
    access_token: str,
    refresh_token: str,
    token_expiry: str,
    account_email: str | None = None,
) -> None:
    """Upsert Google Drive credentials (single-row table)."""
    existing = conn.execute("SELECT id FROM google_drive_credentials LIMIT 1").fetchone()
    now = _now_iso()
    if existing:
        conn.execute(
            """UPDATE google_drive_credentials
               SET access_token=?, refresh_token=?, token_expiry=?, account_email=?, updated_at=?
               WHERE id=?""",
            (access_token, refresh_token, token_expiry, account_email, now, existing["id"]),
        )
    else:
        conn.execute(
            """INSERT INTO google_drive_credentials
               (access_token, refresh_token, token_expiry, account_email, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (access_token, refresh_token, token_expiry, account_email, now, now),
        )
    conn.commit()


def delete_credentials(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM google_drive_credentials")
    conn.commit()


def _build_credentials(row: dict) -> Credentials:
    _require_google_imports()
    return Credentials(
        token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_drive_client_id,
        client_secret=settings.google_drive_client_secret,
        scopes=_SCOPES,
    )


def _refresh_if_needed(conn: sqlite3.Connection, creds_row: dict) -> Credentials:
    _require_google_imports()
    creds = _build_credentials(creds_row)
    if creds.expired or not creds.valid:
        try:
            creds.refresh(Request())
        except Exception:
            logger.exception("Google Drive token refresh failed")
            raise
        expiry_str = creds.expiry.isoformat() if creds.expiry else creds_row["token_expiry"]
        save_credentials(
            conn,
            access_token=creds.token or "",
            refresh_token=creds.refresh_token or creds_row["refresh_token"],
            token_expiry=expiry_str,
            account_email=creds_row.get("account_email"),
        )
    return creds


def get_drive_service(conn: sqlite3.Connection):
    """Return an authorized Drive API service object."""
    _require_google_imports()
    creds_row = get_creds_from_db(conn)
    if creds_row is None:
        raise ValueError("Google Drive not connected. Please connect first.")
    creds = _refresh_if_needed(conn, creds_row)
    return build(_API_SERVICE_NAME, _API_VERSION, credentials=creds)


def get_authorization_url(redirect_uri: str) -> str:
    _require_google_imports()
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.google_drive_client_id,
                "client_secret": settings.google_drive_client_secret,
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
    """Exchange authorization code for tokens. Returns {access_token, refresh_token,
    token_expiry, account_email}."""
    _require_google_imports()
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": settings.google_drive_client_id,
                "client_secret": settings.google_drive_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        },
        scopes=_SCOPES,
    )
    flow.redirect_uri = redirect_uri
    flow.fetch_token(code=code)
    creds = flow.credentials

    account_email = ""
    try:
        oauth2 = build("oauth2", "v2", credentials=creds)
        userinfo = oauth2.userinfo().get().execute()
        account_email = userinfo.get("email", "")
    except Exception:
        logger.warning("could not fetch Google account email (non-fatal)", exc_info=True)

    expiry_str = creds.expiry.isoformat() if creds.expiry else ""
    return {
        "access_token": creds.token or "",
        "refresh_token": creds.refresh_token or "",
        "token_expiry": expiry_str,
        "account_email": account_email,
    }


# ---------------------------------------------------------------------------
# Folder / file operations (drive.file scope: only touches files this app made)
# ---------------------------------------------------------------------------


def create_folder(service, name: str, parent_id: str | None = None) -> dict:
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id, webViewLink").execute()
    link = folder.get("webViewLink") or f"https://drive.google.com/drive/folders/{folder['id']}"
    return {"id": folder["id"], "link": link}


def get_or_create_root_folder(service) -> str:
    """Find (or create) the single 'EPUB Audiobook Exports' folder that every patch
    export's subfolder lives under."""
    resp = service.files().list(
        q=(
            f"name = '{_ROOT_FOLDER_NAME}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        ),
        fields="files(id)",
    ).execute()
    files = resp.get("files", [])
    if files:
        return files[0]["id"]
    return create_folder(service, _ROOT_FOLDER_NAME)["id"]


def upload_file(service, folder_id: str, local_path: str, mime_type: str | None = None) -> str:
    metadata = {"name": Path(local_path).name, "parents": [folder_id]}
    media = MediaFileUpload(local_path, mimetype=mime_type, resumable=False)
    file = service.files().create(body=metadata, media_body=media, fields="id").execute()
    return file["id"]


def list_files(service, folder_id: str) -> list[dict]:
    """Return every non-trashed file directly inside folder_id: [{id, name, modifiedTime}]."""
    files: list[dict] = []
    page_token = None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            fields="nextPageToken, files(id, name, modifiedTime, size)",
            pageToken=page_token,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def download_file(service, file_id: str, dest_path: str) -> None:
    request = service.files().get_media(fileId=file_id)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
