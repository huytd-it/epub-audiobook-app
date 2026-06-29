"""YouTube OAuth and upload routes."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import youtube
from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_conn(request: Request):
    return request.app.state.conn


@router.get("/youtube", response_class=HTMLResponse)
def youtube_page(request: Request):
    conn = _get_conn(request)
    creds = youtube.get_creds_from_db(conn)
    connected = creds is not None and bool(creds.get("channel_name"))
    uploads = youtube.list_uploads(conn, limit=30)
    return templates.TemplateResponse(request, "youtube.html", {
        "request": request,
        "connected": connected,
        "channel_name": creds.get("channel_name") if creds else None,
        "uploads": uploads,
        "configured": youtube.is_configured(),
        "auto_upload": settings.youtube_auto_upload,
    })


@router.get("/youtube/connect")
def youtube_connect(request: Request):
    if not youtube.is_configured():
        raise HTTPException(status_code=400, detail="YouTube not configured. Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET.")
    redirect_uri = str(request.base_url) + "youtube/callback"
    url = youtube.get_authorization_url(redirect_uri)
    return RedirectResponse(url=url)


@router.get("/youtube/callback")
def youtube_callback(request: Request, code: str = "", error: str = ""):
    conn = _get_conn(request)
    if error:
        return RedirectResponse(url=f"/youtube?error={error}")
    if not code:
        return RedirectResponse(url="/youtube?error=no_code")

    redirect_uri = str(request.base_url) + "youtube/callback"
    try:
        result = youtube.exchange_code(code, redirect_uri)
    except Exception as exc:
        logger.exception("YouTube OAuth callback failed")
        return RedirectResponse(url=f"/youtube?error={str(exc)}")

    youtube.save_credentials(
        conn,
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        token_expiry=result["token_expiry"],
        channel_id=result["channel_id"],
        channel_name=result["channel_name"],
    )
    return RedirectResponse(url="/youtube?connected=1")


@router.post("/youtube/disconnect")
def youtube_disconnect(request: Request):
    conn = _get_conn(request)
    youtube.delete_credentials(conn)
    return JSONResponse({"status": "disconnected"})


@router.post("/youtube/upload")
async def youtube_upload_manual(
    request: Request,
    video_path: str = Form(...),
    title: str = Form(...),
    description: str = Form(default=""),
    tags: str = Form(default=""),
    privacy_status: str = Form(default="private"),
):
    conn = _get_conn(request)
    if not youtube.is_configured():
        raise HTTPException(status_code=400, detail="YouTube not configured")
    if not youtube.get_creds_from_db(conn):
        raise HTTPException(status_code=400, detail="YouTube not connected")

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    result = youtube.upload_video(
        conn,
        video_path=video_path,
        title=title,
        description=description,
        tags=tag_list,
        privacy_status=privacy_status,
    )
    return JSONResponse(result)


@router.post("/youtube/upload-file")
async def youtube_upload_file(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    description: str = Form(default=""),
    tags: str = Form(default=""),
    privacy_status: str = Form(default="private"),
):
    """Upload a video file directly (for standalone videos not yet on disk)."""
    conn = _get_conn(request)
    if not youtube.is_configured():
        raise HTTPException(status_code=400, detail="YouTube not configured")
    if not youtube.get_creds_from_db(conn):
        raise HTTPException(status_code=400, detail="YouTube not connected")

    # Save to tmp
    from app.routes.video import _TMP_DIR
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    import uuid
    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    tmp_path = _TMP_DIR / f"yt_upload_{uuid.uuid4().hex[:8]}{ext}"
    with open(tmp_path, "wb") as out:
        import shutil
        shutil.copyfileobj(file.file, out)

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    result = youtube.upload_video(
        conn,
        video_path=str(tmp_path),
        title=title,
        description=description,
        tags=tag_list,
        privacy_status=privacy_status,
    )
    return JSONResponse(result)


@router.get("/youtube/uploads")
def youtube_uploads_list(request: Request):
    conn = _get_conn(request)
    uploads = youtube.list_uploads(conn)
    return JSONResponse({"uploads": uploads})
