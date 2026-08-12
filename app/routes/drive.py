"""Google Drive OAuth routes (Colab/Kaggle export round trip settings page)."""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import sys
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import drive_export, google_drive, repository
from app.config import settings
from app.deps import locked_conn

logger = logging.getLogger(__name__)

router = APIRouter()

# rclone's own Google Drive OAuth client (fixes the "shared client_id will stop working
# during 2026" warning). Stored per-DB in app_state and passed as --drive-client-id /
# --drive-client-secret on every rclone copy. See docs/google-drive-accounts.md.
_RCLONE_CLIENT_ID_KEY = "rclone.drive_client_id"
_RCLONE_CLIENT_SECRET_KEY = "rclone.drive_client_secret"


def _run_rclone_copy(folder_path: str, remote: str, client_id: str, client_secret: str) -> dict:
    """Push a local staging folder up to its Google Drive remote via rclone.

    ALWAYS `copy`, never `sync`: each export is a uniquely-named/timestamped folder, so
    nothing needs deleting on the remote - and `sync` would wipe anything on the account
    that isn't in the local folder. See the safety rule in docs/google-drive-accounts.md.
    """
    if not os.path.isdir(folder_path):
        return {"status": "error", "remote": remote, "output": f"Folder không tồn tại: {folder_path}"}
    cmd = [settings.rclone_path, "copy", folder_path, remote, "-v"]
    if client_id:
        cmd += ["--drive-client-id", client_id]
    if client_secret:
        cmd += ["--drive-client-secret", client_secret]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    except FileNotFoundError:
        return {"status": "error", "remote": remote,
                "output": f"Không tìm thấy rclone (RCLONE_PATH='{settings.rclone_path}'). Cài rclone hoặc đặt đường dẫn."}
    except subprocess.TimeoutExpired:
        return {"status": "error", "remote": remote, "output": "rclone copy quá thời gian (30 phút)."}
    output = (proc.stdout or "") + (proc.stderr or "")
    output = output.strip()[-4000:]
    return {"status": "ok" if proc.returncode == 0 else "error", "remote": remote, "output": output}


@router.get("/drive/pick-folder")
def pick_drive_folder():
    """Open a native folder picker on the machine running the app."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as exc:
        raise HTTPException(status_code=501, detail="Native folder picker is unavailable") from exc

    selected: list[str] = []
    error: list[Exception] = []

    def choose() -> None:
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            selected.append(filedialog.askdirectory(title="Chọn thư mục Google Drive Desktop"))
            root.destroy()
        except Exception as exc:
            error.append(exc)

    thread = threading.Thread(target=choose)
    thread.start()
    thread.join()
    if error:
        raise HTTPException(status_code=500, detail=str(error[0]))
    return {"folder_path": selected[0] if selected else ""}


@router.get("/drive/open-folder")
def open_folder(path: str):
    """Open a folder in the system file explorer."""
    if not path or not os.path.isdir(path):
        raise HTTPException(status_code=400, detail="Thư mục không tồn tại")
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"status": "ok"}




def _target_fields(name: str, account_email: str, folder_path: str, rclone_remote: str) -> tuple[str, str, str, str | None]:
    name, account_email, rclone_remote = name.strip(), account_email.strip(), rclone_remote.strip()
    if not name or not account_email:
        raise ValueError("Name and account email are required")
    if rclone_remote and ":" not in rclone_remote:
        raise ValueError("rclone remote must look like 'remote:path', e.g. codex5:EPUB Audiobook Exports")
    return name, account_email, str(drive_export.validate_sync_folder(folder_path)), (rclone_remote or None)


@router.post("/drive/targets")
def create_sync_target(request: Request, name: str = Form(...), account_email: str = Form(...), folder_path: str = Form(...), rclone_remote: str = Form("")):
    try:
        fields = _target_fields(name, account_email, folder_path, rclone_remote)
        with locked_conn(request) as conn:
            repository.create_drive_sync_target(conn, *fields)
    except ValueError as exc:
        return RedirectResponse(url=f"/drive?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(url="/drive", status_code=303)


@router.post("/drive/targets/{target_id}/edit")
def update_sync_target(request: Request, target_id: int, name: str = Form(...), account_email: str = Form(...), folder_path: str = Form(...), rclone_remote: str = Form("")):
    try:
        fields = _target_fields(name, account_email, folder_path, rclone_remote)
        with locked_conn(request) as conn:
            if not repository.update_drive_sync_target(conn, target_id, *fields):
                raise HTTPException(status_code=404, detail="Sync target not found")
    except ValueError as exc:
        return RedirectResponse(url=f"/drive?error={quote(str(exc))}", status_code=303)
    return RedirectResponse(url="/drive", status_code=303)


@router.post("/drive/targets/{target_id}/delete")
def delete_sync_target(request: Request, target_id: int):
    with locked_conn(request) as conn:
        if not repository.delete_drive_sync_target(conn, target_id):
            raise HTTPException(status_code=404, detail="Sync target not found")
    return RedirectResponse(url="/drive", status_code=303)


@router.post("/drive/rclone-config")
def save_rclone_config(request: Request, client_id: str = Form(""), client_secret: str = Form("")):
    """Store rclone's own Google Drive OAuth client_id/secret (fixes the shared-client
    deprecation warning). Applied via --drive-client-id/--drive-client-secret on sync."""
    with locked_conn(request) as conn:
        repository.set_app_state(conn, _RCLONE_CLIENT_ID_KEY, client_id.strip())
        repository.set_app_state(conn, _RCLONE_CLIENT_SECRET_KEY, client_secret.strip())
        conn.commit()
    return RedirectResponse(url="/drive#rclone", status_code=303)


@router.post("/drive/targets/{target_id}/sync")
def sync_target(request: Request, target_id: int):
    """rclone copy one target's staging folder up to its Google Drive remote."""
    with locked_conn(request) as conn:
        target = repository.get_drive_sync_target(conn, target_id)
        if target is None:
            raise HTTPException(status_code=404, detail="Sync target not found")
        if not target.get("rclone_remote"):
            raise HTTPException(status_code=400, detail="Target này không có rclone remote (Drive Desktop tự đồng bộ).")
        cid = repository.get_app_state(conn, _RCLONE_CLIENT_ID_KEY) or ""
        cs = repository.get_app_state(conn, _RCLONE_CLIENT_SECRET_KEY) or ""
    result = _run_rclone_copy(target["folder_path"], target["rclone_remote"], cid, cs)
    result["name"] = target["name"]
    return JSONResponse(result)


@router.post("/drive/sync-all")
def sync_all(request: Request):
    """rclone copy every target that has an rclone remote configured, sequentially."""
    with locked_conn(request) as conn:
        targets = [t for t in repository.list_drive_sync_targets(conn) if t.get("rclone_remote")]
        cid = repository.get_app_state(conn, _RCLONE_CLIENT_ID_KEY) or ""
        cs = repository.get_app_state(conn, _RCLONE_CLIENT_SECRET_KEY) or ""
    results = []
    for t in targets:
        result = _run_rclone_copy(t["folder_path"], t["rclone_remote"], cid, cs)
        result["name"] = t["name"]
        results.append(result)
    return JSONResponse({"count": len(results), "results": results})


@router.post("/drive/targets/bulk-sync")
def bulk_sync_targets(request: Request, target_ids: list[int]):
    """rclone copy selected targets that have rclone remotes."""
    with locked_conn(request) as conn:
        targets = [t for t in repository.list_drive_sync_targets(conn) if t.get("rclone_remote") and t["id"] in target_ids]
        cid = repository.get_app_state(conn, _RCLONE_CLIENT_ID_KEY) or ""
        cs = repository.get_app_state(conn, _RCLONE_CLIENT_SECRET_KEY) or ""
    results = []
    for t in targets:
        result = _run_rclone_copy(t["folder_path"], t["rclone_remote"], cid, cs)
        result["name"] = t["name"]
        results.append(result)
    synced = sum(1 for r in results if r["status"] == "ok")
    return JSONResponse({"synced": synced, "total": len(results), "results": results})


@router.post("/drive/targets/bulk-delete")
def bulk_delete_targets(request: Request, target_ids: list[int]):
    """Delete multiple sync targets. Exported files are kept."""
    with locked_conn(request) as conn:
        for tid in target_ids:
            repository.delete_drive_sync_target(conn, tid)
    return JSONResponse({"deleted": len(target_ids)})


@router.get("/drive/connect")
def drive_connect(request: Request, oauth_client_id: int | None = None):
    with locked_conn(request) as conn:
        if oauth_client_id:
            client = google_drive.get_client(conn, oauth_client_id)
            if client is None:
                raise HTTPException(status_code=404, detail="OAuth client not found")
            cid, cs = client["client_id"], client["client_secret"]
        else:
            if not google_drive.is_configured(conn):
                raise HTTPException(
                    status_code=400,
                    detail="Google Drive not configured. Set up an OAuth client at /drive first.",
                )
            cid, cs = None, None
    redirect_uri = str(request.base_url) + "drive/callback"
    state = str(oauth_client_id) if oauth_client_id else ""
    try:
        url = google_drive.get_authorization_url(redirect_uri, client_id=cid, client_secret=cs, state=state)
    except Exception as exc:
        logger.exception("drive_connect failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return RedirectResponse(url=url)


@router.get("/drive/callback")
def drive_callback(request: Request, code: str = "", error: str = "", state: str = ""):
    if error:
        return RedirectResponse(url=f"/drive?error={error}")
    if not code:
        return RedirectResponse(url="/drive?error=no_code")

    oauth_client_id = int(state) if state and state.isdigit() else None
    redirect_uri = str(request.base_url) + "drive/callback"

    if oauth_client_id:
        with locked_conn(request) as conn:
            client = google_drive.get_client(conn, oauth_client_id)
            cid, cs = (client["client_id"], client["client_secret"]) if client else (None, None)
    else:
        cid, cs = None, None

    try:
        result = google_drive.exchange_code(code, redirect_uri, client_id=cid, client_secret=cs)
    except Exception as exc:
        logger.exception("Google Drive OAuth callback failed")
        return RedirectResponse(url=f"/drive?error={str(exc)}")

    try:
        with locked_conn(request) as conn:
            google_drive.save_credentials(
                conn,
                access_token=result["access_token"],
                refresh_token=result["refresh_token"],
                token_expiry=result["token_expiry"],
                account_email=result["account_email"],
                oauth_client_id=oauth_client_id,
            )
    except Exception as exc:
        logger.exception("Failed to save Google Drive credentials")
        return RedirectResponse(url=f"/drive?error={str(exc)}")
    return RedirectResponse(url="/drive?connected=1")


@router.get("/drive/kaggle-credentials")
def drive_kaggle_credentials(request: Request, account_id: int | None = None):
    """Credentials JSON for the batch notebook's Kaggle Drive mode: the user pastes
    this into a private Kaggle secret named GDRIVE_CREDS so the notebook can download
    the exported batch from Drive and upload synthesized audio back (same drive.file
    scope and OAuth client as the app itself). Each account has its own credentials -
    the secret must match the account that holds the export."""
    with locked_conn(request) as conn:
        if not google_drive.is_configured(conn):
            raise HTTPException(status_code=400, detail="Google Drive not configured")
        if account_id is not None:
            creds = google_drive.get_account(conn, account_id)
            if creds is None:
                raise HTTPException(status_code=404, detail="Google Drive account not found")
        else:
            accounts = google_drive.list_accounts(conn)
            if not accounts:
                raise HTTPException(status_code=400, detail="Google Drive not connected. Connect it first.")
            if len(accounts) > 1:
                raise HTTPException(
                    status_code=400,
                    detail="Multiple Google Drive accounts connected; pass ?account_id=...",
                )
            creds = accounts[0]
        if not creds.get("refresh_token"):
            raise HTTPException(status_code=400, detail="This account has no refresh token. Reconnect it.")
        cid = settings.google_drive_client_id
        cs = settings.google_drive_client_secret
        if creds.get("oauth_client_id"):
            client = google_drive.get_client(conn, creds["oauth_client_id"])
            if client:
                cid, cs = client["client_id"], client["client_secret"]
        return JSONResponse({
            "client_id": cid,
            "client_secret": cs,
            "refresh_token": creds["refresh_token"],
        })


@router.post("/drive/disconnect")
def drive_disconnect(request: Request, account_id: int = Form(...)):
    with locked_conn(request) as conn:
        google_drive.delete_credentials(conn, account_id)
    return RedirectResponse(url="/drive", status_code=303)


@router.post("/drive/clients")
def drive_create_client(request: Request, name: str = Form(...), client_id: str = Form(...), client_secret: str = Form("")):
    with locked_conn(request) as conn:
        google_drive.create_client(conn, name, client_id, client_secret)
    return RedirectResponse(url="/drive#clients", status_code=303)


@router.post("/drive/clients/{client_id}/edit")
def drive_update_client(request: Request, client_id: int, name: str = Form(...), cid: str = Form(...), client_secret: str = Form("")):
    with locked_conn(request) as conn:
        google_drive.update_client(conn, client_id, name, cid, client_secret)
    return RedirectResponse(url="/drive#clients", status_code=303)


@router.post("/drive/clients/{client_id}/delete")
def drive_delete_client(request: Request, client_id: int):
    with locked_conn(request) as conn:
        try:
            google_drive.delete_client(conn, client_id)
        except ValueError as exc:
            return RedirectResponse(url=f"/drive?error={exc}", status_code=303)
    return RedirectResponse(url="/drive#clients", status_code=303)


@router.post("/drive/exports/{export_id}/delete")
def drive_delete_export(request: Request, export_id: int):
    with locked_conn(request) as conn:
        repository.delete_patch_export(conn, export_id)
    return RedirectResponse(url="/drive", status_code=303)
