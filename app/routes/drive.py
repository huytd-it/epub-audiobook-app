"""Google Drive OAuth routes (Colab/Kaggle export round trip settings page)."""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import google_drive, repository

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_conn(request: Request):
    return request.app.state.conn


@router.get("/drive", response_class=HTMLResponse)
def drive_page(request: Request):
    conn = _get_conn(request)
    creds = google_drive.get_creds_from_db(conn)
    connected = creds is not None
    exports = repository.list_all_patch_exports(conn, limit=30)
    return templates.TemplateResponse(request, "drive.html", {
        "request": request,
        "connected": connected,
        "account_email": creds.get("account_email") if creds else None,
        "exports": exports,
        "configured": google_drive.is_configured(),
    })


@router.get("/drive/connect")
def drive_connect(request: Request):
    if not google_drive.is_configured():
        raise HTTPException(
            status_code=400,
            detail="Google Drive not configured. Set GOOGLE_DRIVE_CLIENT_ID and GOOGLE_DRIVE_CLIENT_SECRET.",
        )
    redirect_uri = str(request.base_url) + "drive/callback"
    url = google_drive.get_authorization_url(redirect_uri)
    return RedirectResponse(url=url)


@router.get("/drive/callback")
def drive_callback(request: Request, code: str = "", error: str = ""):
    conn = _get_conn(request)
    if error:
        return RedirectResponse(url=f"/drive?error={error}")
    if not code:
        return RedirectResponse(url="/drive?error=no_code")

    redirect_uri = str(request.base_url) + "drive/callback"
    try:
        result = google_drive.exchange_code(code, redirect_uri)
    except Exception as exc:
        logger.exception("Google Drive OAuth callback failed")
        return RedirectResponse(url=f"/drive?error={str(exc)}")

    google_drive.save_credentials(
        conn,
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        token_expiry=result["token_expiry"],
        account_email=result["account_email"],
    )
    return RedirectResponse(url="/drive?connected=1")


@router.post("/drive/disconnect")
def drive_disconnect(request: Request):
    conn = _get_conn(request)
    google_drive.delete_credentials(conn)
    return JSONResponse({"status": "disconnected"})
