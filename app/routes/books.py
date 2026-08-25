from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from app import google_drive, repository, video_gen
from app.config import settings
from app.deps import locked_conn
from app.epub_parser import parse_epub
from app.normalization import normalize_text
from app.youtube_metadata import get_book_youtube_config, save_book_youtube_config
from app.video_config import save_book_video_config, validate_video_config
from app.audio_merge import DEFAULT_CHAPTER_PAUSE_MS, DEFAULT_CHUNK_PAUSE_MS
from app.production_defaults import (get_effective_audio_config, get_effective_branding_config,
                                     get_effective_normalization_options,
                                     get_effective_video_config, get_effective_youtube_config,
                                     save_book_audio_section, set_book_group_mode_db,
                                     validate_pause_ms)
from app import youtube
from app import db as app_db

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# Book backgrounds may be a still image or a looping video clip.
ALLOWED_BACKGROUND_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | video_gen.VIDEO_BACKGROUND_EXTENSIONS

router = APIRouter()
logger = logging.getLogger(__name__)


def _list_youtube_playlists():
    api_conn = app_db.connect(settings.db_path)
    try:
        return youtube.list_playlists(api_conn)
    finally:
        api_conn.close()






@router.post("/books/parse-epub")
async def parse_epub_preview(request: Request, epub_file: UploadFile = File(...)):
    """Parse an EPUB and return chapter list as JSON without creating a book."""
    uploads_dir = Path(settings.data_root) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    tmp_path = uploads_dir / f"_tmp_preview_{epub_file.filename}"
    try:
        with open(tmp_path, "wb") as f:
            shutil.copyfileobj(epub_file.file, f)

        chapters = parse_epub(str(tmp_path))
        return JSONResponse({
            "filename": epub_file.filename,
            "title": Path(epub_file.filename).stem,
            "chapters": [
                {
                    "index": idx,
                    "title": ch.title,
                    "char_count": ch.char_count,
                    "text_excerpt": ch.text[:300],
                }
                for idx, ch in enumerate(chapters)
            ],
        })
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


@router.post("/books/upload")
async def upload_book(
    request: Request,
    epub_file: UploadFile = File(...),
):
    """Upload and parse an EPUB; patches are configured later on the book page."""
    uploads_dir = Path(settings.data_root) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    tmp_epub_path = uploads_dir / f"_tmp_{epub_file.filename}"
    with open(tmp_epub_path, "wb") as f:
        shutil.copyfileobj(epub_file.file, f)

    chapters = parse_epub(str(tmp_epub_path))
    title = Path(epub_file.filename).stem

    with locked_conn(request) as conn:
        book = repository.create_book(
            conn,
            title=title,
            original_filename=epub_file.filename,
            epub_path="",  # finalized below once the book id (and thus its folder name) is known
            patch_size=10,
            chapters=chapters,
            background_image_path=None,
        )

        final_epub_path = uploads_dir / f"{book.id}.epub"
        tmp_epub_path.rename(final_epub_path)

        conn.execute(
            "UPDATE book SET epub_path = ? WHERE id = ?",
            (str(final_epub_path), book.id),
        )
        conn.commit()

    return RedirectResponse(url=f"/books/{book.id}", status_code=303)




@router.post("/books/{book_id}/video-settings")
def update_video_settings(
    request: Request, book_id: int,
    video_resolution: str = Form(default=""),
    video_fps: str = Form(default=""),
    default_image_animation: str = Form(default=""),
):
    """Persist the book-wide video config (resolution / fps / default animation)
    used by per-patch video generation. Returns JSON for the async config modal."""
    valid_res = {"1920x1080", "1280x720", "854x480"}
    valid_fps = {24, 30, 60}
    valid_anim = {"none", "static", "zoom-in", "zoom-out", "pan-left", "pan-right"}

    res = video_resolution if video_resolution in valid_res else None
    fps: int | None = None
    if video_fps.strip():
        try:
            fps_val = int(video_fps)
        except ValueError:
            raise HTTPException(status_code=400, detail="fps không hợp lệ")
        fps = fps_val if fps_val in valid_fps else None
    anim = default_image_animation if default_image_animation in valid_anim else None

    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        repository.update_book_video_settings(
            conn, book_id,
            video_resolution=res, video_fps=fps, default_image_animation=anim,
        )
    return JSONResponse({
        "status": "saved",
        "video_resolution": res or (book.video_resolution or "1920x1080"),
        "video_fps": fps or (book.video_fps or 30),
    })


@router.get("/books/{book_id}/video-config")
def get_video_config(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        return get_effective_video_config(conn, book)


@router.post("/books/{book_id}/video-config")
async def save_video_config(request: Request, book_id: int):
    data = await request.json()
    try:
        validated = validate_video_config(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        result = save_book_video_config(conn, book_id, validated)

    # Cấu hình video giờ hợp lệ => các patch đang waiting_config có thể chạy tiếp.
    def _reconcile():
        from app.patch_publishing import reconcile_patch_automation
        with app_db.connect(settings.db_path) as conn:
            return reconcile_patch_automation(conn, book_id=book_id)
    try:
        await asyncio.to_thread(_reconcile)
    except Exception:
        logger.exception("automation reconcile failed after video-config save for book %s", book_id)
    return result


@router.post("/books/{book_id}/youtube-settings")
async def update_youtube_settings(request: Request, book_id: int):
    data = await request.json()
    if "description_template" in data:
        data["description"] = data.pop("description_template")
    playlist = data.get("playlist") or {}
    if playlist.get("mode") == "create":
        raise HTTPException(400, "playlist creation is no longer supported; select an existing playlist")
    if "id" in playlist:
        playlist["playlist_id"] = playlist.pop("id")
    data["playlist"] = playlist
    # Cờ tự động hoá: lưu JSON (auto_upload legacy hoặc auto_upload_youtube mới) được
    # phản chiếu sang cột persisted của sách; upload bao hàm tạo video. auto_upload
    # legacy vẫn được lưu trong JSON để các đường đọc cũ và kiểm tra playlist khớp.
    flags: dict[str, bool] = {}
    if "auto_upload" in data:
        flags["auto_upload_youtube"] = bool(data.get("auto_upload"))
    if isinstance(data.get("auto_upload_youtube"), bool):
        flags["auto_upload_youtube"] = data.pop("auto_upload_youtube")
    if isinstance(data.get("auto_create_video"), bool):
        flags["auto_create_video"] = data.pop("auto_create_video")
    if flags.get("auto_upload_youtube"):
        flags["auto_create_video"] = True
    data["auto_upload"] = bool(flags.get("auto_upload_youtube", data.get("auto_upload")))
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        try:
            normalized = {**get_effective_youtube_config(conn, book), **data}
            if normalized.get("auto_upload"):
                playlist = normalized["playlist"]
                if not youtube.is_configured() or youtube.get_creds_from_db(conn) is None:
                    raise ValueError("YouTube must be connected before enabling auto upload")
                if playlist["mode"] == "none":
                    raise ValueError("auto upload requires a playlist")
                if playlist["mode"] == "existing":
                    if not playlist["playlist_id"]:
                        raise ValueError("playlist was not found")
            save_book_youtube_config(conn, book_id, data)
            if flags:
                repository.update_book_automation_flags(
                    conn, book_id, auto_create_video=flags.get("auto_create_video"),
                    auto_upload_youtube=flags.get("auto_upload_youtube"),
                )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        result = get_effective_youtube_config(conn, repository.get_book(conn, book_id))
        book = repository.get_book(conn, book_id)
    if result.get("auto_upload") and result["playlist"]["mode"] == "existing":
        try:
            if not any(p.get("id") == result["playlist"]["playlist_id"] for p in _list_youtube_playlists()):
                raise HTTPException(400, "playlist was not found")
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, "YouTube playlist access could not be verified") from exc
    if book is not None:
        result["auto_create_video"] = bool(book.auto_create_video)
        result["auto_upload_youtube"] = bool(book.auto_upload_youtube)
    if flags:
        # Cấu hình giờ hợp lệ => chạy reconcile cho các patch đang chờ config.
        def _reconcile():
            from app.patch_publishing import reconcile_patch_automation
            with app_db.connect(settings.db_path) as conn:
                return reconcile_patch_automation(conn, book_id=book_id)
        try:
            await asyncio.to_thread(_reconcile)
        except Exception:
            logger.exception("automation reconcile failed after youtube-settings save for book %s", book_id)
    return result


@router.get("/books/{book_id}/youtube-settings")
def youtube_settings(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        creds = youtube.get_creds_from_db(conn)
        result = {"config": get_effective_youtube_config(conn, book), "connected": bool(creds), "channel_name": creds.get("channel_name") if creds else None,
                  "auto_create_video": bool(book.auto_create_video),
                  "auto_upload_youtube": bool(book.auto_upload_youtube)}
    try:
        result["playlists"] = _list_youtube_playlists() if result["connected"] else []
    except Exception:
        result["playlists"] = []
    return result





@router.get("/books/{book_id}/status")
def book_status(request: Request, book_id: int):
    """Lightweight JSON endpoint for polling status without reloading the page."""
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        patch_list = repository.list_patches(conn, book_id)
        pipelines = {
            row["patch_id"]: {
                "stage": row["stage"],
                "thumbnail_status": row["thumbnail_status"],
                "video_status": row["video_status"],
                "upload_status": row["upload_status"],
                "playlist_status": row["playlist_status"],
                "thumbnail_path": row["thumbnail_path"],
                "youtube_upload_id": row["youtube_upload_id"],
                "last_error": row["last_error"],
                "upload_state": (
                    "published" if row["stage"] == "published" else
                    "active" if row["youtube_upload_id"] and row["upload_status"] in {"claiming", "queued", "pending", "uploading"} else
                    "postprocessing" if row["stage"] in {"thumbnail_setting", "playlist"} else
                    "failed" if row["last_error"] else "ready"
                ),
                "can_force_new": row["stage"] == "published",
            }
            for row in conn.execute(
                "SELECT patch_id,stage,thumbnail_status,video_status,upload_status,playlist_status,thumbnail_path,youtube_upload_id,last_error FROM patch_pipeline WHERE patch_id IN ({})".format(
                    ",".join("?" for _ in patch_list)
                ),
                [p.id for p in patch_list],
            )
        }
    worker = request.app.state.worker
    live_chunk_index = (
        worker.current_chunk_index
        if getattr(worker, "current_patch_id", None) is not None
        else 0
    )
    live_chunk_count = getattr(worker, "current_chunk_count", 0)
    return JSONResponse({
        "book_status": book.status,
        "has_final_audio": bool(book.final_audio_path),
        "has_active_patches": any(p.status in ("pending", "processing") for p in patch_list),
        "pipelines": pipelines,
        "patches": [
            {
                "id": p.id,
                "status": p.status,
                "error_message": p.error_message,
                "chunk_count": p.chunk_count,
                "next_chunk_index": (
                    live_chunk_index
                    if (p.status == "processing"
                        and getattr(worker, "current_patch_id", None) == p.id)
                    else p.next_chunk_index
                ),
            }
            for p in patch_list
        ],
        "current_chunk_count": live_chunk_count,
    })


@router.post("/books/{book_id}/video")
def trigger_video(request: Request, book_id: int):
    """Enqueue a video book_job. Video generation is now handled by the worker
    (background, non-blocking). If the book has no final audio yet, or a video
    book_job already exists in any status, this is a no-op that just redirects."""
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None or not book.final_audio_path:
            return RedirectResponse(url=f"/books/{book_id}", status_code=303)
        if not book.background_image_path:
            return RedirectResponse(url=f"/books/{book_id}", status_code=303)
        existing = repository.get_book_job(conn, book_id, "video")
        if existing is not None:
            return RedirectResponse(url=f"/books/{book_id}", status_code=303)
        book_job = repository.enqueue_book_job(conn, book_id, "video")
        from app.jobqueue import store
        store.enqueue(
            conn, "video", payload={"book_job_id": book_job.id}, book_id=book_id,
            dedupe_key=f"video:book_job={book_job.id}",
        )

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.get("/books/{book_id}/audio-settings")
def get_audio_settings(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        return get_effective_audio_config(conn, book)


@router.post("/books/{book_id}/audio-settings")
async def save_audio_settings(request: Request, book_id: int):
    data = await request.json()
    model_id = str(data.get("model_id") or settings.tts_engine).strip()
    voice_id = str(data.get("voice_id") or "").strip()
    try:
        max_chars = max(0, int(data.get("max_chars") or 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "max_chars không hợp lệ") from exc
    with_effects = bool(data.get("with_effects", False))
    from app.tts_engine import normalize_tts_options, resolve_engine_id
    try:
        model_id = resolve_engine_id(model_id)
        tts_options = normalize_tts_options(model_id, data.get("tts_options"))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    chunk_pause_ms = validate_pause_ms(data.get("chunk_pause_ms"), DEFAULT_CHUNK_PAUSE_MS)
    chapter_pause_ms = validate_pause_ms(data.get("chapter_pause_ms"), DEFAULT_CHAPTER_PAUSE_MS)
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(404, "book not found")
        conn.execute("UPDATE book SET tts_model=?, tts_voice_id=?, tts_max_chars=?, tts_with_effects=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (model_id, voice_id or None, max_chars or None, int(with_effects), book_id))
        # The pauses have no columns of their own; they live in the book's
        # automation_config alongside the other per-group sections.
        save_book_audio_section(conn, book_id, chunk_pause_ms=chunk_pause_ms,
                                chapter_pause_ms=chapter_pause_ms, tts_options=tts_options)
        conn.commit()
        set_book_group_mode_db(conn, book_id, "audio", "custom")
    return {"model_id": model_id, "voice_id": voice_id, "max_chars": max_chars,
            "with_effects": with_effects, "chunk_pause_ms": chunk_pause_ms,
            "chapter_pause_ms": chapter_pause_ms, "tts_options": tts_options}


@router.get("/books/{book_id}/export-audio-settings")
def get_export_audio_settings(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        return {
            "model_id": book.export_tts_model or "omnivoice",
            "voice_id": book.export_tts_voice_id or "",
            "max_chars": book.export_tts_max_chars or 1200,
            "with_effects": bool(book.export_tts_with_effects),
        }


@router.post("/books/{book_id}/export-audio-settings")
async def save_export_audio_settings(request: Request, book_id: int):
    data = await request.json()
    model_id = str(data.get("model_id") or "omnivoice").strip()
    voice_id = str(data.get("voice_id") or "").strip()
    try:
        max_chars = max(0, int(data.get("max_chars") or 1200))
    except (TypeError, ValueError) as exc:
        raise HTTPException(400, "max_chars không hợp lệ") from exc
    with_effects = bool(data.get("with_effects", False))
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(404, "book not found")
        conn.execute(
            """UPDATE book
                  SET export_tts_model=?, export_tts_voice_id=?, export_tts_max_chars=?,
                      export_tts_with_effects=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?""",
            (model_id, voice_id or None, max_chars, int(with_effects), book_id),
        )
        conn.commit()
    return {
        "model_id": model_id,
        "voice_id": voice_id,
        "max_chars": max_chars,
        "with_effects": with_effects,
    }


@router.get("/books/{book_id}/music", name="get_book_music_legacy")
def get_book_music(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        return {"music_id": book.music_id, "music_volume": round((book.music_volume or 0) * 100), "tracks": [dict(row) for row in conn.execute("SELECT id, name, duration_sec FROM music ORDER BY name")]}


@router.post("/books/{book_id}/music-json")
async def save_book_music_json(request: Request, book_id: int):
    data = await request.json()
    music_id = data.get("music_id")
    volume = max(0, min(100, int(data.get("music_volume", 15))))
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(404, "book not found")
        if music_id is not None and conn.execute("SELECT 1 FROM music WHERE id=?", (music_id,)).fetchone() is None:
            raise HTTPException(400, "music track not found")
        repository.set_book_music(conn, book_id, int(music_id) if music_id is not None else None, volume / 100)
        book = repository.get_book(conn, book_id)
    return {"music_id": book.music_id, "music_volume": round((book.music_volume or 0) * 100)}



@router.post("/books/{book_id}/music", name="update_book_music_legacy")
def update_book_music(
    request: Request,
    book_id: int,
    music_id: str = Form(default=""),
    music_volume: int = Form(default=15),
):
    from app import image_overlay
    mid: int | None = None
    if music_id.strip().isdigit():
        mid = int(music_id.strip())
    vol = max(0, min(100, music_volume)) / 100.0
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        repository.set_book_music(conn, book_id, mid, vol)
        book = repository.get_book(conn, book_id)
        patches = repository.list_patches(conn, book_id)
    font_path = settings.default_font_path or None
    for patch in patches:
        try:
            image_overlay.ensure_patch_overlay(book, patch, font_path)
        except Exception:
            pass
    if request.headers.get("X-Requested-With") == "autosave":
        return {"status": "ok"}
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/rename")
def rename_book(request: Request, book_id: int, title: str = Form(...)):
    new_title = title.strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="Tên không được để trống")
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy sách")
        repository.rename_book(conn, book_id, new_title)
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.get("/books/{book_id}/overlay-config")
def get_overlay_config(request: Request, book_id: int):
    from app import image_overlay
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")

    config = image_overlay.parse_overlay_config(book.overlay_config)

    return {
        "config": config,
        "fonts": image_overlay.list_overlay_fonts(),
        "backgrounds": _list_backgrounds(),
        "background_path": book.background_image_path,
        "placeholders": [
            {"key": "book_title", "label": "Book Title"},
            {"key": "patch_name", "label": "Patch Name"},
            {"key": "patch_index", "label": "Patch Index"},
            {"key": "episode", "label": "Episode"},
            {"key": "chapter", "label": "Chapter Range"},
            {"key": "chapter_start", "label": "Start Chapter"},
            {"key": "chapter_end", "label": "End Chapter"},
        ]
    }


@router.post("/books/{book_id}/overlay-config")
async def update_overlay_config(request: Request, book_id: int):
    from app import image_overlay

    values = await request.form()
    cfg = image_overlay.overlay_cfg_from_values(values)

    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")

        # Một form không nhắc gì tới podcast là form cũ, không phải lệnh tắt
        # ảnh bìa — giữ nguyên thiết lập podcast đã lưu.
        if not any(str(key).startswith("podcast_") for key in values.keys()):
            cfg["podcast_cover"] = image_overlay.parse_podcast_cover(
                image_overlay.parse_overlay_config(book.overlay_config)
            )

        # Save config and the image selected in the live preview. Previously the
        # preview background was sent only to overlay-preview, so pressing Save lost it.
        background_path = str(values.get("background_path") or "").strip()
        if background_path:
            allowed = {item["path"] for item in _list_backgrounds() if not item.get("is_video")}
            if background_path not in allowed or not Path(background_path).is_file():
                raise HTTPException(status_code=400, detail="Background preview không hợp lệ")
        conn.execute(
            "UPDATE book SET overlay_config = ?, background_image_path = ?, updated_at = ? WHERE id = ?",
            (json.dumps(cfg, ensure_ascii=False), background_path or book.background_image_path,
             datetime.now(timezone.utc).isoformat(), book_id),
        )

        # Invalidate thumbnail pipeline
        patches = repository.list_patches(conn, book_id)
        if patches:
            patch_ids = [p.id for p in patches]
            conn.execute(
                f"UPDATE patch_pipeline SET thumbnail_status='pending' WHERE patch_id IN ({','.join(['?']*len(patch_ids))})",
                patch_ids
            )

            # Clear old PNGs
            for patch in patches:
                path = image_overlay.get_patch_overlay_path(book_id, patch.patch_index)
                if path.exists():
                    path.unlink()

        # Ảnh bìa podcast cắt ra từ chính thumbnail, nên cũng lỗi thời theo.
        cover = image_overlay.get_podcast_cover_path(book_id)
        if cover.exists():
            cover.unlink()

        conn.commit()

    if request.headers.get("X-Requested-With") == "autosave" or "overlays_json" in values:
        return {"status": "ok"}
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


def _overlay_preview_context(request: Request, book_id: int):
    """(book, overlay cfg, background path, sample patch, branding) for a preview render.

    `live=1` builds the config from the query params so unsaved studio edits
    show up; otherwise the saved config is used. `background_path` previews a
    different (whitelisted) background before it is saved on the book.

    Branding is resolved from the book's effective branding config.  When
    `branding_mode=custom` is passed, a `branding_json` query param overrides
    the saved branding for live preview.
    """
    from app import image_overlay
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        patches = repository.list_patches(conn, book_id)
        # Resolve effective branding
        branding = get_effective_branding_config(conn, book)
    sample_patch = next((p for p in patches if p.audio_path), None) or (patches[0] if patches else None)
    patch_label = (sample_patch.name or str(sample_patch.patch_index)) if sample_patch else "Patch 1"

    params = request.query_params
    if params.get("live"):
        cfg = image_overlay.overlay_cfg_from_values(params)
    else:
        cfg = image_overlay.parse_overlay_config(book.overlay_config)

    # Live branding override from query params
    branding_json = params.get("branding_json")
    if branding_json:
        try:
            branding = json.loads(branding_json)
        except (json.JSONDecodeError, TypeError):
            pass

    bg = None
    requested_bg = params.get("background_path", "").strip()
    if requested_bg:
        allowed = {item["path"] for item in _list_backgrounds()}
        if requested_bg in allowed and Path(requested_bg).exists():
            bg = Path(requested_bg)
    if bg is None:
        bg = image_overlay._resolve_background(book)
    if bg is None:
        raise HTTPException(status_code=400, detail="chưa có background image")

    preview_patch = sample_patch or SimpleNamespace(
        name=patch_label, patch_index=0, chapter_start=0, chapter_end=0,
    )
    return book, cfg, bg, preview_patch, branding


@router.get("/books/{book_id}/overlay-preview")
def overlay_preview(request: Request, book_id: int):
    """Render the overlay preview PNG.

    Without params it renders the saved config. With `live=1` the remaining
    query params (same names as the overlay form fields) override the saved
    config, so the studio can preview unsaved edits. `background_path` (must
    be a known background) previews a different image before saving it.

    `branding_json` optionally overrides the effective branding for live preview.

    The response carries an `X-Overlay-Rect` header with the drawn text-block
    rect so the studio can place its drag handle exactly on the text.
    """
    from io import BytesIO
    book, cfg, bg, preview_patch, branding = _overlay_preview_context(request, book_id)

    from PIL import Image
    from app import image_overlay
    img = Image.open(str(bg)).convert("RGB")
    rects = []
    for overlay in cfg.get("overlays") or [cfg]:
        text = image_overlay.expand_overlay_text(overlay.get("text", ""), book, preview_patch)
        lines = image_overlay.build_overlay_lines(img, text, overlay)
        img, rect = image_overlay.render_overlay_with_rect(img, lines, overlay)
        rects.append(rect)
    # Apply branding to preview if targets include thumbnail
    if branding and branding.get("targets", {}).get("thumbnail", True):
        img = image_overlay.apply_branding(img, branding, target="thumbnail")
    buf = BytesIO()
    img.save(buf, "PNG", optimize=True)
    rect_header = json.dumps({
        "x": rects[0][0], "y": rects[0][1], "w": rects[0][2], "h": rects[0][3],
        "img_w": img.size[0], "img_h": img.size[1],
        "rects": [{"x": r[0], "y": r[1], "w": r[2], "h": r[3]} for r in rects],
    })
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"X-Overlay-Rect": rect_header, "Cache-Control": "no-store"},
    )


@router.get("/books/{book_id}/podcast-cover-preview")
def podcast_cover_preview(request: Request, book_id: int):
    """The same artwork as the thumbnail, cropped 1:1 for YouTube Podcasts.

    Takes the same live params as /overlay-preview plus podcast_focus_x,
    podcast_focus_y and podcast_cover_size, so the studio can drag the crop
    around before saving.

    Branding is applied after the square crop when the podcast target is enabled.
    """
    from io import BytesIO
    from app import image_overlay
    book, cfg, bg, preview_patch, branding = _overlay_preview_context(request, book_id)
    img = image_overlay.compose_patch_overlay(book, preview_patch, cfg, str(bg))
    podcast = image_overlay.parse_podcast_cover(cfg)
    cover = image_overlay.crop_square(img, podcast["focus_x"], podcast["focus_y"], podcast["size"])
    # Apply branding after square crop for podcast target
    if branding and branding.get("targets", {}).get("podcast", True):
        cover = image_overlay.apply_branding(cover, branding, target="podcast")
    buf = BytesIO()
    cover.save(buf, "PNG", optimize=True)
    return Response(
        content=buf.getvalue(),
        media_type="image/png",
        headers={"X-Podcast-Cover-Size": str(podcast["size"]), "Cache-Control": "no-store"},
    )


@router.post("/books/{book_id}/podcast-cover/regenerate")
def regenerate_podcast_cover(request: Request, book_id: int):
    """Write the saved square cover to disk so an upload can pick it up."""
    from app import image_overlay
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        patches = repository.list_patches(conn, book_id)
    try:
        path = image_overlay.render_podcast_cover(book, image_overlay.pick_cover_patch(patches))
    except (ValueError, OSError) as exc:
        # Sách chưa có background, hoặc background là video (PIL không mở được).
        raise HTTPException(400, f"Không tạo được ảnh bìa podcast: {exc}") from exc
    cover = image_overlay.parse_podcast_cover(image_overlay.parse_overlay_config(book.overlay_config))
    return {"status": "ok", "path": path, "size": cover["size"]}


@router.get("/books/{book_id}/podcast-cover")
def get_podcast_cover(request: Request, book_id: int):
    """Serve the saved square cover, rendering it on first use."""
    from app import image_overlay
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        patches = repository.list_patches(conn, book_id)
    force = request.query_params.get("force") in {"1", "true"}
    path = image_overlay.ensure_podcast_cover(book, image_overlay.pick_cover_patch(patches), force=force)
    if not path or not Path(path).is_file():
        raise HTTPException(404, "Chưa tạo được ảnh bìa podcast")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/books/{book_id}/podcast/apply")
def apply_podcast_settings(request: Request, book_id: int):
    """Push the book's podcast settings (flag + 1:1 cover) to its playlist."""
    from app import image_overlay
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        if youtube.get_creds_from_db(conn) is None:
            raise HTTPException(400, "Chưa kết nối YouTube")
        config = get_effective_youtube_config(conn, book)
        playlist_id = _resolve_book_playlist_id(conn, book_id, config)
        patches = repository.list_patches(conn, book_id)
    if not playlist_id:
        raise HTTPException(400, "Sách chưa có playlist trên YouTube — chọn playlist rồi thử lại")

    podcast = config.get("podcast") or {}
    cover_path = None
    if podcast.get("upload_cover", True):
        cover_path = image_overlay.ensure_podcast_cover(book, image_overlay.pick_cover_patch(patches), force=True)
        if not cover_path:
            raise HTTPException(400, "Chưa tạo được ảnh bìa podcast — kiểm tra background của sách")

    api_conn = app_db.connect(settings.db_path)
    try:
        result = youtube.sync_playlist_podcast(
            api_conn, book_id, playlist_id,
            enabled=bool(podcast.get("enabled")), cover_path=cover_path, force=True,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        logger.exception("podcast apply failed for book %s", book_id)
        raise HTTPException(502, f"YouTube từ chối cập nhật podcast: {exc}") from exc
    finally:
        api_conn.close()
    return result


def _resolve_book_playlist_id(conn, book_id: int, config: dict) -> str:
    """The playlist a book publishes into: the configured one, else the
    auto-created one recorded in youtube_playlist_map."""
    playlist = config.get("playlist") or {}
    if playlist.get("mode") == "existing" and playlist.get("playlist_id"):
        return playlist["playlist_id"]
    row = conn.execute(
        "SELECT playlist_id FROM youtube_playlist_map WHERE book_id=? AND playlist_id<>'__creating__' "
        "ORDER BY updated_at DESC LIMIT 1",
        (book_id,),
    ).fetchone()
    return row["playlist_id"] if row else ""


@router.post("/books/{book_id}/delete")
def delete_book(request: Request, book_id: int):
    with locked_conn(request) as conn:
        ok = repository.delete_book(conn, book_id, settings.data_root)
    if not ok:
        raise HTTPException(status_code=404, detail=f"book {book_id} not found")
    return RedirectResponse(url="/books", status_code=303)


@router.post("/books/{book_id}/voice-select")
def select_voice(
    request: Request, book_id: int,
    voice_name: str = Form(default=""),
    voice_transcript: str = Form(default=""),
):
    """Set the book's TTS reference voice clone from the /voices library.

    This is the same file the studio's mix preview plays — picking it there
    both previews it and sets book.voice_clip_path, which the worker passes
    to the TTS engine as reference_wav_path. voice_transcript is the exact
    words spoken in that clip, which improves cloning accuracy.
    """
    from app.routes.voices import ALLOWED_AUDIO_EXTENSIONS as _voice_exts, _voices_dir

    name = voice_name.strip()
    path: str | None = None
    if name:
        candidate = _voices_dir() / name
        if "/" in name or "\\" in name or ".." in name or candidate.suffix.lower() not in _voice_exts:
            raise HTTPException(status_code=400, detail="Tên voice không hợp lệ")
        if not candidate.exists():
            raise HTTPException(status_code=400, detail="Không tìm thấy voice")
        path = str(candidate)

    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail="book not found")
        repository.set_book_voice_clip(conn, book_id, path, voice_transcript.strip() or None)
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/background-image-select")
def select_background_image(
    request: Request, book_id: int,
    background_path: str = Form(default=""),
):
    from app import image_overlay

    path: str | None = background_path.strip() or None
    if path and not Path(path).exists():
        raise HTTPException(status_code=400, detail="File ảnh không tồn tại")

    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        conn.execute(
            "UPDATE book SET background_image_path = ?, updated_at = ? WHERE id = ?",
            (path, datetime.now(timezone.utc).isoformat(), book_id),
        )
        conn.commit()
        book = repository.get_book(conn, book_id)
        patches = repository.list_patches(conn, book_id)

    font_path = settings.default_font_path or None
    for patch in patches:
        try:
            image_overlay.ensure_patch_overlay(book, patch, font_path)
        except Exception:
            pass
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/background-image")
async def upload_background_image(
    request: Request, book_id: int,
    image: UploadFile = File(...),
):
    ext = Path(image.filename or "").suffix.lower()
    if ext not in ALLOWED_BACKGROUND_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Định dạng không hỗ trợ: {ext}")

    from app import image_overlay

    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")

    uploads_dir = Path(settings.data_root) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    if book.background_image_path:
        Path(book.background_image_path).unlink(missing_ok=True)

    filename = f"{book_id}_bg_{uuid.uuid4().hex[:8]}{ext}"
    dest = uploads_dir / filename

    # Off the lock and off the event loop: a large background would otherwise stall
    # every concurrent request while it is written to disk.
    def _save() -> None:
        with open(dest, "wb") as f:
            shutil.copyfileobj(image.file, f)

    await asyncio.to_thread(_save)

    with locked_conn(request) as conn:
        conn.execute(
            "UPDATE book SET background_image_path = ?, updated_at = ? WHERE id = ?",
            (str(dest), datetime.now(timezone.utc).isoformat(), book_id),
        )
        conn.commit()
        book = repository.get_book(conn, book_id)

        patches = repository.list_patches(conn, book_id)

    # A video background is a plain looping backdrop with no baked-in text, so
    # there are no per-patch overlays to pre-render.
    if not video_gen.is_video_background(dest):
        font_path = settings.default_font_path or None
        for patch in patches:
            try:
                image_overlay.ensure_patch_overlay(book, patch, font_path)
            except Exception:
                pass

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


def _parse_ids(raw: str | None) -> list[int]:
    """Parse a comma-separated list of integer ids, ignoring empty / non-integer tokens."""
    if not raw:
        return []
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out


@router.get("/books/{book_id}/chapters/preview")
def preview_chapters(
    request: Request,
    book_id: int,
    ids: str | None = Query(default=None, description="Comma-separated chapter_index values"),
    preview_chars: int = Query(default=500, ge=1, le=100_000),
):
    """Return a JSON list of {chapter_index, title, char_count, text_excerpt} for the
    requested chapters. Unknown indices are silently skipped."""
    if ids is None or ids.strip() == "":
        raise HTTPException(status_code=400, detail="'ids' query parameter is required")

    indices = _parse_ids(ids)
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        chapters = repository.get_chapters_by_indices(conn, book_id, indices)

    return JSONResponse([
        {
            "chapter_index": ch.chapter_index,
            "title": ch.title,
            "char_count": ch.char_count,
            "text_excerpt": ch.text[:preview_chars],
        }
        for ch in chapters
    ])


@router.get("/books/{book_id}/chapters/{chapter_index}/text", response_class=PlainTextResponse)
def get_chapter_text(request: Request, book_id: int, chapter_index: int):
    """Return the full text of a single chapter as text/plain."""
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        text = repository.get_chapter_text(conn, book_id, chapter_index)
    if text is None:
        raise HTTPException(status_code=404, detail=f"chapter {chapter_index} not found")
    return PlainTextResponse(text)




# ---------------------------------------------------------------------------
# Chapter exclude
# ---------------------------------------------------------------------------


@router.post("/books/{book_id}/chapters/{chapter_index}/exclude")
def toggle_chapter_exclude(
    request: Request,
    book_id: int,
    chapter_index: int,
    excluded: str = Form(default="true"),
):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        repository.set_chapter_excluded(
            conn, book_id, chapter_index, excluded.lower() != "false"
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Replace rules
# ---------------------------------------------------------------------------


def _rule_dict(rule) -> dict:
    return {"id": rule.id, "book_id": rule.book_id, "find": rule.find, "replace": rule.replace,
            "is_regex": rule.is_regex, "position": rule.position}


def _rule_result(request: Request, book_id: int, payload: dict) -> Response:
    """Mutations answer JSON to the SPA (which asks for it) and keep the 303 redirect
    for plain form posts, so nothing that submits these endpoints as a form breaks."""
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse(payload)
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.get("/books/{book_id}/replace-rules")
def list_rules(request: Request, book_id: int):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        rules = repository.list_replace_rules(conn, book_id)
    return JSONResponse([_rule_dict(r) for r in rules])


@router.post("/books/{book_id}/replace-rules")
def create_rule(
    request: Request,
    book_id: int,
    find: str = Form(...),
    replace: str = Form(default=""),
    is_regex: str = Form(default="false"),
    position: int = Form(default=0),
):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        try:
            rule = repository.create_replace_rule(
                conn, book_id, find, replace, is_regex.lower() == "true", position
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        reset = repository.reset_done_patches_for_book(conn, book_id)
    return _rule_result(request, book_id, {"rule": _rule_dict(rule), "reset_patches": reset})


@router.post("/books/{book_id}/replace-rules/{rule_id}/edit")
def edit_rule(
    request: Request,
    book_id: int,
    rule_id: int,
    find: str = Form(...),
    replace: str = Form(default=""),
    is_regex: str = Form(default="false"),
    position: int = Form(default=0),
):
    with locked_conn(request) as conn:
        try:
            updated = repository.update_replace_rule(
                conn, rule_id, find=find, replace=replace,
                is_regex=is_regex.lower() == "true", position=position,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail=f"rule {rule_id} not found")
        reset = repository.reset_done_patches_for_book(conn, book_id)
    return _rule_result(request, book_id, {"rule": _rule_dict(updated), "reset_patches": reset})


@router.post("/books/{book_id}/replace-rules/{rule_id}/delete")
def delete_rule(request: Request, book_id: int, rule_id: int):
    reset = 0
    with locked_conn(request) as conn:
        deleted = repository.delete_replace_rule(conn, rule_id)
        if deleted:
            reset = repository.reset_done_patches_for_book(conn, book_id)
    return _rule_result(request, book_id, {"deleted": deleted, "reset_patches": reset})


# ---------------------------------------------------------------------------
# TTS normalization settings + preview
# ---------------------------------------------------------------------------


@router.post("/books/{book_id}/normalization")
def update_normalization(
    request: Request,
    book_id: int,
    numbers: str = Form(default=""),
    junk: str = Form(default=""),
    spellcheck: str = Form(default=""),
    dictionary: str = Form(default=""),
    transliteration: str = Form(default=""),
    abbreviations: str = Form(default=""),
    breaks: str = Form(default=""),
):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        repository.update_book_normalization(
            conn,
            book_id,
            numbers=numbers.lower() == "on",
            junk=junk.lower() == "on",
            spellcheck=spellcheck.lower() == "on",
            dictionary=dictionary.lower() == "on",
            transliteration=transliteration.lower() == "on",
            abbreviations=abbreviations.lower() == "on",
            breaks=breaks.lower() == "on",
        )
        set_book_group_mode_db(conn, book_id, "normalization", "custom")
        repository.reset_done_patches_for_book(conn, book_id)
    if request.headers.get("X-Requested-With") == "autosave":
        return {"status": "ok"}
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.get("/books/{book_id}/normalization/preview", response_class=PlainTextResponse)
def preview_normalization(
    request: Request,
    book_id: int,
    chapter_index: int = Query(default=0),
):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        text = repository.get_chapter_text(conn, book_id, chapter_index)
        if text is None:
            raise HTTPException(status_code=404, detail=f"chapter {chapter_index} not found")
        opts = get_effective_normalization_options(conn, book)
        normalized = normalize_text(text, opts)
    return PlainTextResponse(normalized)


# ---------------------------------------------------------------------------
# Patch rebuild + preview actions
# ---------------------------------------------------------------------------


@router.post("/books/{book_id}/patches/rebuild")
async def rebuild_patches(request: Request, book_id: int):
    body = await request.json()
    ranges_raw = body.get("ranges", [])
    reset_done = body.get("reset_done", True)
    ranges: list[tuple[int, int]] = []
    for item in ranges_raw:
        if isinstance(item, list) and len(item) == 2:
            ranges.append((item[0], item[1]))
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        try:
            patches = repository.rebuild_patches(conn, book_id, ranges, reset_done)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # No background_gen job is queued here: building patches must not fire
        # off image generation on its own (the Pollinations call is slow and
        # times out). Backgrounds are generated on demand instead.
    return JSONResponse([
        {"patch_index": p.patch_index, "chapter_start": p.chapter_start,
         "chapter_end": p.chapter_end, "name": p.name, "chunk_count": p.chunk_count,
         "status": p.status}
        for p in patches
    ])


@router.post("/books/{book_id}/patches/auto-build")
async def auto_build_patches(
    request: Request,
    book_id: int,
):
    body = await request.form()
    start_chapter_str = body.get("start_chapter")
    end_chapter_str = body.get("end_chapter")
    patch_size_str = body.get("patch_size")
    try:
        start_chapter = int(start_chapter_str) if start_chapter_str else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="start_chapter is required and must be an integer")
    if start_chapter is None:
        raise HTTPException(status_code=400, detail="start_chapter is required")
    end_chapter = None
    if end_chapter_str is not None and end_chapter_str.strip() != "":
        try:
            end_chapter = int(end_chapter_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="end_chapter must be an integer")
    patch_size = None
    if patch_size_str is not None and patch_size_str.strip() != "":
        try:
            patch_size = int(patch_size_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="patch_size must be an integer")

    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        try:
            repository.auto_build_patches(conn, book_id, start_chapter, end_chapter, patch_size)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.get("/books/{book_id}/patches/auto-build/preview")
def preview_auto_build(
    request: Request,
    book_id: int,
    start_chapter: int = Query(...),
    end_chapter: int | None = Query(default=None),
    patch_size: int | None = Query(default=None),
):
    """Return planned patches as JSON without creating them."""
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        try:
            planned = repository.preview_auto_build(
                conn, book_id, start_chapter, end_chapter, patch_size,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"patches": planned})


@router.get("/books/{book_id}/patches/{patch_id}/text", response_class=PlainTextResponse)
def get_patch_text(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        text = repository.build_patch_text(conn, patch)
    return PlainTextResponse(text)


@router.get("/books/{book_id}/patches/{patch_id}/audio")
def get_patch_audio(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        if patch.status != "done" or not patch.audio_path:
            raise HTTPException(status_code=404, detail="audio not available")
        path = patch.audio_path
    return FileResponse(path, media_type="audio/wav")




@router.post("/books/{book_id}/patches/build")
async def patch_builder_submit(request: Request, book_id: int):
    body = await request.form()
    excluded_list = body.getlist("excluded")
    excluded_set = {int(x) for x in excluded_list if x.isdigit()}
    range_starts = body.getlist("range_start")
    range_ends = body.getlist("range_end")
    ranges: list[tuple[int, int]] = []
    for rs, re_ in zip(range_starts, range_ends):
        try:
            s, e = int(rs), int(re_)
            if s <= e:
                ranges.append((s, e))
        except ValueError:
            continue

    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        for ch in repository.list_chapters(conn, book_id):
            new_excluded = ch.chapter_index in excluded_set
            if new_excluded != ch.is_excluded:
                repository.set_chapter_excluded(
                    conn, book_id, ch.chapter_index, new_excluded
                )
        if ranges:
            try:
                repository.rebuild_patches(conn, book_id, ranges, reset_done=True)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.get("/books/{book_id}/youtube-description")
def get_youtube_description(request: Request, book_id: int):
    """Return the enriched YouTube description + tags for the Copy button."""
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail="book not found")
        result = repository.build_youtube_description(conn, book_id)
    return JSONResponse(result)


@router.post("/books/{book_id}/thumbnails/regenerate")
async def regenerate_thumbnails(request: Request, book_id: int):
    body = await request.json()
    patch_ids = body.get("patch_ids", [])
    if not patch_ids:
        raise HTTPException(400, "patch_ids is required")

    from app import image_overlay
    font_path = settings.default_font_path or None

    # Step 1: Gather data outside the lock
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        # Validate all patches exist
        all_patches = repository.list_patches(conn, book_id)
        patch_map = {p.id: p for p in all_patches}
        target_patches = [patch_map[pid] for pid in patch_ids if pid in patch_map]
        invalid_ids = [pid for pid in patch_ids if pid not in patch_map]

    # Step 2: Render outside the lock
    generated = []
    failed = []
    for patch in target_patches:
        try:
            output = image_overlay.ensure_patch_overlay(book, patch, font_path, force=True)
            if not output or not Path(output).is_file():
                raise RuntimeError("Không thể tạo file thumbnail")
            generated.append(patch.id)
        except Exception as e:
            logger.error("Failed to regenerate thumbnail for patch %s: %s", patch.id, e)
            failed.append({"patch_id": patch.id, "error": str(e)})

    # Step 3: Update DB briefly inside lock
    if generated:
        with locked_conn(request) as conn:
            conn.execute(
                f"UPDATE patch_pipeline SET thumbnail_status='pending' WHERE patch_id IN ({','.join(['?'] * len(generated))})",
                generated,
            )
            conn.commit()

    return {"status": "ok", "generated": generated, "failed": failed, "invalid_ids": invalid_ids}






def _list_backgrounds() -> list[dict]:
    """Shared helper: list backgrounds (default + user-uploaded images/videos)."""
    from app.routes.video import ALLOWED_BACKGROUND_EXTENSIONS
    items: list[dict] = []
    default = settings.default_background_image
    if Path(default).exists():
        items.append({"name": "__default__", "path": default, "is_default": True,
                      "is_video": video_gen.is_video_background(default)})
    backgrounds_dir = Path(settings.data_root) / "backgrounds"
    backgrounds_dir.mkdir(parents=True, exist_ok=True)
    for f in sorted(backgrounds_dir.iterdir()):
        if f.suffix.lower() in ALLOWED_BACKGROUND_EXTENSIONS:
            items.append({"name": f.name, "path": str(f), "is_default": False,
                          "is_video": video_gen.is_video_background(f)})
    return items
