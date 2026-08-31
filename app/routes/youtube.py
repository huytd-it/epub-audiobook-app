"""YouTube OAuth, upload, and playlist management routes."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, field_validator

from app import db as app_db
from app import youtube
from app import youtube_io
from app import youtube_metadata
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


def _enqueue(request: Request, video_path: str, title: str, description: str, tags: str, privacy_status: str,
            playlist_id: str = "", not_for_kids: bool = True, ai_labels_enabled: bool = False) -> dict:
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
            not_for_kids=not_for_kids,
            ai_labels_enabled=ai_labels_enabled,
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


class _PlaylistReorderAllBody(BaseModel):
    # Every playlistItem.id of the playlist, in the desired order. Used by the
    # "save all" button, which batches a whole session of local reordering.
    item_ids: list[str] = Field(min_length=1)


class _PlaylistSortBody(BaseModel):
    direction: str = Field(default="asc")
    # 'natural' keeps the plain numeric-chunk title order; 'episode' groups by the
    # series name in front of the episode marker ("... - Tập 3 - ...") and orders by
    # its number, so the chapter range and tags that follow it never drive the sort.
    mode: str = Field(default="natural")

    @field_validator("direction")
    @classmethod
    def _valid_direction(cls, v):
        if v not in ("asc", "desc"):
            raise ValueError("direction must be 'asc' or 'desc'")
        return v

    @field_validator("mode")
    @classmethod
    def _valid_mode(cls, v):
        if v not in youtube.SORT_MODES:
            raise ValueError(f"mode must be one of {', '.join(youtube.SORT_MODES)}")
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
    not_for_kids: bool = Form(default=True),
    ai_labels_enabled: bool = Form(default=False),
):
    if not youtube.is_configured():
        raise HTTPException(status_code=400, detail="YouTube not configured")

    return JSONResponse(_enqueue(request, video_path, title, description, tags, privacy_status, playlist_id,
                                 not_for_kids, ai_labels_enabled))


@router.post("/youtube/upload-file")
async def youtube_upload_file(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    description: str = Form(default=""),
    tags: str = Form(default=""),
    privacy_status: str = Form(default="private"),
    playlist_id: str = Form(default=""),
    not_for_kids: bool = Form(default=True),
    ai_labels_enabled: bool = Form(default=False),
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

    return JSONResponse(_enqueue(request, str(tmp_path), title, description, tags, privacy_status, playlist_id,
                                 not_for_kids, ai_labels_enabled))


@router.get("/youtube/uploads")
def youtube_uploads_list(
    request: Request,
    search: str = "",
    status: str = "",
    privacy_status: str = "",
    has_playlist: str = "",
    not_for_kids: str = "",
    ai_labels_enabled: str = "",
    date_from: str = "",
    date_to: str = "",
    sort: str = "created_at",
    order: str = "desc",
):
    with locked_conn(request) as conn:
        uploads = youtube.list_uploads(
            conn,
            search=search,
            status=status,
            privacy_status=privacy_status,
            has_playlist=has_playlist,
            not_for_kids=not_for_kids,
            ai_labels_enabled=ai_labels_enabled,
            date_from=date_from,
            date_to=date_to,
            sort=sort,
            order=order,
        )
    return JSONResponse({"uploads": uploads})


class _UploadUpdateBody(BaseModel):
    title: str | None = None
    description: str | None = None
    tags: list[str] | None = None
    privacy_status: str | None = None
    not_for_kids: bool | None = None
    ai_labels_enabled: bool | None = None


@router.patch("/youtube/uploads/{upload_id}")
def youtube_update_upload(request: Request, upload_id: int, body: _UploadUpdateBody):
    """Edit an upload-queue row's own fields (title/description/tags/privacy/flags).

    This never touches YouTube itself - once `status='done'` the row edit is local
    only, matching the existing import/export behavior (see youtube_io.apply_import).
    """
    if body.title is not None and (not body.title.strip() or len(body.title) > youtube_metadata.YOUTUBE_TITLE_LIMIT):
        raise HTTPException(status_code=400,
                            detail=f"title phải có nội dung và tối đa {youtube_metadata.YOUTUBE_TITLE_LIMIT} ký tự")
    if body.description is not None and len(body.description) > youtube_metadata.YOUTUBE_DESCRIPTION_LIMIT:
        raise HTTPException(status_code=400,
                            detail=f"description tối đa {youtube_metadata.YOUTUBE_DESCRIPTION_LIMIT} ký tự")
    if body.privacy_status is not None and body.privacy_status not in youtube_io.PRIVACY_VALUES:
        raise HTTPException(status_code=400,
                            detail=f"privacy_status phải là một trong: {', '.join(youtube_io.PRIVACY_VALUES)}")
    with locked_conn(request) as conn:
        updated = youtube.update_upload_fields(conn, upload_id, **body.model_dump(exclude_unset=True))
    if updated is None:
        raise HTTPException(status_code=404, detail="Upload not found")
    return JSONResponse(updated)


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
# Upload queue import / export
#
# Export dumps the editable columns (title, description, tags, privacy, playlist,
# video path) as JSON or CSV; import reads the same shape back so the queue can be
# bulk-edited in a spreadsheet. See app/youtube_io.py for the round-trip contract.
# ---------------------------------------------------------------------------


def _parse_id_list(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    try:
        return [int(part) for part in raw.split(",") if part.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids phải là danh sách số nguyên, phân cách dấu phẩy")


def _queue_imported_upload(request: Request):
    """Factory for youtube_io.apply_import's `create` hook.

    Mirrors _enqueue: the row is inserted and a youtube_upload job is queued on the
    same already-locked connection, so an imported row behaves exactly like one added
    through the upload form.
    """
    from app.jobqueue import store

    def create(conn, payload: dict) -> int:
        upload_id = youtube.enqueue_upload(
            conn,
            video_path=payload["video_path"],
            title=payload["title"],
            description=payload["description"],
            tags=payload["tags"],
            privacy_status=payload["privacy_status"],
            playlist_id=payload["playlist_id"],
            not_for_kids=payload.get("not_for_kids", True),
            ai_labels_enabled=payload.get("ai_labels_enabled", False),
        )
        store.enqueue(conn, "youtube_upload", payload={"upload_id": upload_id},
                      dedupe_key=f"youtube_upload:upload={upload_id}")
        return upload_id

    return create


@router.get("/youtube/uploads/export")
def youtube_export_uploads(
    request: Request,
    format: str = "json",
    ids: str | None = None,
    statuses: str | None = None,
):
    """Download the upload queue as an editable JSON or CSV sheet."""
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format phải là 'json' hoặc 'csv'")
    id_list = _parse_id_list(ids)
    status_list = [s.strip() for s in statuses.split(",") if s.strip()] if statuses else None

    with locked_conn(request) as conn:
        records = youtube_io.export_records(conn, ids=id_list, statuses=status_list)

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if format == "csv":
        body, media_type = youtube_io.records_to_csv(records), "text/csv; charset=utf-8"
    else:
        body, media_type = youtube_io.records_to_json(records), "application/json"
    filename = f"youtube-uploads-{stamp}.{format}"
    return Response(
        # utf-8-sig so Excel opens the Vietnamese titles without mojibake.
        content=body.encode("utf-8-sig") if format == "csv" else body.encode("utf-8"),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/youtube/uploads/import")
async def youtube_import_uploads(
    request: Request,
    file: UploadFile = File(...),
    format: str = Form(default=""),
    mode: str = Form(default="update"),
    dry_run: bool = Form(default=False),
):
    """Apply an edited sheet back to the upload queue.

    `dry_run=true` returns the per-row verdict without writing, so the UI can preview
    the changes before the user commits them.
    """
    if mode not in youtube_io.IMPORT_MODES:
        raise HTTPException(status_code=400,
                            detail=f"mode phải là một trong: {', '.join(youtube_io.IMPORT_MODES)}")
    fmt = format or youtube_io.detect_format(file.filename)
    if fmt not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format phải là 'json' hoặc 'csv'")

    raw = await file.read()
    if not raw.strip():
        raise HTTPException(status_code=400, detail="File nhập vào rỗng")
    try:
        records = youtube_io.parse_records(raw, fmt)
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail=f"Không đọc được file {fmt.upper()}: {exc}")
    if not records:
        raise HTTPException(status_code=400, detail="File nhập vào không có bản ghi nào")

    create = None
    if mode == "upsert" and not dry_run:
        if getattr(request.app.state, "job_queue", None) is None:
            raise HTTPException(status_code=503, detail="Upload worker is unavailable")
        create = _queue_imported_upload(request)

    with locked_conn(request) as conn:
        try:
            summary = youtube_io.apply_import(conn, records, mode=mode, dry_run=dry_run, create=create)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    return JSONResponse(summary)


# ---------------------------------------------------------------------------
# Playlist management JSON APIs
#
# Every endpoint calls the expected app.youtube service functions
# (list_playlists, list_playlist_items, list_channel_videos, bulk_add_to_playlist,
# bulk_remove_from_playlist, get_all_playlist_items, copy_playlist_items,
# move_playlist_items, update_playlist_item_position, reorder_playlist_page,
# reorder_playlist, sort_playlist_preview, sort_playlist) on a throwaway connection
# via _call_youtube,
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


class _PlaylistUpdateBody(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    description: str | None = None


@router.patch("/youtube/api/playlists/{playlist_id}")
def api_update_playlist(request: Request, playlist_id: str, body: _PlaylistUpdateBody):
    """Rename a playlist (and optionally its description)."""
    _require_connected(request)
    _require_playlist_id(playlist_id)
    result = _call_youtube(request, youtube.update_playlist_title, playlist_id, body.title, body.description)
    return JSONResponse(result)


class _VideoUpdateBody(BaseModel):
    title: str = Field(min_length=1, max_length=youtube_metadata.YOUTUBE_TITLE_LIMIT)
    description: str | None = None


@router.patch("/youtube/api/videos/{video_id}")
def api_update_video(request: Request, video_id: str, body: _VideoUpdateBody):
    """Rename a video already live on the channel (e.g. one shown in a playlist)."""
    _require_connected(request)
    result = _call_youtube(request, youtube.update_video_metadata, video_id, body.title, body.description)
    return JSONResponse(result)


@router.get("/youtube/api/playlists/{playlist_id}/items")
def api_list_playlist_items(request: Request, playlist_id: str, max_results: int = 50,
                            page_token: str | None = None, fetch_all: bool = False):
    """List items (videos) in a playlist.

    One page by default. fetch_all=1 pages through the whole playlist server-side and
    returns every item with no page tokens, so the manager can show one flat list.
    """
    _require_connected(request)
    _require_playlist_id(playlist_id)
    if fetch_all:
        items = _call_youtube(request, youtube.get_all_playlist_items, playlist_id)
        return JSONResponse({
            "playlist_id": playlist_id,
            "items": items,
            "next_page_token": None,
            "prev_page_token": None,
            "total": len(items),
            "count": len(items),
        })
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


@router.post("/youtube/api/playlists/{playlist_id}/reorder-all")
def api_reorder_playlist_all(request: Request, playlist_id: str, body: _PlaylistReorderAllBody):
    """Apply one whole-playlist order in a single call.

    `item_ids` is every playlistItem.id in its target order - the shape the manager's
    "save all" produces after a session of local drag/sort/index edits. Unlike
    /reorder this is not page-bounded, so items may cross page boundaries.
    """
    _require_connected(request)
    _require_playlist_id(playlist_id)
    order = _map_playlist_item_ids(request, playlist_id, body.item_ids)
    result = _call_youtube(request, youtube.reorder_playlist, playlist_id, order)
    return JSONResponse(_mark_partial(result))


@router.post("/youtube/api/playlists/{playlist_id}/sort/preview")
def api_preview_playlist_sort(request: Request, playlist_id: str, body: _PlaylistSortBody):
    """Preview a whole-playlist title sort without applying it."""
    _require_connected(request)
    _require_playlist_id(playlist_id)
    preview = _call_youtube(request, youtube.sort_playlist_preview, playlist_id,
                            body.direction, body.mode)
    return JSONResponse(preview)


@router.post("/youtube/api/playlists/{playlist_id}/sort/apply")
def api_apply_playlist_sort(request: Request, playlist_id: str, body: _PlaylistSortBody):
    """Apply a whole-playlist title sort."""
    _require_connected(request)
    _require_playlist_id(playlist_id)
    result = _call_youtube(request, youtube.sort_playlist, playlist_id,
                           body.direction, body.mode)
    return JSONResponse(_mark_partial(result))


# ---------------------------------------------------------------------------
# Playlist create/delete/bulk-update
# ---------------------------------------------------------------------------


class _PlaylistCreateBody(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    description: str = ""
    privacy: str = "private"


@router.post("/youtube/api/playlists")
def api_create_playlist(request: Request, body: _PlaylistCreateBody):
    """Create a new playlist on the channel."""
    _require_connected(request)
    if body.privacy not in youtube_io.PRIVACY_VALUES:
        raise HTTPException(status_code=400,
                            detail=f"privacy phải là một trong: {', '.join(youtube_io.PRIVACY_VALUES)}")
    result = _call_youtube(request, youtube.create_playlist, body.title, body.description, body.privacy)
    return JSONResponse(result)


@router.delete("/youtube/api/playlists/{playlist_id}")
def api_delete_playlist(request: Request, playlist_id: str):
    """Delete a playlist (does not delete the videos it contained)."""
    _require_connected(request)
    _require_playlist_id(playlist_id)
    result = _call_youtube(request, youtube.delete_playlist, playlist_id)
    return JSONResponse(result)


class _PlaylistBulkUpdateBody(BaseModel):
    playlist_ids: list[str] = Field(min_length=1)
    privacy_status: str | None = None
    title_prefix: str = ""
    title_suffix: str = ""
    description_template: str | None = None

    @field_validator("privacy_status")
    @classmethod
    def _valid_privacy(cls, v):
        if v is not None and v not in youtube_io.PRIVACY_VALUES:
            raise ValueError(f"privacy_status phải là một trong: {', '.join(youtube_io.PRIVACY_VALUES)}")
        return v


@router.post("/youtube/api/playlists/bulk-update")
def api_bulk_update_playlists(request: Request, body: _PlaylistBulkUpdateBody):
    """Apply the same privacy/title-affix/description change to several playlists."""
    _require_connected(request)
    result = _call_youtube(
        request, youtube.bulk_update_playlists, body.playlist_ids,
        privacy_status=body.privacy_status, title_prefix=body.title_prefix,
        title_suffix=body.title_suffix, description_template=body.description_template,
    )
    return JSONResponse(_mark_partial(result))


# ---------------------------------------------------------------------------
# Channel-videos cache (Videos-kênh tab)
#
# "Đồng bộ từ YouTube" (sync_channel_videos) snapshots every video on the channel -
# including ones in no playlist - plus its playlist membership into
# youtube_channel_videos; browsing/search/filter/sort/pagination below reads that
# local cache (locked_conn, no network, no quota). Only the sync itself and the
# bulk actions that mutate YouTube touch the network (_call_youtube).
# ---------------------------------------------------------------------------


@router.post("/youtube/api/channel/videos/sync")
def api_sync_channel_videos(request: Request):
    """Refresh the local channel-videos cache from the YouTube API."""
    _require_connected(request)
    channel_id = _channel_id_from_creds(request)
    result = _call_youtube(request, youtube.sync_channel_videos, channel_id)
    return JSONResponse(result)


@router.get("/youtube/api/channel/videos/cached")
def api_list_cached_channel_videos(
    request: Request,
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
):
    """Browse the cached channel-videos snapshot (local only; call sync first)."""
    _validate_page_size(page_size)
    with locked_conn(request) as conn:
        result = youtube.list_cached_channel_videos(
            conn, search=search, privacy_status=privacy_status, has_playlist=has_playlist,
            playlist_id=playlist_id, date_from=date_from, date_to=date_to,
            sort=sort, order=order, page=page, page_size=page_size,
        )
        status = youtube.channel_videos_sync_status(conn)
    return JSONResponse({**result, "synced_at": status["synced_at"]})


@router.get("/youtube/api/channel/videos/status")
def api_channel_videos_status(request: Request):
    """Last sync time and cached row count, for the sync banner."""
    with locked_conn(request) as conn:
        return JSONResponse(youtube.channel_videos_sync_status(conn))


@router.get("/youtube/api/channel/videos/export")
def api_export_channel_videos(request: Request, format: str = "json", ids: str | None = None):
    """Export the cached channel-videos snapshot (or a selected subset) as JSON/CSV."""
    if format not in ("json", "csv"):
        raise HTTPException(status_code=400, detail="format phải là 'json' hoặc 'csv'")
    id_list = [part.strip() for part in ids.split(",") if part.strip()] if ids else None
    with locked_conn(request) as conn:
        if id_list:
            records = youtube.get_cached_channel_videos(conn, id_list)
        else:
            records = youtube.list_cached_channel_videos(conn, page=1, page_size=10_000)["items"]

    columns = ["video_id", "title", "description", "tags", "privacy_status", "duration_sec",
              "view_count", "published_at", "playlist_ids"]
    rows = [{**r, "tags": ", ".join(r["tags"]), "playlist_ids": ", ".join(r["playlist_ids"])} for r in records]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if format == "csv":
        import csv, io
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        body, media_type = buffer.getvalue(), "text/csv; charset=utf-8"
        content = body.encode("utf-8-sig")
    else:
        body = json.dumps({"kind": "youtube_channel_videos", "version": 1, "videos": rows},
                          ensure_ascii=False, indent=2)
        media_type = "application/json"
        content = body.encode("utf-8")
    filename = f"youtube-channel-videos-{stamp}.{format}"
    return Response(content=content, media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


class _ChannelVideoBulkUpdateBody(BaseModel):
    video_ids: list[str] = Field(min_length=1)
    title_template: str = ""
    description_template: str = ""
    privacy_status: str | None = None
    add_tags: list[str] = Field(default_factory=list)

    @field_validator("privacy_status")
    @classmethod
    def _valid_privacy(cls, v):
        if v is not None and v not in youtube_io.PRIVACY_VALUES:
            raise ValueError(f"privacy_status phải là một trong: {', '.join(youtube_io.PRIVACY_VALUES)}")
        return v


_EPISODE_RE = re.compile(r"(?:episode|tap|tập|chuong|chương|ep)[\s#]*(\d+)", re.IGNORECASE)


@router.post("/youtube/api/channel/videos/bulk-update")
def api_bulk_update_channel_videos(request: Request, body: _ChannelVideoBulkUpdateBody):
    """Bulk-edit title/description (with {episode} templating), privacy, and tags
    for several live channel videos at once."""
    _require_connected(request)
    with locked_conn(request) as conn:
        current = {r["video_id"]: r for r in youtube.get_cached_channel_videos(conn, body.video_ids)}

    updates = []
    for video_id in body.video_ids:
        row = current.get(video_id, {})
        item: dict = {"video_id": video_id}
        if body.title_template:
            title = row.get("title", "")
            match = _EPISODE_RE.search(title)
            episode = match.group(1) if match else video_id
            item["title"] = body.title_template.replace("{episode}", episode)
        if body.description_template:
            description = row.get("description", "")
            match = _EPISODE_RE.search(description)
            episode = match.group(1) if match else video_id
            item["description"] = body.description_template.replace("{episode}", episode)
        if body.privacy_status:
            item["privacy_status"] = body.privacy_status
        if body.add_tags:
            item["tags"] = sorted(set(row.get("tags", [])) | set(body.add_tags))
        updates.append(item)

    result = _call_youtube(request, youtube.bulk_update_channel_videos, updates)
    return JSONResponse(_mark_partial(result))


@router.post("/youtube/api/channel/videos/bulk-delete")
def api_bulk_delete_channel_videos(request: Request, video_ids: list[str]):
    """Permanently delete videos from YouTube. Irreversible - the UI requires the
    user to type a confirmation phrase before calling this."""
    _require_connected(request)
    if not video_ids:
        raise HTTPException(status_code=400, detail="video_ids không được để trống")
    result = _call_youtube(request, youtube.delete_channel_videos, video_ids)
    return JSONResponse(_mark_partial(result))


class _ChannelVideoPlaylistBody(BaseModel):
    playlist_id: str = Field(min_length=1)
    video_ids: list[str] = Field(min_length=1)


@router.post("/youtube/api/channel/videos/bulk-add-to-playlist")
def api_bulk_add_channel_videos_to_playlist(request: Request, body: _ChannelVideoPlaylistBody):
    """Add several channel videos (selected from the Videos-kênh tab) to a playlist."""
    _require_connected(request)
    result = _call_youtube(request, youtube.bulk_add_to_playlist, body.playlist_id, body.video_ids)
    return JSONResponse(_mark_partial(result))


@router.post("/youtube/api/channel/videos/bulk-remove-from-playlist")
def api_bulk_remove_channel_videos_from_playlist(request: Request, body: _ChannelVideoPlaylistBody):
    """Remove several channel videos from a playlist (by video id)."""
    _require_connected(request)
    result = _call_youtube(request, youtube.bulk_remove_from_playlist, body.playlist_id,
                           video_ids=body.video_ids)
    return JSONResponse(_mark_partial(result))


# ---------------------------------------------------------------------------
# Bulk actions: standardize title/description, scheduled publish, AI labels
# ---------------------------------------------------------------------------


class _BulkUpdateBody(BaseModel):
    ids: list[int]
    title_template: str = ""
    description_template: str = ""
    scheduled_publish_at: str | None = None
    generate_ai_labels: bool = False


@router.post("/youtube/uploads/bulk-update")
def bulk_update_uploads(request: Request, body: _BulkUpdateBody):
    """Bulk update title/description templates with {episode} placeholder,
    set scheduled publish time, and optionally generate AI labels."""
    if not body.ids:
        raise HTTPException(400, "no upload IDs provided")

    with locked_conn(request) as conn:
        placeholders = conn.execute(
            f"SELECT * FROM youtube_uploads WHERE id IN ({','.join(['?'] * len(body.ids))})",
            body.ids,
        ).fetchall()

        updated_count = 0
        for row in placeholders:
            upload = dict(row)
            title = upload.get("title") or ""
            description = upload.get("description") or ""

            # Apply title template with {episode} replacement
            if body.title_template:
                episode_num = upload.get("id", 0)
                new_title = body.title_template.replace("{episode}", str(episode_num))
                # Also try to extract episode from existing title
                import re
                ep_match = re.search(r'(?:episode|tap|chuong|ep)[\s#]*(\d+)', title, re.IGNORECASE)
                if ep_match:
                    new_title = new_title.replace("{episode}", ep_match.group(1))
                conn.execute("UPDATE youtube_uploads SET title=? WHERE id=?", (new_title, upload["id"]))
                updated_count += 1

            # Apply description template with {episode} replacement
            if body.description_template:
                episode_num = upload.get("id", 0)
                new_desc = body.description_template.replace("{episode}", str(episode_num))
                import re
                ep_match = re.search(r'(?:episode|tap|chuong|ep)[\s#]*(\d+)', description, re.IGNORECASE)
                if ep_match:
                    new_desc = new_desc.replace("{episode}", ep_match.group(1))
                conn.execute("UPDATE youtube_uploads SET description=? WHERE id=?", (new_desc, upload["id"]))
                updated_count += 1

            # Set scheduled publish time
            if body.scheduled_publish_at is not None:
                youtube.set_upload_scheduled_publish(conn, upload["id"], body.scheduled_publish_at)

            # Generate AI labels
            if body.generate_ai_labels:
                labels = youtube.generate_ai_labels(
                    upload.get("title") or "",
                    upload.get("description") or "",
                )
                if labels:
                    youtube.set_upload_ai_labels(conn, upload["id"], labels)

        conn.commit()

    return {"updated": updated_count, "total": len(body.ids)}


@router.post("/youtube/uploads/{upload_id}/schedule")
def set_upload_schedule(request: Request, upload_id: int, body: dict):
    """Set or clear the scheduled publish time for a single upload."""
    scheduled_at = body.get("scheduled_publish_at")
    with locked_conn(request) as conn:
        youtube.set_upload_scheduled_publish(conn, upload_id, scheduled_at)
    return {"status": "ok", "upload_id": upload_id, "scheduled_publish_at": scheduled_at}


@router.post("/youtube/uploads/{upload_id}/ai-labels")
def generate_upload_ai_labels(request: Request, upload_id: int):
    """Generate AI labels for a single upload and merge into tags."""
    with locked_conn(request) as conn:
        row = conn.execute("SELECT title, description FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "upload not found")
        labels = youtube.generate_ai_labels(row["title"] or "", row["description"] or "")
        if labels:
            youtube.set_upload_ai_labels(conn, upload_id, labels)
    return {"upload_id": upload_id, "ai_labels": labels}


@router.get("/youtube/uploads/daily-status")
def get_daily_upload_status(request: Request):
    """Return the daily upload count and remaining quota."""
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)

    with locked_conn(request) as conn:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM youtube_uploads WHERE status='done' AND uploaded_at >= ? AND uploaded_at < ?",
            (today_start.isoformat(), today_end.isoformat()),
        ).fetchone()
        uploaded_today = row["cnt"] if row else 0

    daily_limit = 50  # YouTube default
    return {
        "uploaded_today": uploaded_today,
        "daily_limit": daily_limit,
        "remaining": max(0, daily_limit - uploaded_today),
    }
