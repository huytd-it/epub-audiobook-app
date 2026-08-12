from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sys
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
import sqlite3
import soundfile as sf
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app import audio_merge, drive_export, google_drive, image_overlay, repository, video_gen, video_repository, youtube
from app import db as app_db
from app.chunker import split_into_tts_chunks
from app.config import settings
from app.deps import locked_conn
from app.jobqueue import store
from app.patch_publishing import (confirm_patch_republish, discard_stale_patch_video,
                                  enqueue_patch_publish, enqueue_patch_video,
                                  evaluate_patch_preflight, fetch_thumbnail_inputs,
                                  on_patch_audio_ready, resolve_automation_policy,
                                  run_patch_publish_stage, warm_patch_thumbnail)
from app.youtube_metadata import get_book_youtube_config, get_patch_youtube_override, load_timeline, resolve_patch_youtube_metadata, save_patch_youtube_override, validate_book_youtube_config, validate_timeline
from app.video_config import get_book_video_config
from app.video_integrity import validate_video
from app.video_publish import publish_validated_video

logger = logging.getLogger(__name__)

router = APIRouter()

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4"}
# A patch background may be a still image or a looping video clip.
ALLOWED_BACKGROUND_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | video_gen.VIDEO_BACKGROUND_EXTENSIONS


def _off_lock(request: Request, work):
    """Run `work(conn)` on a connection of its own, leaving the shared one free.

    Publish work renders images with PIL, renders video with ffmpeg, and calls the YouTube
    API (thumbnail upload, paginated playlist lookups). Doing that on the shared connection
    means holding db_lock for minutes, which blocks every other request in the process -
    including the status poll these features depend on.
    """
    shared = request.app.state.conn
    with request.app.state.db_lock:
        database = shared.execute("PRAGMA database_list").fetchone()[2]
    if not database or database == ":memory:":
        # An in-memory database exists only on the shared connection, so there is nothing
        # else to open; tests take this path.
        with request.app.state.db_lock:
            return work(shared)
    own_conn = app_db.connect(database)
    try:
        return work(own_conn)
    finally:
        own_conn.close()


def _advance_and_enqueue(conn, patch_id: int, *, book_id: int | None = None, force_new: bool = False) -> dict:
    """Advance one patch's publish pipeline and queue the upload it created.

    enqueue_patch_publish only seeds/resets the patch_pipeline row - it clears
    youtube_upload_id - so there is nothing to enqueue until run_patch_publish_stage has
    advanced the row far enough to insert the youtube_uploads row. Skipping that step
    parks the patch at stage='upload' forever: the polling UploadWorker that used to
    drive these rows is gone, so the queue job is now the only thing that uploads.
    """
    if force_new:
        enqueue_patch_publish(conn, patch_id, force_new=True)
    pipeline = run_patch_publish_stage(conn, patch_id)
    upload_id = pipeline.get("youtube_upload_id")
    if upload_id:
        store.enqueue(
            conn, "youtube_upload", payload={"upload_id": upload_id}, book_id=book_id,
            dedupe_key=f"youtube_upload:upload={upload_id}",
        )
    return pipeline


def _run_publish_stage(request: Request, patch_id: int, *, book_id: int | None = None, force_new: bool = False) -> dict:
    """Advance one patch's publish pipeline without holding the shared db_lock."""
    return _off_lock(
        request,
        lambda conn: _advance_and_enqueue(conn, patch_id, book_id=book_id, force_new=force_new),
    )


def _warm_thumbnail(request: Request, patch_id: int) -> None:
    """Pre-render the patch thumbnail before a locked block calls on_patch_audio_ready,
    so the PIL render happens off the shared db_lock (see app.patch_publishing)."""
    with locked_conn(request) as conn:
        inputs = fetch_thumbnail_inputs(conn, patch_id)
    warm_patch_thumbnail(inputs)


def _build_or_400(build, *args, **kwargs):
    """Run one of the drive_export.build_* functions, turning its ValueError
    (no text, missing voice reference clip, ...) into a 400 instead of a 500."""
    try:
        return build(*args, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/books/{book_id}/patches/{patch_id}/delete")
def delete_patch(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        repository.delete_patch(conn, patch_id)
    return RedirectResponse(url=f"/books/{book_id}/patches/build", status_code=303)


@router.post("/books/{book_id}/patches/{patch_id}/regenerate")
def regenerate_patch(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        repository.reset_patch(conn, patch_id)
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/patches/{patch_id}/image")
async def upload_patch_image(
    request: Request, book_id: int, patch_id: int,
    image: UploadFile = File(...),
):
    ext = Path(image.filename or "").suffix.lower()
    if ext not in ALLOWED_BACKGROUND_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported background format: {ext}")

    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")

    img_dir = Path(settings.data_root) / "uploads" / str(book_id) / "patches" / str(patch_id)
    img_dir.mkdir(parents=True, exist_ok=True)

    if patch.image_path:
        Path(patch.image_path).unlink(missing_ok=True)

    filename = f"img_{uuid.uuid4().hex[:8]}{ext}"
    dest = img_dir / filename

    # Writing the upload off the lock and off the event loop, same as the MP4 path below.
    def _save() -> None:
        with open(dest, "wb") as f:
            shutil.copyfileobj(image.file, f)

    await asyncio.to_thread(_save)

    with locked_conn(request) as conn:
        repository.save_patch_image(conn, patch_id, str(dest))

    return RedirectResponse(url=f"/books/{book_id}/patches/build", status_code=303)


@router.post("/books/{book_id}/patches/{patch_id}/video")
async def upload_patch_video(
    request: Request, book_id: int, patch_id: int,
    video: UploadFile = File(...),
):
    ext = Path(video.filename or "").suffix.lower()
    if ext not in ALLOWED_VIDEO_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported video format: {ext}")

    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")

    video_dir = Path(settings.data_root) / "books" / str(book_id) / "patch_videos"
    video_dir.mkdir(parents=True, exist_ok=True)
    video_path = video_dir / f"{patch_id}.mp4"
    with open(video_path, "wb") as dest:
        shutil.copyfileobj(video.file, dest)

    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        video_repository.upsert_patch_video(
            conn, book_id=book_id, patch_id=patch_id,
            file_path=str(video_path), resolution=(book.video_resolution if book else None) or "1920x1080",
            filename=f"patch_{book_id}_{patch_id}.mp4",
            original_name=video.filename or f"patch_{patch_id}.mp4",
            title=f"Patch {patch.patch_index + 1}",
            batch_id=f"patch:{book_id}",
            background_path=patch.image_path,
        )

    return RedirectResponse(url=f"/books/{book_id}/patches/build", status_code=303)


@router.post("/books/{book_id}/patches/{patch_id}/image/delete")
def delete_patch_image(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        if patch.image_path:
            Path(patch.image_path).unlink(missing_ok=True)
        repository.clear_patch_image(conn, patch_id)
    return RedirectResponse(url=f"/books/{book_id}/patches/build", status_code=303)


@router.post("/books/{book_id}/patches/{patch_id}/image-type")
def update_image_type(
    request: Request, book_id: int, patch_id: int,
    image_type: str = Form(...),
):
    valid = {"static", "zoom-in", "zoom-out", "pan-left", "pan-right"}
    if image_type not in valid:
        raise HTTPException(status_code=400, detail=f"Invalid image_type: {image_type}")
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        repository.update_patch_image_type(conn, patch_id, image_type)
    return RedirectResponse(url=f"/books/{book_id}/patches/build", status_code=303)


@router.get("/books/{book_id}/patches/{patch_id}/image")
def get_patch_image(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        book = repository.get_book(conn, book_id)
        if patch.image_path and Path(patch.image_path).exists():
            return FileResponse(patch.image_path)
        if book and book.background_image_path and Path(book.background_image_path).exists():
            return FileResponse(book.background_image_path)
        default = settings.default_background_image
        if Path(default).exists():
            return FileResponse(default)
    raise HTTPException(status_code=404, detail="no image available")


def _patch_video_path(book_id: int, patch_id: int) -> Path:
    return Path(settings.data_root) / "books" / str(book_id) / "patch_videos" / f"{patch_id}.mp4"


def _patch_video_title(book, patch) -> str:
    label = patch.name or f"Patch {patch.patch_index + 1}"
    return f"{book.title} - {label}" if book and book.title else label


def _register_patch_video(conn, book, patch, video_path: Path) -> int:
    """Insert (or refresh) a `videos` row for a patch's MP4 so it shows in the
    Video Library and can be handed to the YouTube upload worker. Returns id."""
    record = video_repository.upsert_patch_video(
        conn,
        book_id=book.id,
        patch_id=patch.id,
        file_path=str(video_path),
        resolution=book.video_resolution or "1920x1080",
        filename=f"patch_{book.id}_{patch.id}.mp4",
        original_name=f"{_patch_video_title(book, patch)}.mp4",
        title=_patch_video_title(book, patch),
        batch_id=f"patch:{book.id}",
        background_path=patch.image_path,
    )
    return record["id"]


def _wants_json(request: Request, ajax: int) -> bool:
    return bool(ajax) or "application/json" in (request.headers.get("accept") or "")


def _safe_batch_path(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _resolve_batch_result(patch_folder: Path, patch_id: int) -> Path | None:
    root = patch_folder.resolve()
    for parent in [root, *root.parents]:
        manifest_path = parent / "batch_manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = next((item for item in manifest.get("patches", []) if item.get("patch_id") == patch_id), None)
            if not entry or not isinstance(entry.get("result_wav"), str):
                return None
            result = _safe_batch_path(parent, entry["result_wav"])
            patch_manifest = _safe_batch_path(parent, str(entry.get("folder", "")) + "/manifest.json")
            return result if result and patch_manifest and patch_manifest.is_file() else None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
    return None


def _build_import_timeline(chunk_paths: list[Path], metadata: list[dict], pause_ms: int) -> dict | None:
    if not chunk_paths or len(chunk_paths) != len(metadata):
        return None
    try:
        infos = [sf.info(str(path)) for path in chunk_paths]
        rate = infos[0].samplerate
        # Chunks pair with chunk_paths by position - both are built from the same
        # chunk_NNN ordering - so the metadata carries no filename of its own.
        keys = {"chapter_index", "chapter_title", "is_chapter_start"}
        if any(set(item) != keys or info.samplerate != rate or info.channels != infos[0].channels
               for info, item in zip(infos, metadata)):
            return None
        pause = round(rate * pause_ms / 1000)
        starts, chapters = [], []
        frame = 0
        previous_index = None
        for index, (info, item) in enumerate(zip(infos, metadata)):
            chapter_index = item["chapter_index"]
            title = item["chapter_title"]
            marker = item["is_chapter_start"]
            if (isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or
                    (previous_index is not None and chapter_index <= previous_index) or
                    not isinstance(title, str) or not title.strip() or not isinstance(marker, bool) or
                    (index == 0 and not marker) or
                    (index > 0 and marker != (chapter_index != previous_index))):
                return None
            previous_index = chapter_index
            starts.append(frame)
            if marker:
                chapters.append({"chapter_index": chapter_index, "start_frame": frame,
                                 "start_seconds": frame / rate, "title": title.strip()})
            frame += info.frames + pause
        total_frames = frame - pause
        if any(b - a < rate * 10 for a, b in zip(starts, starts[1:])) or total_frames - starts[-1] < rate * 10:
            return None
        return {"version": 1, "sample_rate": rate, "total_frames": total_frames, "chapters": chapters}
    except (OSError, TypeError, ValueError, KeyError, sf.SoundFileError):
        return None


def _timeline_metadata(manifest: dict) -> list[dict]:
    """Reduce a patch manifest's chunk_metadata to the three fields
    _build_import_timeline validates.

    Current exports are compact: entries carry no chapter_title (titles are
    de-duplicated into the chapter_titles map) and no filename. Older packages carry
    both, plus the chunk text - dropping the extras here is what lets them import with
    a chapter timeline too."""
    titles = manifest.get("chapter_titles") or {}
    metadata = []
    for item in manifest.get("chunk_metadata") or []:
        if not isinstance(item, dict):
            return []
        title = item.get("chapter_title")
        if not isinstance(title, str) or not title.strip():
            title = titles.get(str(item.get("chapter_index")))
        metadata.append({
            "chapter_index": item.get("chapter_index"),
            "chapter_title": title,
            "is_chapter_start": item.get("is_chapter_start"),
        })
    return metadata


def _atomic_copy(source: Path, target: Path) -> None:
    shutil.copy2(source, target)


def _install_imported_wav(source: Path, audio_path: Path, timeline: dict | None = None) -> None:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    local_sidecar = audio_path.with_suffix(".timeline.json")
    temp_wav = audio_path.with_name(f".{audio_path.name}.{uuid.uuid4().hex}.tmp")
    temp_sidecar = local_sidecar.with_name(f".{local_sidecar.name}.{uuid.uuid4().hex}.tmp")
    try:
        sf.info(str(source))
        _atomic_copy(source, temp_wav)
        if timeline is None:
            timeline = load_timeline(source)
        if timeline is not None:
            temp_sidecar.write_text(json.dumps(timeline), encoding="utf-8")
        os.replace(temp_wav, audio_path)
        if timeline is not None:
            try:
                os.replace(temp_sidecar, local_sidecar)
            except OSError:
                logger.warning("Timeline persistence failed after local install", exc_info=True)
                try:
                    local_sidecar.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove stale timeline sidecar %s", local_sidecar, exc_info=True)
        else:
            try:
                local_sidecar.unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to remove stale timeline sidecar %s", local_sidecar, exc_info=True)
    except Exception:
        temp_wav.unlink(missing_ok=True)
        temp_sidecar.unlink(missing_ok=True)
        raise
    finally:
        for path in (temp_wav, temp_sidecar):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to clean import staging path %s", path, exc_info=True)


@router.post("/books/{book_id}/patches/{patch_id}/generate-video")
async def generate_patch_video(
    request: Request, book_id: int, patch_id: int,
    upload_youtube: bool = Form(default=False),
    privacy: str = Form(default=""),
    ajax: int = Query(default=0),
):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        if patch.status != "done" or not patch.audio_path:
            raise HTTPException(status_code=400, detail="Patch audio not ready")
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        music_path = None
        if book.music_id is not None:
            music = repository.get_music(conn, book.music_id)
            if music and Path(music.file_path).exists():
                music_path = music.file_path
        video_config = get_book_video_config(conn, book)

    fallback_bg = video_gen.resolve_patch_image(patch, book, settings.default_background_image)
    raw_bg = video_gen.resolve_configured_patch_image(patch, video_config, fallback_bg or "")
    if not raw_bg:
        raise HTTPException(status_code=400, detail="No background image available")

    dedupe_key = f"patch_video:patch={patch_id}"
    with locked_conn(request) as conn:
        existing = store.find_live_by_dedupe(conn, dedupe_key)
        job_id = existing.id if existing else store.enqueue(
            conn, "patch_video",
            payload={"patch_id": patch_id, "upload_youtube": upload_youtube, "privacy": privacy},
            book_id=book_id, dedupe_key=dedupe_key,
        )
    if job_id is None:
        raise HTTPException(status_code=500, detail="Could not enqueue patch video")
    if _wants_json(request, ajax):
        return JSONResponse({
            "status": "queued", "job_id": job_id,
            "deduplicated": existing is not None,
        }, status_code=202)
    return RedirectResponse(url=f"/books/{book_id}/patches/build", status_code=303)


@router.post("/books/{book_id}/patches/{patch_id}/video/delete")
def delete_patch_video(
    request: Request, book_id: int, patch_id: int,
    ajax: int = Query(default=0),
):
    """Remove a patch's rendered MP4: the file, its Video Library row, and the
    publish pipeline's pointer at it. An upload that already reached YouTube is
    left alone - only the local artefact goes away, so the row keeps its
    history and the pipeline isn't rewound past the upload."""
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")

    video_path = _patch_video_path(book_id, patch_id)
    with locked_conn(request) as conn:
        row = conn.execute("SELECT id FROM videos WHERE file_path = ?", (str(video_path),)).fetchone()
        if row:
            video_repository.delete_video(conn, row["id"])
        video_path.unlink(missing_ok=True)

        pipeline = conn.execute(
            "SELECT upload_status FROM patch_pipeline WHERE patch_id = ?", (patch_id,)
        ).fetchone()
        if pipeline:
            if pipeline["upload_status"] == "done":
                conn.execute(
                    "UPDATE patch_pipeline SET video_id = NULL, video_path = NULL, "
                    "updated_at = ? WHERE patch_id = ?",
                    (datetime.now(timezone.utc).isoformat(), patch_id),
                )
            else:
                conn.execute(
                    "UPDATE patch_pipeline SET stage = 'video', video_status = 'pending', "
                    "video_id = NULL, video_path = NULL, updated_at = ? WHERE patch_id = ?",
                    (datetime.now(timezone.utc).isoformat(), patch_id),
                )
            conn.commit()

    if _wants_json(request, ajax):
        return JSONResponse({"status": "deleted"})
    return RedirectResponse(url=f"/books/{book_id}/patches/build", status_code=303)


@router.post("/books/{book_id}/patches/{patch_id}/youtube-upload")
def upload_patch_video_to_youtube(
    request: Request, book_id: int, patch_id: int,
    privacy: str = Form(default=""),
    force_new: bool = Form(default=False),
):
    """Push a patch's already-generated MP4 (server-rendered or uploaded from
    Colab/Kaggle) to YouTube via the upload worker. Returns JSON."""
    video_path = _patch_video_path(book_id, patch_id)
    if not video_path.exists():
        raise HTTPException(status_code=400, detail="Chưa có video cho patch này")
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        existing = conn.execute("SELECT * FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone()
        if existing and not force_new:
            if existing["stage"] == "published":
                return JSONResponse({"status": "skipped", "reason": "already_published",
                    "detail": "Video đã được publish; video YouTube cũ sẽ không bị thay thế.",
                    "can_force_new": True, "pipeline": dict(existing)})
            if existing["upload_status"] in {"claiming", "queued", "pending", "uploading"} and existing["youtube_upload_id"]:
                return JSONResponse({"status": "skipped", "reason": "upload_in_progress",
                    "detail": "Video đang được xếp hàng hoặc upload lên YouTube.",
                    "can_force_new": False, "pipeline": dict(existing)})
        if force_new and existing and existing["upload_status"] in {"claiming", "queued", "pending", "uploading"} and existing["stage"] != "published":
            raise HTTPException(status_code=409, detail="YouTube upload is already active")
        video_db_id = _register_patch_video(conn, book, patch, video_path)

    with locked_conn(request) as conn:
        from app.patch_publishing import seed_patch_video
        if force_new:
            enqueue_patch_publish(conn, patch_id, force_new=True)
        seed_patch_video(conn, patch_id, video_db_id, str(video_path))
    status = _run_publish_stage(request, patch_id, book_id=book_id)
    return JSONResponse({"status": "queued", "reason": None, "can_force_new": False, "pipeline": status})


@router.get("/books/{book_id}/patches/{patch_id}/overlay-image")
def get_patch_overlay_image(request: Request, book_id: int, patch_id: int):
    """Render (idempotent, cached) and serve the per-patch overlay PNG
    (background + "Book - Patch" text). Powers the row thumbnail, the lightbox
    preview, download, and the batch "generate images" action."""
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")

    force = request.query_params.get("force") in {"1", "true"}
    overlay = image_overlay.ensure_patch_overlay(
        book, patch, settings.default_font_path or None,
        background_path=patch.image_path or None,
        force=force,
    )
    if overlay and Path(overlay).exists():
        return FileResponse(str(overlay), media_type="image/png")
    # Fall back to the raw patch/book background so the row still shows something.
    fallback = video_gen.resolve_patch_image(patch, book, settings.default_background_image)
    if fallback and Path(fallback).exists():
        return FileResponse(str(fallback))
    raise HTTPException(status_code=404, detail="Chưa có ảnh nền để tạo overlay")


@router.get("/books/{book_id}/patches/{patch_id}/video")
def get_patch_video(request: Request, book_id: int, patch_id: int):
    video_path = _patch_video_path(book_id, patch_id)
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not generated yet")
    return FileResponse(str(video_path), media_type="video/mp4")


# ---------------------------------------------------------------------------
# Chunk manager: per-chunk view, max_chars override, resume-from-chunk,
# Google Drive export/import for Colab/Kaggle synthesis.
# ---------------------------------------------------------------------------




@router.post("/books/{book_id}/patches/{patch_id}/max_chars")
def update_patch_max_chars(
    request: Request, book_id: int, patch_id: int,
    max_chars: str = Form(default=""),
):
    value: int | None = None
    if max_chars.strip():
        try:
            value = int(max_chars)
        except ValueError:
            raise HTTPException(status_code=400, detail="max_chars must be an integer")
        if value < 1:
            raise HTTPException(status_code=400, detail="max_chars must be >= 1")
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        ok = repository.set_patch_max_chars(conn, patch_id, value)
    if not ok:
        raise HTTPException(status_code=400, detail="max_chars can only be changed while the patch is pending")
    return RedirectResponse(url=f"/books/{book_id}/patches/{patch_id}/chunks", status_code=303)


@router.post("/books/{book_id}/patches/{patch_id}/resume_from_chunk")
def resume_patch_from_chunk(
    request: Request, book_id: int, patch_id: int,
    from_index: int = Form(...),
):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        ok = repository.resume_patch_from_chunk(conn, patch_id, from_index)
    if not ok:
        raise HTTPException(status_code=400, detail="patch must be 'failed' to resume from a chunk")
    return RedirectResponse(url=f"/books/{book_id}/patches/{patch_id}/chunks", status_code=303)


def _load_batch_patches(conn, book_id: int, patch_ids: list[int]):
    """Validate a multi-patch export selection and return (book, patches sorted by
    patch_index). Raises HTTPException on empty/unknown/processing selections."""
    if not patch_ids:
        raise HTTPException(status_code=400, detail="no patches selected")
    book = repository.get_book(conn, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="book not found")
    patches = []
    for patch_id in dict.fromkeys(patch_ids):  # dedupe, keep order
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail=f"patch {patch_id} not found")
        if patch.status == "processing":
            raise HTTPException(
                status_code=400,
                detail=f"cannot export patch {patch.name or patch.patch_index} while it is processing",
            )
        patches.append(patch)
    return book, sorted(patches, key=lambda p: p.patch_index)


@router.post("/books/{book_id}/patches/export-batch/download")
def download_batch_export(
    request: Request, book_id: int, patch_ids: list[int] = Form(...),
    model_id: str = Form("voxcpm2"), voice_id: str = Form(""), max_chars: int = Form(0),
    with_effects: int = Form(0),
):
    with locked_conn(request) as conn:
        book, patches = _load_batch_patches(conn, book_id, patch_ids)
        # Compute the timestamped batch name once and bake it into the notebook so
        # its fallback matches the zip filename.
        folder_name = drive_export.folder_name_for_batch(book.title, patches)
        zip_path = _build_or_400(
            drive_export.build_batch_export_zip,
            conn, patches, drive_folder_name=folder_name, hf_token=settings.hf_token,
            model_id=model_id, voice_id=voice_id or None, max_chars=max_chars,
            with_effects=bool(with_effects),
        )
    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename=f"{folder_name}.zip",
    )


@router.post("/books/{book_id}/patches/export-batch")
def export_batch_to_drive(
    request: Request, book_id: int, patch_ids: list[int] = Form(...), sync_target_id: int = Form(...),
    model_id: str = Form("voxcpm2"), voice_id: str = Form(""), max_chars: int = Form(0),
    with_effects: int = Form(0),
):
    with locked_conn(request) as conn:
        book, patches = _load_batch_patches(conn, book_id, patch_ids)
        target = repository.get_drive_sync_target(conn, sync_target_id)
        if target is None:
            raise HTTPException(status_code=400, detail="Sync target not found")

        folder_name = drive_export.folder_name_for_batch(book.title, patches)
        package_dir, batch_manifest = _build_or_400(
            drive_export.build_batch_export_package,
            conn, patches, drive_folder_name=folder_name, hf_token=settings.hf_token,
            model_id=model_id, voice_id=voice_id or None, max_chars=max_chars,
            with_effects=bool(with_effects),
        )
        try:
            batch_folder = drive_export.publish_package(package_dir, target["folder_path"], folder_name)
            for entry in batch_manifest["patches"]:
                patch_folder = batch_folder / entry["folder"]
                repository.create_patch_export(
                    conn, entry["patch_id"], str(patch_folder), str(patch_folder), entry["chunk_count"],
                    sync_target_id=target["id"], local_folder_path=str(patch_folder), commit=False,
                )
            conn.commit()
        except Exception as exc:
            logger.exception("batch export to Google Drive failed for book %s", book_id)
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Drive Desktop export failed: {exc}")
        finally:
            shutil.rmtree(package_dir, ignore_errors=True)

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/patches/export-batch-api")
def export_batch_to_drive_api(
    request: Request, book_id: int, patch_ids: list[int] = Form(...), account_id: int = Form(...),
    model_id: str = Form("voxcpm2"), voice_id: str = Form(""), max_chars: int = Form(0),
    with_effects: int = Form(0),
):
    """Upload the batch package to Google Drive via the Drive API (drive.file scope) so
    the Kaggle notebook can use it. This is the API counterpart of export_batch_to_drive,
    which copies into a local Google Drive Desktop folder - files that arrive on Drive
    that way (or via rclone / manual upload) are invisible to the drive.file scope the
    Kaggle GDRIVE_CREDS secret uses, so Kaggle could never find them. Uploading through
    the app's own API makes the batch (and every result the notebook pushes back) visible
    to those same credentials.

    The account chosen here MUST be the one whose "Copy Kaggle credentials" JSON is stored
    in the Kaggle GDRIVE_CREDS secret: drive.file only reveals files created by that exact
    account."""
    with locked_conn(request) as conn:
        book, patches = _load_batch_patches(conn, book_id, patch_ids)
        if google_drive.get_account(conn, account_id) is None:
            raise HTTPException(status_code=400, detail="Google Drive account not found")

        folder_name = drive_export.folder_name_for_batch(book.title, patches)
        package_dir, batch_manifest = _build_or_400(
            drive_export.build_batch_export_package,
            conn, patches, drive_folder_name=folder_name, hf_token=settings.hf_token,
            model_id=model_id, voice_id=voice_id or None, max_chars=max_chars,
            with_effects=bool(with_effects),
        )
        try:
            service = google_drive.get_drive_service(conn, account_id)
            root_id = google_drive.get_or_create_root_folder(service)
            batch_folder = google_drive.create_folder(service, folder_name, parent_id=root_id)
            folder_map = google_drive.upload_directory(service, batch_folder["id"], str(package_dir))
            for entry in batch_manifest["patches"]:
                sub = folder_map.get(entry["folder"], batch_folder)
                repository.create_patch_export(
                    conn, entry["patch_id"], sub["id"], sub["link"], entry["chunk_count"],
                    drive_account_id=account_id, commit=False,
                )
            conn.commit()
        except HTTPException:
            conn.rollback()
            raise
        except Exception as exc:
            logger.exception("batch export to Google Drive API failed for book %s", book_id)
            conn.rollback()
            raise HTTPException(status_code=500, detail=f"Drive API export failed: {exc}")
        finally:
            shutil.rmtree(package_dir, ignore_errors=True)

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/patches/{patch_id}/import")
def import_patch_from_drive(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        if patch.status == "processing":
            raise HTTPException(status_code=400, detail="cannot import while the patch is processing")
        export = repository.get_latest_patch_export(conn, patch_id)
        if export is None:
            raise HTTPException(status_code=400, detail="this patch has never been exported")
        if not export.local_folder_path:
            raise HTTPException(status_code=400, detail="Legacy Drive API export: export again through Google Drive Desktop or upload result files manually")
        package_folder = Path(export.local_folder_path)
        if not package_folder.is_dir():
            raise HTTPException(status_code=400, detail="Export folder is unavailable; check Google Drive Desktop or export again")

        plan_inputs = repository.fetch_patch_chunk_inputs(conn, patch)

    expected_chunk_count = len(repository.build_chunk_plan_from_inputs(plan_inputs))

    chunk_dir = Path(settings.data_root) / "books" / str(book_id) / "patches" / f"{patch_id}_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    # Imported chunks are not LightTTS output — invalidate its reuse marker.
    (chunk_dir / ".light_tts_meta").unlink(missing_ok=True)

    # Copying and merging the chunk WAVs is hundreds of MB of disk I/O. It runs outside
    # locked_conn so the shared connection - and therefore every other request - stays
    # free; only the short status writes below take the lock.
    try:
        batch_root = package_folder
        while not (batch_root / "batch_manifest.json").is_file() and batch_root != batch_root.parent:
            batch_root = batch_root.parent
        manifest_path = batch_root / "batch_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else None
        entry = next((item for item in (manifest or {}).get("patches", []) if item.get("patch_id") == patch_id), None)
        result = _resolve_batch_result(package_folder, patch_id)
        audio_path = Path(settings.data_root) / "books" / str(book_id) / "patches" / f"{patch_id}.wav"
        if result and result.is_file():
            installed = False
            try:
                sf.info(str(result))
                _install_imported_wav(result, audio_path)
                installed = True
            except Exception:
                logger.warning("batch result WAV invalid for patch %s; falling back to chunks", patch_id, exc_info=True)
            if installed:
                _warm_thumbnail(request, patch_id)
                with locked_conn(request) as conn:
                    repository.mark_patch_done(conn, patch_id, str(audio_path))
                    on_patch_audio_ready(conn, patch_id)
                    repository.update_patch_export(conn, export.id, status="imported", imported_chunk_count=expected_chunk_count)
                return RedirectResponse(url=f"/books/{book_id}/patches/{patch_id}/chunks", status_code=303)
        patch_folder = _safe_batch_path(batch_root, entry.get("folder", "")) if entry else package_folder
        chunk_source_dir = patch_folder / "output" if patch_folder else package_folder / "output"
        if not chunk_source_dir.is_dir():
            chunk_source_dir = package_folder / "output"
        if not chunk_source_dir.is_dir():
            chunk_source_dir = package_folder

        imported = 0
        expected_info = None
        for i in range(expected_chunk_count):
            name = f"chunk_{i:03d}.wav"
            local_path = chunk_dir / name
            if local_path.exists():
                info = sf.info(str(local_path))
                if expected_info is None:
                    expected_info = (info.samplerate, info.channels)
                elif (info.samplerate, info.channels) != expected_info:
                    raise ValueError("chunk samplerate/channels mismatch")
                imported += 1
                continue
            source_path = _safe_batch_path(batch_root, str(chunk_source_dir.relative_to(batch_root) / name)) if chunk_source_dir.is_relative_to(batch_root) else None
            if source_path is None or not source_path.is_file():
                break  # first missing chunk: stop here, contiguous prefix ends
            info = sf.info(str(source_path))
            if expected_info is None:
                expected_info = (info.samplerate, info.channels)
            elif (info.samplerate, info.channels) != expected_info:
                raise ValueError("chunk samplerate/channels mismatch")
            shutil.copy2(source_path, local_path)
            imported += 1

        if imported >= expected_chunk_count:
            book_dir = Path(settings.data_root) / "books" / str(book_id) / "patches"
            audio_path = Path(book_dir / f"{patch_id}.wav")
            chunk_paths = [str(chunk_dir / f"chunk_{i:03d}.wav") for i in range(expected_chunk_count)]
            temp_audio = audio_path.with_name(f".{audio_path.stem}.{uuid.uuid4().hex}.tmp.wav")
            try:
                audio_merge.concat_wavs(chunk_paths, str(temp_audio), pause_ms=300)
                metadata = []
                patch_manifest = (patch_folder or package_folder) / "manifest.json"
                if patch_manifest.is_file():
                    metadata = _timeline_metadata(json.loads(patch_manifest.read_text(encoding="utf-8")))
                timeline = _build_import_timeline([Path(p) for p in chunk_paths], metadata, 300)
                _install_imported_wav(temp_audio, audio_path, timeline)
            finally:
                temp_audio.unlink(missing_ok=True)
            # Chunk files (downloaded from Drive) are intentionally kept on disk, same as
            # the local synthesis path in worker.py - not auto-deleted after merge.
            _warm_thumbnail(request, patch_id)
            with locked_conn(request) as conn:
                repository.mark_patch_done(conn, patch_id, str(audio_path))
                on_patch_audio_ready(conn, patch_id)
                repository.update_patch_export(conn, export.id, status="imported", imported_chunk_count=imported)
        else:
            with locked_conn(request) as conn:
                repository.update_patch_chunk_progress(conn, patch_id, imported)
                repository.update_patch_export(conn, export.id, status="partially_imported", imported_chunk_count=imported)
    except Exception as exc:
        logger.exception("import from Google Drive Desktop failed for patch %s", patch_id)
        with locked_conn(request) as conn:
            repository.update_patch_export(conn, export.id, status="failed", error_message=str(exc))
        raise HTTPException(status_code=500, detail=f"Drive Desktop import failed: {exc}")

    return RedirectResponse(url=f"/books/{book_id}/patches/{patch_id}/chunks", status_code=303)


@router.post("/books/{book_id}/patches/{patch_id}/background")
async def set_patch_background(
    request: Request,
    book_id: int,
    patch_id: int,
    background_path: str = Form(default=""),
):
    """Set patch background to an existing library path (empty = clear to book default)."""
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        path = background_path.strip() or None
        if path:
            from app.routes.books import _list_backgrounds
            allowed = {item["path"] for item in _list_backgrounds()}
            if path not in allowed:
                raise HTTPException(status_code=400, detail="unknown background path")
        repository.save_patch_image(conn, patch_id, path)
    return JSONResponse({"ok": True, "patch_id": patch_id})


@router.post("/books/{book_id}/patches/{patch_id}/upload-audio")
async def upload_patch_audio(
    request: Request,
    book_id: int,
    patch_id: int,
    audio: UploadFile = File(...),
):
    """Upload a completed audio file for a patch and mark it as done."""
    ext = Path(audio.filename or "").suffix.lower()
    if ext not in {".wav", ".mp3", ".ogg", ".flac"}:
        raise HTTPException(status_code=400, detail=f"Unsupported audio format: {ext}")

    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")

    audio_dir = Path(settings.data_root) / "books" / str(book_id) / "patches"
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / f"{patch_id}.wav"
    with open(audio_path, "wb") as dest:
        shutil.copyfileobj(audio.file, dest)

    _warm_thumbnail(request, patch_id)
    with locked_conn(request) as conn:
        repository.mark_patch_done(conn, patch_id, str(audio_path))
        on_patch_audio_ready(conn, patch_id)

    return JSONResponse({"ok": True, "patch_id": patch_id})


def _youtube_patch(conn, book_id, patch_id):
    book = repository.get_book(conn, book_id)
    patch = repository.get_patch(conn, patch_id)
    if not book or not patch or patch.book_id != book_id:
        raise HTTPException(404, "patch not found")
    return book, patch


@router.get("/books/{book_id}/youtube-metadata-preview")
def youtube_metadata_preview(request: Request, book_id: int, patch_id: int | None = None):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        patch = repository.get_patch(conn, patch_id) if patch_id else next(iter(repository.list_patches(conn, book_id)), None)
        if not book or not patch or patch.book_id != book_id:
            raise HTTPException(404, "patch not found")
        return resolve_patch_youtube_metadata(
            book, patch, get_patch_youtube_override(conn, patch.id),
            repository.build_patch_metadata_context(conn, book, patch))


@router.post("/books/{book_id}/youtube-metadata-preview")
async def youtube_metadata_preview_draft(request: Request, book_id: int):
    """Resolve the rendered title/description/tags from a draft config without saving it."""
    data = await request.json()
    config = data.get("config")
    patch_id = data.get("patch_id")
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        patch = repository.get_patch(conn, patch_id) if patch_id else next(iter(repository.list_patches(conn, book_id)), None)
        if not patch or patch.book_id != book_id:
            last_patch = next(iter(repository.list_patches(conn, book_id)), None)
            if not last_patch:
                raise HTTPException(400, "book has no patches to preview against")
            patch = last_patch
        try:
            validated = validate_book_youtube_config(config) if config is not None else None
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if validated is None:
            validated = get_book_youtube_config(conn, book_id)
        try:
            return resolve_patch_youtube_metadata(
                book, patch, get_patch_youtube_override(conn, patch.id),
                repository.build_patch_metadata_context(conn, book, patch), config=validated)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.get("/books/{book_id}/patches/{patch_id}/youtube-metadata")
def get_youtube_metadata(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        book, patch = _youtube_patch(conn, book_id, patch_id)
        override = get_patch_youtube_override(conn, patch_id)
        pipeline = conn.execute("SELECT stage,last_error,thumbnail_path,video_path,thumbnail_status,video_status,upload_status,playlist_status FROM patch_pipeline WHERE patch_id = ?", (patch_id,)).fetchone()
        context = repository.build_patch_metadata_context(conn, book, patch)
        return {"metadata": resolve_patch_youtube_metadata(book, patch, override, context), "override": override, "pipeline": dict(pipeline) if pipeline else {}}


@router.post("/books/{book_id}/patches/{patch_id}/youtube-metadata")
async def save_youtube_metadata(request: Request, book_id: int, patch_id: int):
    data = await request.json()
    with locked_conn(request) as conn:
        book, patch = _youtube_patch(conn, book_id, patch_id)
        try:
            save_patch_youtube_override(conn, patch_id, data)
            return resolve_patch_youtube_metadata(
                book, patch, data, repository.build_patch_metadata_context(conn, book, patch))
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@router.post("/books/{book_id}/patches/{patch_id}/publish")
async def publish_patch(request: Request, book_id: int, patch_id: int):
    data = await request.json()
    with locked_conn(request) as conn:
        book, patch = _youtube_patch(conn, book_id, patch_id)
        if not youtube.is_configured() or not youtube.get_creds_from_db(conn):
            raise HTTPException(400, "YouTube connection is required")
        if getattr(request.app.state, "job_queue", None) is None:
            raise HTTPException(503, "Upload worker is unavailable")
        override = {k: v for k, v in data.items() if k != "force_new"}
        if any(k in {"title", "description", "genre_tags", "tags", "privacy_status", "playlist"} for k in override):
            save_patch_youtube_override(conn, patch_id, override)
        effective_override = get_patch_youtube_override(conn, patch_id)
        metadata = resolve_patch_youtube_metadata(
            book, patch, effective_override, repository.build_patch_metadata_context(conn, book, patch))
    force_new = bool(data.get("force_new"))
    # The publish stage renders the thumbnail with PIL (and the video if it is still
    # missing), so it goes off the shared lock and off the event loop like every other
    # slow publish step.
    pipeline = await asyncio.to_thread(
        _off_lock, request,
        lambda conn: _advance_and_enqueue(conn, patch_id, book_id=book_id, force_new=force_new),
    )
    return {"metadata": metadata, "pipeline": pipeline}


@router.post("/books/{book_id}/patches/{patch_id}/publish/retry")
def retry_publish_patch(request: Request, book_id: int, patch_id: int, force_new: bool = False):
    with locked_conn(request) as conn:
        _youtube_patch(conn, book_id, patch_id)
    return _run_publish_stage(request, patch_id, book_id=book_id, force_new=force_new)


@router.post("/books/{book_id}/patches/{patch_id}/republish-confirm")
def republish_confirm(request: Request, book_id: int, patch_id: int):
    """Xác nhận publish lại một patch đã publish: audio mới được chấp nhận, pipeline
    được reset để dựng video mới + upload — lịch sử upload cũ giữ nguyên."""
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        try:
            pipeline = confirm_patch_republish(conn, patch_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        automation = enqueue_patch_video(conn, patch_id, force_new=True)
    return {"pipeline": pipeline, "automation": automation}


@router.post("/books/{book_id}/patches/{patch_id}/import-local")
async def import_patch_from_upload(
    request: Request,
    book_id: int,
    patch_id: int,
    files: list[UploadFile] = File(...),
):
    """Import synthesized audio from uploaded files - no Google Drive connection needed.

    Accepts either the individual chunk_NNN.wav files, or a single .zip containing them
    (e.g. what you'd download after running the notebook on another Google account). Used
    for the fully-offline round trip: download package locally -> run on any Colab/Kaggle
    account -> upload the resulting .wav files back here.
    """
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        if patch.status == "processing":
            raise HTTPException(status_code=400, detail="cannot import while the patch is processing")

        plan_inputs = repository.fetch_patch_chunk_inputs(conn, patch)

    expected_chunk_count = len(
        await asyncio.to_thread(repository.build_chunk_plan_from_inputs, plan_inputs)
    )

    chunk_dir = Path(settings.data_root) / "books" / str(book_id) / "patches" / f"{patch_id}_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    # Uploaded chunks are not LightTTS output — invalidate its reuse marker.
    (chunk_dir / ".light_tts_meta").unlink(missing_ok=True)

    # Unzipping and merging the uploads is heavy disk I/O, so it stays outside
    # locked_conn: holding the shared connection through it would block every other
    # request for the whole import.
    def _import_chunks() -> tuple[int, Path | None]:
        # Pull every chunk_NNN.wav out of the uploads (loose .wav files and/or .zip archives)
        # and drop them into the chunk dir, keeping only names we actually expect.
        wanted = {f"chunk_{i:03d}.wav" for i in range(expected_chunk_count)}
        saved = 0
        for upload in files:
            name = Path(upload.filename or "").name
            if name.lower().endswith(".zip"):
                tmp_zip = chunk_dir / f".upload_{uuid.uuid4().hex[:8]}.zip"
                try:
                    with open(tmp_zip, "wb") as out:
                        shutil.copyfileobj(upload.file, out)
                    with zipfile.ZipFile(tmp_zip) as zf:
                        for member in zf.namelist():
                            base = Path(member).name
                            if base in wanted:
                                with zf.open(member) as src, open(chunk_dir / base, "wb") as dst:
                                    shutil.copyfileobj(src, dst)
                                saved += 1
                finally:
                    tmp_zip.unlink(missing_ok=True)
            elif name in wanted:
                with open(chunk_dir / name, "wb") as out:
                    shutil.copyfileobj(upload.file, out)
                saved += 1

        if saved == 0:
            raise HTTPException(
                status_code=400,
                detail="no matching chunk_NNN.wav files found in the upload",
            )

        # Same contiguous-prefix logic as the Drive import: count how many chunks we have
        # in order, merge into the patch WAV if the whole set is present, else just record
        # progress so the local worker (or another upload) can finish the rest.
        imported = 0
        for i in range(expected_chunk_count):
            if (chunk_dir / f"chunk_{i:03d}.wav").exists():
                imported += 1
            else:
                break

        if imported < expected_chunk_count:
            return imported, None

        book_dir = Path(settings.data_root) / "books" / str(book_id) / "patches"
        audio_path = Path(book_dir / f"{patch_id}.wav")
        chunk_paths = [str(chunk_dir / f"chunk_{i:03d}.wav") for i in range(expected_chunk_count)]
        temp_audio = audio_path.with_name(f".{audio_path.stem}.{uuid.uuid4().hex}.wav")
        try:
            audio_merge.concat_wavs(chunk_paths, str(temp_audio), pause_ms=300)
            _install_imported_wav(temp_audio, audio_path, None)
        finally:
            temp_audio.unlink(missing_ok=True)
        return imported, audio_path

    imported, audio_path = await asyncio.to_thread(_import_chunks)

    if audio_path is not None:
        await asyncio.to_thread(_warm_thumbnail, request, patch_id)

    with locked_conn(request) as conn:
        if audio_path is not None:
            repository.mark_patch_done(conn, patch_id, str(audio_path))
            on_patch_audio_ready(conn, patch_id)
        else:
            repository.update_patch_chunk_progress(conn, patch_id, imported)

    return RedirectResponse(url=f"/books/{book_id}/patches/{patch_id}/chunks", status_code=303)


# drive_export names a batch's merged files "<patch_index:03d> - <patch name>.wav"
# (plus a matching .timeline.json), so the leading number is the only part that
# identifies the patch - the name half is free text the user may have renamed around.
_RESULT_INDEX_RE = re.compile(r"^\s*(\d{1,4})(?!\d)")
_RESULT_BOOK_EPISODE_RE = re.compile(r"^\s*(\d+)_(\d{1,4})(?!\d)")


def _result_patch_index(filename: str, book_id: int | None = None) -> int | None:
    """Resolve a zero-based patch index from either legacy ``NNN - name`` files or
    the canonical ``<book_id>_<episode>`` name (episode is one-based)."""
    name = Path(filename).name
    canonical = _RESULT_BOOK_EPISODE_RE.match(name)
    if canonical:
        if book_id is not None and int(canonical.group(1)) != book_id:
            return None
        episode = int(canonical.group(2))
        return episode - 1 if episode > 0 else None
    match = _RESULT_INDEX_RE.match(name)
    return int(match.group(1)) if match else None


def _write_timeline_sidecar(audio_path: Path, timeline: dict) -> None:
    sidecar = audio_path.with_suffix(".timeline.json")
    temp = sidecar.with_name(f".{sidecar.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(timeline), encoding="utf-8")
        os.replace(temp, sidecar)
    finally:
        temp.unlink(missing_ok=True)


def _install_result_upload(audio_path: Path, audio: UploadFile | None, timeline_file: UploadFile | None) -> dict:
    """Install one patch's uploaded result WAV and/or timeline sidecar.

    A timeline is kept only when it describes the WAV it lands on - the same rule
    load_timeline applies to a sidecar read from disk - so one dropped against the
    wrong patch is reported back rather than installed. Raises ValueError with a
    user-facing reason when nothing can be installed at all."""
    timeline = None
    if timeline_file is not None:
        try:
            timeline = json.loads(timeline_file.file.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"timeline không đọc được: {exc}") from exc

    if audio is None:
        # A timeline on its own backfills the sidecar of audio already in place.
        if not audio_path.is_file():
            raise ValueError("chưa có audio để gắn timeline")
        info = sf.info(str(audio_path))
        checked = validate_timeline(timeline, info.samplerate, info.frames)
        if checked is not None:
            _write_timeline_sidecar(audio_path, checked)
        return {"audio": False, "timeline": "installed" if checked else "rejected"}

    staged = audio_path.with_name(f".{audio_path.name}.{uuid.uuid4().hex}.upload")
    try:
        with open(staged, "wb") as dest:
            shutil.copyfileobj(audio.file, dest)
        try:
            info = sf.info(str(staged))
        except sf.SoundFileError as exc:
            raise ValueError(f"WAV không hợp lệ: {exc}") from exc
        checked = validate_timeline(timeline, info.samplerate, info.frames) if timeline is not None else None
        # checked=None also clears a stale sidecar left by an earlier upload.
        _install_imported_wav(staged, audio_path, checked)
    finally:
        staged.unlink(missing_ok=True)
    return {
        "audio": True,
        "timeline": "none" if timeline is None else ("installed" if checked else "rejected"),
    }


def _result_patch_publish_state(conn, patch_id: int) -> str | None:
    pipeline = conn.execute(
        """SELECT pp.stage, pp.youtube_upload_id, yu.status AS youtube_status,
                  yu.youtube_video_id
             FROM patch_pipeline pp
             LEFT JOIN youtube_uploads yu ON yu.id=pp.youtube_upload_id
            WHERE pp.patch_id=?""", (patch_id,),
    ).fetchone()
    if pipeline and pipeline["youtube_video_id"]:
        return "published"
    live_job = conn.execute(
        """SELECT 1 FROM job
            WHERE (patch_id=? OR (job_type='patch_video' AND json_extract(payload_json, '$.patch_id')=?))
              AND job_type IN ('patch_video', 'youtube_upload')
              AND status IN ('pending', 'running') LIMIT 1""", (patch_id, patch_id),
    ).fetchone()
    if live_job or (pipeline and pipeline["youtube_status"] in {"pending", "uploading"}):
        return "active"
    return None


def _result_inbox(book_id: int) -> Path:
    # Mỗi ebook dùng chính data/books/<book_id> làm nơi nhận file lớn để toàn bộ
    # EPUB, WAV, timeline và artefact liên quan nằm chung một hồ sơ dễ quản lý.
    folder = Path(settings.data_root) / "books" / str(book_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _archive_move(root: Path, source: Path, kind: str) -> Path:
    """Move a processed result file into <root>/processed or <root>/rejected,
    collision-safe (suffix .1, .2, ... — files are never overwritten)."""
    dest_dir = root / kind
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / source.name
    n = 1
    while dest.exists():
        base = source.name[:-(len(source.suffix) or 0)]
        dest = dest_dir / f"{base}.{n}{source.suffix}"
        n += 1
    source.replace(dest)
    return dest


def _row_file_outcomes(row: dict, group: dict, outcome: dict | None) -> list[dict]:
    """Per-file outcome for a result row: which of the dropped files went where."""
    entries = []
    audio_file = group.get("audio")
    if audio_file is not None:
        ok = bool(outcome and outcome.get("audio"))
        entries.append({
            "filename": audio_file.filename,
            "outcome": "processed" if ok else "rejected",
            "reason": None if ok else row.get("detail"),
        })
    timeline_file = group.get("timeline")
    if timeline_file is not None:
        timeline_outcome = (outcome or {}).get("timeline")
        entries.append({
            "filename": timeline_file.filename,
            "outcome": "rejected" if timeline_outcome == "rejected" else "processed",
            "reason": row.get("detail") if timeline_outcome == "rejected" else None,
        })
    if not entries and row.get("filename"):
        entries.append({
            "filename": row["filename"], "outcome": "rejected",
            "reason": row.get("detail"),
        })
    return entries


@router.get("/books/{book_id}/patches/result-inbox")
def result_inbox_status(request: Request, book_id: int):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail="book not found")
        patches = repository.list_patches(conn, book_id)
    folder = _result_inbox(book_id)
    files = sorted(
        path.name for path in folder.iterdir()
        if path.is_file() and (path.name.lower().endswith(".wav") or path.name.lower().endswith(".timeline.json"))
    )
    processed_dir = folder / "processed"
    rejected_dir = folder / "rejected"
    processed = sorted(p.name for p in processed_dir.iterdir() if p.is_file()) if processed_dir.is_dir() else []
    rejected = sorted(p.name for p in rejected_dir.iterdir() if p.is_file()) if rejected_dir.is_dir() else []
    states = {}
    for patch in patches:
        if patch.status != "done" or not patch.audio_path:
            continue
        try:
            with locked_conn(request) as inner:
                check = evaluate_patch_preflight(inner, patch.id)
        except Exception:
            continue
        states[patch.id] = {"state": check["state"], "code": check["code"],
                            "error": check["error"]}
    return {"path": str(folder.resolve()), "files": files, "count": len(files),
            "processed": processed, "rejected": rejected, "patches": states}


@router.post("/books/{book_id}/patches/result-inbox/open")
def open_result_inbox(request: Request, book_id: int):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail="book not found")
    folder = _result_inbox(book_id).resolve()
    try:
        if sys.platform == "win32":
            os.startfile(str(folder))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(folder)])
        else:
            subprocess.Popen(["xdg-open", str(folder)])
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Không mở được folder: {exc}") from exc
    return {"ok": True, "path": str(folder)}


@router.post("/books/{book_id}/patches/result-inbox/process")
async def process_result_inbox(request: Request, book_id: int):
    """Process local large files without sending them through HTTP multipart.

    Legacy ``NNN - name`` inputs are renamed to ``<book_id>_<episode:03d>`` before
    being installed, while already-canonical files are accepted unchanged.
    """
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail="book not found")
        patches = {patch.patch_index: patch for patch in repository.list_patches(conn, book_id)}
    folder = _result_inbox(book_id)
    candidates = sorted(
        path for path in folder.iterdir()
        if path.is_file() and (path.name.lower().endswith(".wav") or path.name.lower().endswith(".timeline.json"))
    )
    renamed: list[dict] = []
    ready: list[Path] = []
    occupied: set[Path] = set()
    for source in candidates:
        index = _result_patch_index(source.name, book_id)
        if index is None or index not in patches:
            ready.append(source)
            continue
        suffix = ".timeline.json" if source.name.lower().endswith(".timeline.json") else ".wav"
        target = folder / f"{book_id}_{index + 1:03d}{suffix}"
        if source != target:
            if target.exists() or target in occupied:
                ready.append(source)
                continue
            source.rename(target)
            renamed.append({"from": source.name, "to": target.name})
        occupied.add(target)
        ready.append(target)
    if not ready:
        return {"ok": True, "installed": 0, "renamed": [], "results": [], "path": str(folder.resolve())}
    handles = []
    uploads = []
    try:
        for path in ready:
            handle = open(path, "rb")
            handles.append(handle)
            uploads.append(UploadFile(filename=path.name, file=handle))
        result = await upload_batch_results(request, book_id, uploads)
    finally:
        for handle in handles:
            handle.close()
    result["renamed"] = renamed
    result["path"] = str(folder.resolve())

    # Lưu trữ file đã xử lý: file OK vào <book>/processed, file lỗi vào <book>/rejected
    # kèm file .reason.txt — giữ vĩnh viễn để đối soát, không bao giờ ghi đè.
    outcome_by_file: dict[str, tuple[str, str | None]] = {}
    for row in result.get("results", []):
        for entry in row.get("file_outcomes") or []:
            outcome_by_file[entry["filename"]] = (entry["outcome"], entry.get("reason"))
    archived = {"processed": [], "rejected": []}
    for target in ready:
        outcome, reason = outcome_by_file.get(target.name, (None, None))
        if outcome is None or outcome == "kept":
            continue
        dest = _archive_move(folder, target, outcome)
        archived[outcome].append({"from": target.name, "to": dest.name})
        if outcome == "rejected" and reason:
            try:
                dest.with_name(dest.name + ".reason.txt").write_text(str(reason), encoding="utf-8")
            except OSError:
                logger.warning("cannot write reason file for rejected inbox file %s", dest, exc_info=True)
    result["archived"] = archived
    return result


@router.post("/books/{book_id}/patches/upload-results")
async def upload_batch_results(
    request: Request,
    book_id: int,
    files: list[UploadFile] = File(...),
):
    """Install a batch's whole result/ folder in one drop.

    Every "NNN - name.wav" is matched to the patch with index NNN and installed as
    that patch's audio; a "NNN - name.timeline.json" beside it becomes the chapter
    sidecar. Timelines may also arrive on their own to backfill patches whose audio
    is already installed. Each file is reported back individually - one bad file
    never blocks the rest of the drop."""
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail="book not found")
        book = repository.get_book(conn, book_id)
        patches = {p.patch_index: p for p in repository.list_patches(conn, book_id)}
        policy = resolve_automation_policy(book)
        publish_states = {p.id: _result_patch_publish_state(conn, p.id) for p in patches.values()}

    groups: dict[int, dict[str, UploadFile]] = {}
    skipped: list[dict] = []
    for upload in files:
        name = Path(upload.filename or "").name
        lower = name.lower()
        kind = "timeline" if lower.endswith(".timeline.json") else "audio" if lower.endswith(".wav") else None
        index = _result_patch_index(name, book_id)
        if kind is None:
            reason = "chỉ nhận .wav và .timeline.json"
        elif index is None or index not in patches:
            reason = "không khớp patch nào của sách này"
        elif kind in groups.get(index, {}):
            reason = f"trùng file {kind} cho patch {index:03d}"
        else:
            groups.setdefault(index, {})[kind] = upload
            continue
        skipped.append({"filename": name, "status": "skipped", "detail": reason,
                        "file_outcomes": [{"filename": name, "outcome": "rejected", "reason": reason}]})

    audio_dir = Path(settings.data_root) / "books" / str(book_id) / "patches"
    audio_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    installed_audio: list[tuple[int, Path]] = []
    installed_timelines: list[int] = []
    for index in sorted(groups):
        patch, group = patches[index], groups[index]
        audio_path = audio_dir / f"{patch.id}.wav"
        row = {
            "patch_id": patch.id,
            "patch_index": index,
            "patch_name": patch.name or str(index),
            "filename": (group.get("audio") or group["timeline"]).filename,
        }
        if patch.status == "processing":
            # Same refusal as the single-patch import: the worker owns this patch's
            # audio right now, so installing over it would race the merge.
            row["file_outcomes"] = _row_file_outcomes(row, group, None)
            results.append({**row, "status": "error", "detail": "patch đang xử lý"})
            continue
        if publish_states[patch.id] == "active":
            row["file_outcomes"] = _row_file_outcomes(row, group, None)
            results.append({**row, "status": "error", "detail": "patch đang tạo hoặc upload video", "publish_status": "blocked_active_pipeline"})
            continue
        try:
            outcome = await asyncio.to_thread(
                _install_result_upload, audio_path, group.get("audio"), group.get("timeline")
            )
        except (ValueError, OSError) as exc:
            logger.warning("result upload failed for patch %s", patch.id, exc_info=True)
            row["detail"] = str(exc)
            row["file_outcomes"] = _row_file_outcomes(row, group, None)
            results.append({**row, "status": "error", "detail": row["detail"]})
            continue
        if outcome["audio"]:
            installed_audio.append((patch.id, audio_path))
        if outcome.get("timeline") == "installed":
            installed_timelines.append(patch.id)
        row["file_outcomes"] = _row_file_outcomes(row, group, outcome)
        results.append({**row, "status": "ok", **outcome,
                        "publish_status": "pending_automation" if outcome["audio"] else "skipped_no_new_audio"})

    for patch_id, _ in installed_audio:
        await asyncio.to_thread(_warm_thumbnail, request, patch_id)

    with locked_conn(request) as conn:
        for patch_id, audio_path in installed_audio:
            repository.mark_patch_done(conn, patch_id, str(audio_path))

    # Cổng tự động hoá: với mỗi patch vừa có audio, enqueue_patch_video chạy preflight
    # (waiting_config / waiting_timeline / awaiting_republish_confirmation) và chỉ xếp
    # job patch_video khi sẵn sàng. Audio không đổi sau khi publish -> awaiting_confirmation
    # và KHÔNG reset pipeline (lịch sử upload giữ nguyên).
    with locked_conn(request) as conn:
        for patch_id, _ in installed_audio:
            result = next(row for row in results if row.get("patch_id") == patch_id)
            try:
                automation = enqueue_patch_video(conn, patch_id, force_new=True)
            except Exception as exc:  # audio remains installed and can be retried manually
                logger.warning("result publish enqueue failed for patch %s", patch_id, exc_info=True)
                result["publish_status"] = "enqueue_failed"
                result["publish_error"] = str(exc)
                continue
            state = automation["state"]
            if state == "queued":
                result["publish_status"] = "queued"
                result["job_id"] = automation["job_id"]
            elif state == "no_automation":
                result["publish_status"] = "skipped_auto_upload_disabled"
            elif state == "waiting_timeline":
                result["publish_status"] = "waiting_timeline"
                result["publish_error"] = automation.get("error")
            elif state == "waiting_config":
                result["publish_status"] = "waiting_config"
                result["publish_error"] = automation.get("error")
            elif state == "awaiting_republish_confirmation":
                result["publish_status"] = "awaiting_republish_confirmation"
                result["publish_error"] = automation.get("error")
            else:
                result["publish_status"] = "enqueue_failed"
                result["publish_error"] = automation.get("error")

    if installed_timelines and not installed_audio:
        # Timeline-only backfill (bổ sung sidecar cho audio đã có): patches đang
        # waiting_timeline có thể chạy tiếp mà không cần treo lại audio nữa.
        def _reconcile():
            from app.patch_publishing import reconcile_patch_automation
            with app_db.connect(settings.db_path) as conn:
                return reconcile_patch_automation(conn, book_id=book_id)
        try:
            await asyncio.to_thread(_reconcile)
        except Exception:
            logger.exception("automation reconcile failed after timeline backfill for book %s", book_id)

    statuses = [r.get("publish_status") for r in results if r.get("status") == "ok"]
    return {
        "ok": True,
        "installed": len(installed_audio),
        "auto_upload": policy["auto_upload_youtube"],
        "auto_create_video": policy["auto_create_video"],
        "publish_ready": any(s == "queued" for s in statuses),
        "publish_warning": next(
            (r["publish_error"] for r in results if r.get("publish_status") == "waiting_config"
             and r.get("publish_error")), None),
        "results": results + skipped,
    }


@router.post("/books/{book_id}/patches/{patch_id}/publish/confirm")
def confirm_patch_publish(request: Request, book_id: int, patch_id: int):
    """Xác nhận publish lại sau khi audio thay đổi: đóng dấu audio hiện tại và ngay
    lập tức preflight + snapshot + enqueue video mới (upload intent theo policy).

    Lịch sử upload cũ (youtube_uploads) được giữ nguyên; pipeline trỏ sang render mới."""
    from fastapi import HTTPException

    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        try:
            outcome = confirm_patch_republish(conn, patch_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    pipeline = outcome.get("pipeline") or {}
    return {
        "ok": True,
        "patch_id": patch_id,
        "state": outcome["state"],
        "job_id": outcome.get("job_id"),
        "deduplicated": outcome.get("deduplicated"),
        "code": outcome.get("code"),
        "error": outcome.get("error"),
        "stage": pipeline.get("stage"),
        "republish_confirmed_for": pipeline.get("republish_confirmed_for"),
        "note": "video mới đã được xếp hàng; upload sẽ chạy khi render xong",
    }
