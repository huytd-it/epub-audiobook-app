"""YouTube OAuth, upload, and playlist management routes."""
from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from app import db as app_db
from app import youtube
from app.config import settings
from app.deps import locked_conn

try:
    from google.auth.exceptions import RefreshError
    from googleapiclient.errors import HttpError
except ModuleNotFoundError:
    RefreshError = ()
    HttpError = ()

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _enqueue(request: Request, video_path: str, title: str, description: str, tags: str, privacy_status: str, playlist_id: str = "") -> dict:
    """Queue a video for the upload worker and return immediately.

    The upload itself must not run here: these handlers hold the shared db_lock via
    locked_conn, so a multi-minute network upload inside one would block every other
    request (including the progress poll this feature depends on).
    """
    from app.jobqueue import store
    if getattr(request.app.state, "job_queue", None) is None:
        raise HTTPException(status_code=503, detail="Upload worker is unavailable")

    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    with locked_conn(request) as conn:
        if not youtube.get_creds_from_db(conn):
            raise HTTPException(status_code=400, detail="YouTube not connected")
        upload_id = youtube.enqueue_upload(
            conn,
            video_path=video_path,
            title=title,
            description=description,
            tags=tag_list,
            privacy_status=privacy_status,
            playlist_id=playlist_id,
        )
        store.enqueue(conn, "youtube_upload", payload={"upload_id": upload_id},
                      dedupe_key=f"youtube_upload:upload={upload_id}")
    return {"upload_id": upload_id, "status": "pending"}


@contextmanager
def _youtube_api_conn(request: Request):
    """Yield a connection for YouTube network calls - never the shared one.

    Every call that touches the YouTube Data API (listing playlists/items/videos, adds,
    removals, copies, moves, reorders, sorts) can take seconds over the network, so it
    must not run on the shared connection: that connection is guarded by the shared
    db_lock and a slow call there would freeze every other request (see
    tests/test_db_lock_contention.py). A real file DB gets a throwaway connection; a
    :memory: test DB has no other file to open, so it falls back to the shared
    connection, which is safe because tests run single-threaded.
    """
    with locked_conn(request) as conn:
        database = conn.execute("PRAGMA database_list").fetchone()[2]
    if not database or database == ":memory:":
        yield request.app.state.conn
        return
    api_conn = app_db.connect(database)
    try:
        yield api_conn
    finally:
        api_conn.close()


_REORDER_PAGE_SIZE = 50


class _PlaylistAddBody(BaseModel):
    video_ids: list[str] = Field(min_length=1)


class _PlaylistRemoveBody(BaseModel):
    item_ids: list[str] = Field(min_length=1)


class _PlaylistCopyBody(BaseModel):
    dest_playlist_id: str = Field(min_length=1)
    item_ids: list[str] | None = None

    @field_validator("item_ids")
    @classmethod
    def _no_empty_list(cls, v):
        if v is not None and len(v) == 0:
            raise ValueError("item_ids must not be empty when provided")
        return v


class _PlaylistMoveBody(BaseModel):
    dest_playlist_id: str = Field(min_length=1)
    item_ids: list[str] = Field(min_length=1)


class _PlaylistItemPositionBody(BaseModel):
    # UI sends 1-based positions; routes convert them to 0-based for the service.
    position: int = Field(ge=1)


class _PlaylistReorderBody(BaseModel):
    # Maps playlistItem.id -> absolute 0-based position for one page of a playlist.
    positions: dict[str, int] = Field(min_length=1)


class _PlaylistSortBody(BaseModel):
    direction: str = Field(default="asc")

    @field_validator("direction")
    @classmethod
    def _valid_direction(cls, v):
        if v not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")
        return v


def _youtube_error(status: int, code: str, message: str, *, auth: bool = False) -> None:
    """Raise a normalized JSON error, optionally with auth CTA metadata."""
    detail = {"code": code, "message": message}
    if auth:
        detail["auth"] = {
            "required": True,
            "connect_url": "/youtube/connect",
            "cta": "Connect your YouTube account",
        }
    raise HTTPException(status_code=status, detail=detail)


def _validate_page_size(page_size: int) -> None:
    if page_size < 1 or page_size > 50:
        _youtube_error(400, "validation", "page_size must be between 1 and 50")


def _require_playlist_id(playlist_id: str) -> None:
    if not playlist_id or not playlist_id.strip():
        _youtube_error(400, "validation", "playlist_id is required")


def _require_distinct(source: str, dest: str) -> None:
    if source == dest:
        _youtube_error(400, "validation", "source and destination playlists must be different")


def _require_connected(request: Request) -> None:
    if not youtube.is_configured():
        _youtube_error(
            400,
            "not_configured",
            "YouTube not configured. Set YOUTUBE_CLIENT_ID and YOUTUBE_CLIENT_SECRET.",
        )
    with locked_conn(request) as conn:
        creds = youtube.get_creds_from_db(conn)
    if not creds:
        _youtube_error(401, "auth_required",
                       "YouTube not connected. Connect your account first.", auth=True)


def _classify_youtube_error(exc: Exception) -> dict:
    """Map a google/YouTube exception to a normalized (status, code, message, auth) dict."""
    status = getattr(getattr(exc, "resp", None), "status", None)
    if isinstance(exc, RefreshError) or (isinstance(exc, HttpError) and status == 401):
        return {"status": 401, "code": "auth_required",
                "message": "YouTube authentication expired. Reconnect your account.", "auth": True}
    if isinstance(exc, HttpError) and status == 404:
        return {"status": 404, "code": "not_found", "message": str(exc)}
    if isinstance(exc, HttpError) and status == 403:
        reason = ""
        try:
            body = json.loads(exc.content) if getattr(exc, "content", None) else {}
            reason = body.get("error", {}).get("errors", [{}])[0].get("reason", "")
        except (ValueError, TypeError, IndexError, AttributeError):
            pass
        if reason in ("quotaExceeded", "rateLimitExceeded", "dailyLimitExceeded", "userRateLimitExceeded"):
            return {"status": 429, "code": "quota_exceeded",
                    "message": f"YouTube API quota exceeded ({reason}). Try again later."}
        if reason in ("authError", "forbidden", "insufficientPermissions", "youtubeSignupRequired"):
            return {"status": 403, "code": "auth_required",
                    "message": f"YouTube permission error ({reason}). Reconnect your account.", "auth": True}
        return {"status": 403, "code": "forbidden", "message": str(exc)}
    if isinstance(exc, ValueError) and "not connected" in str(exc).lower():
        return {"status": 401, "code": "auth_required",
                "message": "YouTube not connected. Connect your account first.", "auth": True}
    return {"status": 500, "code": "internal", "message": str(exc)}


def _call_youtube(request: Request, func, *args, **kwargs):
    """Run an app.youtube service function on a throwaway connection.

    The service functions take a sqlite3.Connection as their first argument; the routes
    here never call them on the shared connection under the db_lock. Failures are
    normalized to JSON errors (never redirects) with auth CTA metadata when a reconnect
    is needed.
    """
    with _youtube_api_conn(request) as conn:
        try:
            return func(conn, *args, **kwargs)
        except Exception as exc:
            logger.debug("YouTube API call failed: %s", exc)
            error = _classify_youtube_error(exc)
            _youtube_error(error["status"], error["code"], error["message"],
                           auth=error.get("auth", False))


def _mark_partial(result):
    """Flag partial failures from bulk operations in the JSON response."""
    if not isinstance(result, dict):
        return result
    if result.get("failed") or result.get("errors"):
        result = dict(result)
        result["partial"] = True
    else:
        result.setdefault("partial", False)
    return result


@router.get("/youtube", response_class=HTMLResponse)
def youtube_page(request: Request):
    with locked_conn(request) as conn:
        creds = youtube.get_creds_from_db(conn)
        connected = creds is not None and bool(creds.get("channel_name"))
        uploads = youtube.list_uploads(conn, limit=30)
    if connected:
        try:
            with _youtube_api_conn(request) as api_conn:
                playlists = youtube.list_playlists(api_conn)
        except Exception:
            playlists = []
    else:
        playlists = []
    return templates.TemplateResponse(request, "youtube.html", {
        "request": request,
        "connected": connected,
        "channel_name": creds.get("channel_name") if creds else None,
        "uploads": uploads,
        "configured": youtube.is_configured(),
        "auto_upload": settings.youtube_auto_upload,
        "playlists": playlists,
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

    try:
        with locked_conn(request) as conn:
            youtube.save_credentials(
                conn,
                access_token=result["access_token"],
                refresh_token=result["refresh_token"],
                token_expiry=result["token_expiry"],
                channel_id=result["channel_id"],
                channel_name=result["channel_name"],
            )
    except Exception as exc:
        logger.exception("Failed to save YouTube credentials")
        return RedirectResponse(url=f"/youtube?error={str(exc)}")
    return RedirectResponse(url="/youtube?connected=1")


@router.post("/youtube/disconnect")
def youtube_disconnect(request: Request):
    with locked_conn(request) as conn:
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
    playlist_id: str = Form(default=""),
):
    if not youtube.is_configured():
        raise HTTPException(status_code=400, detail="YouTube not configured")

    return JSONResponse(_enqueue(request, video_path, title, description, tags, privacy_status, playlist_id))


@router.post("/youtube/upload-file")
async def youtube_upload_file(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    description: str = Form(default=""),
    tags: str = Form(default=""),
    privacy_status: str = Form(default="private"),
    playlist_id: str = Form(default=""),
):
    """Upload a video file directly (for standalone videos not yet on disk)."""
    if not youtube.is_configured():
        raise HTTPException(status_code=400, detail="YouTube not configured")

    # Save to tmp
    from app.routes.video import _TMP_DIR
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    import uuid
    ext = Path(file.filename or "video.mp4").suffix or ".mp4"
    tmp_path = _TMP_DIR / f"yt_upload_{uuid.uuid4().hex[:8]}{ext}"

    def _save():
        import shutil
        with open(tmp_path, "wb") as out:
            shutil.copyfileobj(file.file, out)

    # Off the event loop: a large file otherwise stalls every concurrent request.
    await asyncio.to_thread(_save)

    return JSONResponse(_enqueue(request, str(tmp_path), title, description, tags, privacy_status, playlist_id))


@router.get("/youtube/uploads")
def youtube_uploads_list(request: Request):
    with locked_conn(request) as conn:
        uploads = youtube.list_uploads(conn)
    return JSONResponse({"uploads": uploads})


@router.get("/youtube/kaggle-credentials")
def youtube_kaggle_credentials(request: Request):
    """Credentials JSON for use as YOUTUBE_CREDS Kaggle/Colab Secret so the
    batch notebook can upload rendered MP4s directly to YouTube."""
    if not youtube.is_configured():
        raise HTTPException(status_code=400, detail="YouTube chưa được cấu hình")
    with locked_conn(request) as conn:
        creds = youtube.get_creds_from_db(conn)
    if creds is None or not creds.get("refresh_token"):
        raise HTTPException(status_code=400, detail="YouTube chưa được kết nối. Kết nối trước tại /youtube.")
    return JSONResponse({
        "client_id": settings.youtube_client_id,
        "client_secret": settings.youtube_client_secret,
        "refresh_token": creds["refresh_token"],
    })


@router.delete("/youtube/uploads/{upload_id}")
def youtube_delete_upload(request: Request, upload_id: int):
    """Delete a single upload history record."""
    with locked_conn(request) as conn:
        if not youtube.delete_upload(conn, upload_id):
            raise HTTPException(status_code=404, detail="Upload not found")
    return JSONResponse({"deleted": 1})


@router.post("/youtube/uploads/bulk-delete")
def youtube_bulk_delete_uploads(request: Request, ids: list[int]):
    """Delete multiple upload history records."""
    with locked_conn(request) as conn:
        deleted = youtube.delete_uploads(conn, ids)
    return JSONResponse({"deleted": deleted})


@router.post("/youtube/uploads/bulk-retry")
def youtube_bulk_retry_uploads(request: Request, ids: list[int]):
    """Reset failed uploads to pending status for retry."""
    with locked_conn(request) as conn:
        retried = youtube.reset_upload_status(conn, ids)
    return JSONResponse({"retried": retried})


# ---------------------------------------------------------------------------
# Playlist management JSON APIs
#
# Every endpoint calls the expected app.youtube service functions
# (list_playlists, list_playlist_items, list_channel_videos, bulk_add_to_playlist,
# bulk_remove_from_playlist, get_all_playlist_items, copy_playlist_items,
# move_playlist_items, update_playlist_item_position, reorder_playlist_page,
# sort_playlist_preview, sort_playlist) on a throwaway connection via _call_youtube,
# so no network work ever runs on the shared connection under the db_lock. Errors are
# normalized to JSON (never redirects) with auth CTA metadata when a reconnect is needed.
#
# Response shape conventions:
#   * List endpoints return {items, next_page_token, prev_page_token, total, count};
#     playlist items are the service's normalized shape
#     {playlist_item_id, playlist_id, video_id, title, thumbnail, position, published_at}.
#   * Batch operations return the service's standardized batch result
#     {requested, succeeded, skipped, failed, items: [{key, status, message}]}
#     plus a `partial` flag when any item failed.
#   * Positions are converted from 1-based (UI) to 0-based (service) only here.
# ---------------------------------------------------------------------------


@router.get("/youtube/api/playlists")
def api_list_playlists(request: Request, max_results: int = 50):
    """List the authenticated channel's playlists."""
    _require_connected(request)
    _validate_page_size(max_results)
    items = _call_youtube(request, youtube.list_playlists, max_results=max_results)
    return JSONResponse({"items": items, "count": len(items),
                         "next_page_token": None, "prev_page_token": None})


@router.get("/youtube/api/playlists/{playlist_id}/items")
def api_list_playlist_items(request: Request, playlist_id: str, max_results: int = 50,
                            page_token: str | None = None):
    """List one page of items (videos) in a playlist."""
    _require_connected(request)
    _require_playlist_id(playlist_id)
    _validate_page_size(max_results)
    page = _call_youtube(request, youtube.list_playlist_items, playlist_id,
                         max_results=max_results, page_token=page_token)
    return JSONResponse({
        "playlist_id": playlist_id,
        "items": page["items"],
        "next_page_token": page.get("next_page_token"),
        "prev_page_token": page.get("prev_page_token"),
        "total": page.get("total", len(page["items"])),
        "count": len(page["items"]),
    })


def _channel_id_from_creds(request: Request) -> str:
    """Read the stored channel id from the DB credentials row."""
    with locked_conn(request) as conn:
        creds = youtube.get_creds_from_db(conn)
    channel_id = (creds or {}).get("channel_id") or ""
    if not channel_id:
        _youtube_error(400, "validation",
                       "channel_id is missing from stored credentials; reconnect your account",
                       auth=True)
    return channel_id


@router.get("/youtube/api/channel/videos")
def api_list_channel_videos(request: Request, max_results: int = 50,
                            page_token: str | None = None, q: str | None = None):
    """List the authenticated channel's uploaded videos (channel id from DB creds)."""
    _require_connected(request)
    _validate_page_size(max_results)
    channel_id = _channel_id_from_creds(request)
    page = _call_youtube(request, youtube.list_channel_videos, channel_id,
                         max_results=max_results, page_token=page_token, title_query=q)
    return JSONResponse({
        "items": page["items"],
        "next_page_token": page.get("next_page_token"),
        "prev_page_token": page.get("prev_page_token"),
        "total": page.get("total", len(page["items"])),
        "count": len(page["items"]),
    })


def _add_playlist_videos(request: Request, playlist_id: str, video_ids: list[str]):
    """Shared add handler used by the canonical and alias routes."""
    _require_connected(request)
    _require_playlist_id(playlist_id)
    result = _call_youtube(request, youtube.bulk_add_to_playlist, playlist_id, video_ids)
    return JSONResponse(_mark_partial(result))


@router.post("/youtube/api/playlists/{playlist_id}/items")
def api_add_playlist_items(request: Request, playlist_id: str, body: _PlaylistAddBody):
    """Add one or more videos to a playlist by YouTube video ID."""
    return _add_playlist_videos(request, playlist_id, body.video_ids)


@router.post("/youtube/api/playlists/{playlist_id}/videos")
def api_add_playlist_videos(request: Request, playlist_id: str, body: _PlaylistAddBody):
    """Alias of POST /playlists/{playlist_id}/items."""
    return _add_playlist_videos(request, playlist_id, body.video_ids)


@router.delete("/youtube/api/playlists/{playlist_id}/items")
def api_remove_playlist_items(request: Request, playlist_id: str, body: _PlaylistRemoveBody):
    """Remove several playlist items at once (by playlistItem.id)."""
    _require_connected(request)
    _require_playlist_id(playlist_id)
    result = _call_youtube(request, youtube.bulk_remove_from_playlist, playlist_id,
                           playlist_item_ids=body.item_ids)
    return JSONResponse(_mark_partial(result))


@router.delete("/youtube/api/playlist-items/{item_id}")
def api_remove_playlist_item(request: Request, item_id: str):
    """Remove a single playlist item (alias; playlist id is not needed for item deletion)."""
    _require_connected(request)
    result = _call_youtube(request, youtube.bulk_remove_from_playlist, "",
                           playlist_item_ids=[item_id])
    return JSONResponse(_mark_partial(result))


def _map_playlist_item_ids(request: Request, playlist_id: str, item_ids: list[str] | None) -> list[str] | None:
    """Resolve playlistItem ids to video ids by fetching the source playlist server-side.

    The service copy/move functions key on video ids, while the UI selection is keyed on
    playlistItem ids; this route is the only place that resolves between the two.
    """
    if item_ids is None:
        return None
    items = _call_youtube(request, youtube.get_all_playlist_items, playlist_id)
    by_id = {i["playlist_item_id"]: i["video_id"] for i in items}
    video_ids: list[str] = []
    missing: list[str] = []
    for item_id in item_ids:
        video_id = by_id.get(item_id)
        if video_id:
            video_ids.append(video_id)
        else:
            missing.append(item_id)
    if missing:
        _youtube_error(400, "validation",
                       f"playlist item(s) not found in source playlist: {', '.join(missing)}")
    return video_ids


@router.post("/youtube/api/playlists/{source_playlist_id}/copy")
def api_copy_playlist_items(request: Request, source_playlist_id: str, body: _PlaylistCopyBody):
    """Copy selected playlist items from one playlist to another."""
    _require_connected(request)
    _require_playlist_id(source_playlist_id)
    _require_playlist_id(body.dest_playlist_id)
    _require_distinct(source_playlist_id, body.dest_playlist_id)
    video_ids = _map_playlist_item_ids(request, source_playlist_id, body.item_ids)
    result = _call_youtube(request, youtube.copy_playlist_items,
                           source_playlist_id, body.dest_playlist_id, video_ids)
    return JSONResponse(_mark_partial(result))


@router.post("/youtube/api/playlists/{source_playlist_id}/move")
def api_move_playlist_items(request: Request, source_playlist_id: str, body: _PlaylistMoveBody):
    """Move selected playlist items to another playlist (add then remove from source)."""
    _require_connected(request)
    _require_playlist_id(source_playlist_id)
    _require_playlist_id(body.dest_playlist_id)
    _require_distinct(source_playlist_id, body.dest_playlist_id)
    video_ids = _map_playlist_item_ids(request, source_playlist_id, body.item_ids)
    result = _call_youtube(request, youtube.move_playlist_items,
                           source_playlist_id, body.dest_playlist_id, video_ids)
    return JSONResponse(_mark_partial(result))


@router.post("/youtube/api/playlists/{playlist_id}/items/{item_id}/position")
def api_update_item_position(request: Request, playlist_id: str, item_id: str,
                             body: _PlaylistItemPositionBody):
    """Move one playlist item to a new position. UI positions are 1-based; the service
    works 0-based, so the route subtracts one before calling it."""
    _require_connected(request)
    _require_playlist_id(playlist_id)
    backend_position = body.position - 1
    item = _call_youtube(request, youtube.update_playlist_item_position,
                         playlist_id, item_id, backend_position)
    return JSONResponse(item)


@router.post("/youtube/api/playlists/{playlist_id}/reorder")
def api_reorder_playlist_page(request: Request, playlist_id: str, body: _PlaylistReorderBody):
    """Apply a drag-and-drop order for one page of a playlist.

    `positions` maps each playlistItem.id to its absolute 0-based position. The route
    converts it to an ordered video-id list plus the page index for the service.
    """
    _require_connected(request)
    _require_playlist_id(playlist_id)
    items = _call_youtube(request, youtube.get_all_playlist_items, playlist_id)
    by_id = {i["playlist_item_id"]: i for i in items}
    ordered = sorted(body.positions.items(), key=lambda kv: kv[1])
    order: list[str] = []
    page_positions: list[int] = []
    missing: list[str] = []
    for item_id, _pos in ordered:
        item = by_id.get(item_id)
        if item is None:
            missing.append(item_id)
            continue
        order.append(item["video_id"])
        page_positions.append(item["position"])
    if missing:
        _youtube_error(400, "validation", f"playlist item(s) not found: {', '.join(missing)}")
    page_index = min(page_positions) // _REORDER_PAGE_SIZE if page_positions else 0
    result = _call_youtube(request, youtube.reorder_playlist_page, playlist_id,
                           page_index, order, page_size=_REORDER_PAGE_SIZE)
    return JSONResponse(_mark_partial(result))


@router.post("/youtube/api/playlists/{playlist_id}/sort/preview")
def api_preview_playlist_sort(request: Request, playlist_id: str, body: _PlaylistSortBody):
    """Preview a whole-playlist natural-title sort without applying it."""
    _require_connected(request)
    _require_playlist_id(playlist_id)
    preview = _call_youtube(request, youtube.sort_playlist_preview, playlist_id, body.direction)
    return JSONResponse(preview)


@router.post("/youtube/api/playlists/{playlist_id}/sort/apply")
def api_apply_playlist_sort(request: Request, playlist_id: str, body: _PlaylistSortBody):
    """Apply a whole-playlist natural-title sort."""
    _require_connected(request)
    _require_playlist_id(playlist_id)
    result = _call_youtube(request, youtube.sort_playlist, playlist_id, body.direction)
    return JSONResponse(_mark_partial(result))
