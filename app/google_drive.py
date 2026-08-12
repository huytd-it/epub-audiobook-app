"""Google Drive integration: OAuth2 flow, folder/file operations.

Scoped narrowly to the Colab/Kaggle chunk export round trip (see
app/drive_export.py and app/routes/drive.py) - this is not a general-purpose
Drive browser. Structure mirrors app/youtube.py, which already proved this
OAuth pattern in this codebase.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

# Reusing the same OAuth client as YouTube (see .env.example) means Google's token
# response often includes every scope ever granted to that client for this account (e.g.
# the youtube.upload scopes from a prior connect), not just the drive.file scope this flow
# requested. oauthlib treats that as an error by default ("Scope has changed") unless this
# is set - this is the standard, documented way to allow it.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

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


def is_configured(conn: sqlite3.Connection | None = None) -> bool:
    if settings.google_drive_client_id and settings.google_drive_client_secret:
        return True
    if conn:
        return bool(list_clients(conn))
    return False


# ---------------------------------------------------------------------------
# Client CRUD
# ---------------------------------------------------------------------------


def list_clients(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM drive_oauth_client ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_client(conn: sqlite3.Connection, client_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM drive_oauth_client WHERE id = ?", (client_id,)).fetchone()
    return dict(row) if row else None


def create_client(conn: sqlite3.Connection, name: str, client_id: str, client_secret: str) -> int:
    now = _now_iso()
    cur = conn.execute(
        """INSERT INTO drive_oauth_client (name, client_id, client_secret, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (name, client_id, client_secret, now, now),
    )
    conn.commit()
    return cur.lastrowid


def update_client(conn: sqlite3.Connection, row_id: int, name: str, client_id: str, client_secret: str) -> None:
    if client_secret:
        conn.execute(
            """UPDATE drive_oauth_client SET name=?, client_id=?, client_secret=?, updated_at=?
               WHERE id=?""",
            (name, client_id, client_secret, _now_iso(), row_id),
        )
    else:
        conn.execute(
            """UPDATE drive_oauth_client SET name=?, client_id=?, updated_at=?
               WHERE id=?""",
            (name, client_id, _now_iso(), row_id),
        )
    conn.commit()


def delete_client(conn: sqlite3.Connection, row_id: int) -> None:
    cnt = count_accounts_for_client(conn, row_id)
    if cnt > 0:
        raise ValueError(
            f"Cannot delete client with {cnt} connected account(s). "
            "Disconnect those accounts first."
        )
    conn.execute("DELETE FROM drive_oauth_client WHERE id = ?", (row_id,))
    conn.commit()


def count_accounts_for_client(conn: sqlite3.Connection, client_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM google_drive_credentials WHERE oauth_client_id = ?",
        (client_id,),
    ).fetchone()
    return row["n"]


def list_accounts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM google_drive_credentials ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_account(conn: sqlite3.Connection, account_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM google_drive_credentials WHERE id = ?", (account_id,)
    ).fetchone()
    return dict(row) if row else None


def any_account_connected(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM google_drive_credentials LIMIT 1").fetchone() is not None


def save_credentials(
    conn: sqlite3.Connection,
    access_token: str,
    refresh_token: str,
    token_expiry: str,
    account_email: str | None = None,
    oauth_client_id: int | None = None,
) -> int:
    """Upsert Google Drive credentials keyed by account_email; returns the account id.

    Reconnecting an already-known email updates that row in place (same id), so
    patch_export.drive_account_id references keep working - this is also the recovery
    path when an account's token got revoked. An unknown or empty email inserts a new
    row (a duplicate empty-email row is harmless and user-deletable on /drive).
    """
    now = _now_iso()
    if account_email:
        existing = conn.execute(
            "SELECT id FROM google_drive_credentials WHERE account_email = ?",
            (account_email,),
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE google_drive_credentials
                   SET access_token=?, refresh_token=?, token_expiry=?, updated_at=?, oauth_client_id=?
                   WHERE id=?""",
                (access_token, refresh_token, token_expiry, now, oauth_client_id, existing["id"]),
            )
            conn.commit()
            return existing["id"]
    cur = conn.execute(
        """INSERT INTO google_drive_credentials
           (access_token, refresh_token, token_expiry, account_email, oauth_client_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (access_token, refresh_token, token_expiry, account_email, oauth_client_id, now, now),
    )
    conn.commit()
    return cur.lastrowid


def _update_account_tokens(
    conn: sqlite3.Connection,
    account_id: int,
    access_token: str,
    refresh_token: str,
    token_expiry: str,
) -> None:
    """Token-refresh persistence path: plain UPDATE by id, never the email upsert."""
    conn.execute(
        """UPDATE google_drive_credentials
           SET access_token=?, refresh_token=?, token_expiry=?, updated_at=?
           WHERE id=?""",
        (access_token, refresh_token, token_expiry, _now_iso(), account_id),
    )
    conn.commit()


def delete_credentials(conn: sqlite3.Connection, account_id: int) -> None:
    conn.execute("DELETE FROM google_drive_credentials WHERE id = ?", (account_id,))
    conn.commit()


def _resolve_client_creds(conn: sqlite3.Connection, creds_row: dict) -> tuple[str, str]:
    oauth_client_id = creds_row.get("oauth_client_id")
    if oauth_client_id:
        client = get_client(conn, oauth_client_id)
        if client:
            return client["client_id"], client["client_secret"]
    return settings.google_drive_client_id, settings.google_drive_client_secret


def kaggle_credentials(conn: sqlite3.Connection, account_id: int) -> dict | None:
    """The GDRIVE_CREDS payload for one account: what the batch notebook needs to talk
    to Drive on its own (same OAuth client and drive.file scope as the app). Returns
    None when the account is unknown or has no refresh token - without one the notebook
    cannot mint an access token, so there is nothing usable to hand it."""
    row = get_account(conn, account_id)
    if row is None or not row.get("refresh_token"):
        return None
    client_id, client_secret = _resolve_client_creds(conn, row)
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": row["refresh_token"],
    }


def _build_credentials(row: dict, client_id: str | None = None, client_secret: str | None = None) -> Credentials:
    _require_google_imports()
    return Credentials(
        token=row["access_token"],
        refresh_token=row["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=client_id or settings.google_drive_client_id,
        client_secret=client_secret or settings.google_drive_client_secret,
        scopes=_SCOPES,
    )


def _refresh_if_needed(conn: sqlite3.Connection, creds_row: dict) -> Credentials:
    _require_google_imports()
    client_id, client_secret = _resolve_client_creds(conn, creds_row)
    creds = _build_credentials(creds_row, client_id, client_secret)
    if creds.expired or not creds.valid:
        try:
            creds.refresh(Request())
        except Exception:
            logger.exception("Google Drive token refresh failed")
            raise
        expiry_str = creds.expiry.isoformat() if creds.expiry else creds_row["token_expiry"]
        _update_account_tokens(
            conn,
            creds_row["id"],
            access_token=creds.token or "",
            refresh_token=creds.refresh_token or creds_row["refresh_token"],
            token_expiry=expiry_str,
        )
    return creds


def get_drive_service(conn: sqlite3.Connection, account_id: int):
    """Return an authorized Drive API service object for one connected account."""
    _require_google_imports()
    creds_row = get_account(conn, account_id)
    if creds_row is None:
        raise ValueError(
            f"Google Drive account not found (id={account_id}). It may have been disconnected."
        )
    creds = _refresh_if_needed(conn, creds_row)
    return build(_API_SERVICE_NAME, _API_VERSION, credentials=creds)


_RR_STATE_KEY = "drive.rr_last_account_id"


def pick_export_account(conn: sqlite3.Connection) -> dict:
    """Round-robin over connected accounts: each export/batch goes wholly to one
    account (one notebook run mounts one Drive), and consecutive exports rotate so
    Colab/Kaggle GPU quota is spread across accounts.

    The rotation pointer lives in app_state; a pointer at a since-deleted account
    self-heals (the next-higher id, or the first account on wrap, is picked).
    """
    from app import repository  # local import: repository does not import google_drive

    accounts = list_accounts(conn)  # ORDER BY id
    if not accounts:
        raise ValueError("Google Drive not connected. Connect an account at /drive first.")
    last = repository.get_app_state(conn, _RR_STATE_KEY)
    last_id = int(last) if last and last.isdigit() else -1
    chosen = next((a for a in accounts if a["id"] > last_id), accounts[0])
    repository.set_app_state(conn, _RR_STATE_KEY, str(chosen["id"]))
    return chosen


def resolve_import_account(conn: sqlite3.Connection, drive_account_id: int | None) -> dict:
    """Resolve which account to import an export from. With the drive.file scope only
    the account that created the export folder can even see it, so this must be the
    exact account recorded on the patch_export row."""
    if drive_account_id is not None:
        row = get_account(conn, drive_account_id)
        if row is None:
            raise ValueError(
                "The Drive account used for this export has been disconnected. "
                "Reconnect that Google account at /drive (same email restores access), "
                "or import the synthesized files via local upload instead."
            )
        return row
    # Legacy export (pre multi-account): the DB had exactly one account back then, and
    # that original row keeps the lowest id - so the oldest account is the right guess.
    # A wrong guess just finds no files (drive.file scope), it doesn't crash.
    row = conn.execute(
        "SELECT * FROM google_drive_credentials ORDER BY id ASC LIMIT 1"
    ).fetchone()
    if row is None:
        raise ValueError("Google Drive not connected. Connect an account at /drive first.")
    return dict(row)


def get_authorization_url(redirect_uri: str, client_id: str | None = None, client_secret: str | None = None, state: str = "") -> str:
    _require_google_imports()
    cid = client_id or settings.google_drive_client_id
    cs = client_secret or settings.google_drive_client_secret
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": cid,
                "client_secret": cs,
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
        state=state or None,
    )
    return url


def exchange_code(code: str, redirect_uri: str, client_id: str | None = None, client_secret: str | None = None) -> dict:
    """Exchange authorization code for tokens. Returns {access_token, refresh_token,
    token_expiry, account_email}."""
    _require_google_imports()
    cid = client_id or settings.google_drive_client_id
    cs = client_secret or settings.google_drive_client_secret
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": cid,
                "client_secret": cs,
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


def upload_directory(service, parent_id: str, local_dir: str) -> dict[str, dict]:
    """Recursively upload the contents of ``local_dir`` into the Drive folder
    ``parent_id``, creating subfolders as needed.

    Returns {relative_posix_path: {"id", "link"}} for every created subfolder, so
    callers can record e.g. the Drive folder id of "patches/patch_004" (the batch
    export route stores one patch_export row per patch pointing at its subfolder).
    """
    folder_map: dict[str, dict] = {}
    root = Path(local_dir)

    def _upload(folder_id: str, directory: Path, rel: str) -> None:
        for entry in sorted(directory.iterdir()):
            if entry.is_dir():
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                child = create_folder(service, entry.name, parent_id=folder_id)
                folder_map[child_rel] = child
                _upload(child["id"], entry, child_rel)
            else:
                upload_file(service, folder_id, str(entry))

    _upload(parent_id, root, "")
    return folder_map


def find_subfolder(service, parent_id: str, name: str) -> str | None:
    """Return the id of a folder named ``name`` directly inside ``parent_id``, or None.

    The Colab/Kaggle notebook writes synthesized chunk_NNN.wav files into an "output"
    subfolder of the exported folder (see the batch TTS notebook) - this locates
    it so the import routes can look there instead of the export folder's top level.
    """
    resp = service.files().list(
        q=(
            f"'{parent_id}' in parents and name = '{name}' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        ),
        fields="files(id)",
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


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
