"""Standalone Video Creator page: upload audio + image + ffmpeg settings -> mp4."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app import image_overlay, media_library, repository, video_gen
from app.config import settings
from app.deps import locked_conn

router = APIRouter()
logger = logging.getLogger(__name__)

# In-memory step progress for debug UI, keyed by "{batch_id}:{idx}" or job_id.
# Best-effort: lost on restart, purged after 1h alongside tmp files.
_progress_store: dict[str, dict] = {}


def _record_step(job_key: str, event: str, fields: dict) -> None:
    entry = _progress_store.setdefault(
        job_key, {"status": "running", "steps": [], "updated_at": 0.0}
    )
    detail = " ".join(f"{k}={v}" for k, v in fields.items())
    entry["steps"].append({
        "t": datetime.now().strftime("%H:%M:%S"),
        "event": event,
        "detail": detail,
    })
    if event.endswith(".failed"):
        entry["status"] = "error"
    elif event == "job.done":
        entry["status"] = "done"
    entry["updated_at"] = time.time()


def _cleanup_progress_store(max_age_seconds: int = 3600) -> None:
    now = time.time()
    stale = [k for k, v in _progress_store.items()
             if (now - v.get("updated_at", 0)) > max_age_seconds]
    for k in stale:
        _progress_store.pop(k, None)


def _make_progress_logger(
    prefix: str, job_key: str | None = None, **base_fields
) -> video_gen.ProgressCallback:
    def _on_progress(event: str, fields: dict) -> None:
        parts = [f"event={prefix}.{event}"]
        merged = {**base_fields, **fields}
        for k, v in merged.items():
            parts.append(f"{k}={v}")
        logger.info(" ".join(parts))
        if job_key is not None:
            _record_step(job_key, event, fields)
    return _on_progress

_TMP_DIR = Path(settings.data_root) / "tmp" / "video_creator"
_VIDEOS_DIR = Path(settings.data_root) / "videos"
_BACKGROUNDS_DIR = Path(settings.data_root) / "backgrounds"

ALLOWED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg"}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
# Backgrounds may be a still image or a looping video clip.
ALLOWED_BACKGROUND_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | video_gen.VIDEO_BACKGROUND_EXTENSIONS
_BACKGROUND_MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm",
    ".mov": "video/quicktime",
}

VALID_RESOLUTIONS = {"1920x1080", "1280x720", "854x480"}
VALID_FPS = {24, 30, 60}
VALID_CODECS = {"libx264", "h264_nvenc"}
VALID_AUDIO_BITRATES = {"128k", "192k", "256k", "320k"}
VALID_IMAGE_TYPES = {"none", "zoom-in", "zoom-out", "pan-left", "pan-right"}


def _cleanup_old_tmp_files(max_age_seconds: int = 3600) -> None:
    """Delete tmp files older than max_age_seconds. Best-effort."""
    _cleanup_progress_store(max_age_seconds)
    if not _TMP_DIR.exists():
        return
    now = time.time()
    for p in _TMP_DIR.iterdir():
        try:
            if p.is_dir():
                if (now - p.stat().st_mtime) > max_age_seconds:
                    shutil.rmtree(p, ignore_errors=True)
            elif p.is_file() and (now - p.stat().st_mtime) > max_age_seconds:
                p.unlink()
        except OSError:
            pass


def _validate_config(
    resolution: str, fps: int, codec: str,
    audio_bitrate: str, image_type: str, crf: int,
) -> dict:
    return {
        "resolution": resolution if resolution in VALID_RESOLUTIONS else "1920x1080",
        "fps": fps if fps in VALID_FPS else 30,
        "codec": codec if codec in VALID_CODECS else "libx264",
        "audio_bitrate": audio_bitrate if audio_bitrate in VALID_AUDIO_BITRATES else "192k",
        "image_type": image_type if image_type in VALID_IMAGE_TYPES else "none",
        "crf": crf if 18 <= crf <= 28 else 23,
    }


def _resolve_background_image(bg_path: str | None) -> Path | None:
    """Resolve background image from path, falling back to default."""
    if bg_path:
        p = Path(bg_path)
        if p.exists():
            return p
    default = settings.default_background_image
    if Path(default).exists():
        return Path(default)
    return None


def _overlay_values_with_defaults(values) -> dict:
    """Video Creator's overlay UI only exposes text/position/size/color/drag
    offset - no shadow/box/marquee toggles. Force shadow on (matching the
    always-on shadow this page has always rendered) so reusing the shared
    config builder doesn't silently drop it for missing fields."""
    merged = dict(values)
    merged.setdefault("shadow_enabled", "on")
    return merged


def _convert_overlay_config_to_flat(cfg: dict) -> dict:
    """Convert frontend nested overlay config to backend flat format."""
    flat = {
        "position": cfg.get("position", "top"),
        "alignment": cfg.get("alignment", "center"),
        "font_size": cfg.get("font_size", 52),
        "text_color": cfg.get("text_color", "#FFFFFF"),
        "margin": cfg.get("margin", 20),
        "offset_x": cfg.get("offset_x", 0),
        "offset_y": cfg.get("offset_y", 0),
    }
    
    # Shadow
    shadow = cfg.get("shadow", {})
    flat["shadow_enabled"] = "on" if shadow.get("enabled", False) else "off"
    flat["shadow_color"] = shadow.get("color", "#000000")
    flat["shadow_offset"] = shadow.get("offset", 3)
    
    # Box
    box = cfg.get("box", {})
    flat["box_enabled"] = "on" if box.get("enabled", False) else "off"
    flat["box_color"] = box.get("color", "#000000")
    flat["box_opacity"] = box.get("opacity", 60)
    flat["box_padding_x"] = box.get("padding_x", 16)
    flat["box_padding_y"] = box.get("padding_y", 8)
    flat["box_radius"] = box.get("radius", 8)
    
    # Marquee
    marquee = cfg.get("marquee", {})
    flat["marquee_enabled"] = "on" if marquee.get("enabled", False) else "off"
    flat["marquee_height"] = marquee.get("height", 60)
    flat["marquee_font_size"] = marquee.get("font_size", 36)
    flat["marquee_text_color"] = marquee.get("text_color", "#FFFFFF")
    flat["marquee_bg_color"] = marquee.get("bg_color", "#000000")
    flat["marquee_bg_opacity"] = marquee.get("bg_opacity", 80)
    flat["marquee_speed"] = marquee.get("speed_px_per_sec", 50)
    
    return flat


def _render_overlay_for_batch(
    bg_path: Path, text: str, overlay_opts: dict, out_path: Path,
) -> Path | None:
    """Render user text onto a copy of the background. None on failure (caller
    falls back to the plain background rather than failing the video)."""
    try:
        from PIL import Image
        # Convert nested config to flat format
        flat_opts = _convert_overlay_config_to_flat(overlay_opts)
        logger.info("video_creator: overlay_opts=%r", overlay_opts)
        logger.info("video_creator: flat_opts=%r", flat_opts)
        cfg = image_overlay.overlay_cfg_from_values(_overlay_values_with_defaults(flat_opts))
        logger.info("video_creator: cfg=%r", cfg)
        img = Image.open(str(bg_path)).convert("RGB")
        lines = image_overlay.build_overlay_lines(img, text, cfg)
        logger.info("video_creator: text=%r lines=%r", text, lines)
        image_overlay.render_overlay(img, lines, cfg)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path), "PNG")
        return out_path
    except Exception as exc:
        logger.error("video_creator: overlay render failed: %s", exc)
        return None


def _sanitize_basename(name: str) -> str:
    """Sanitize an audio filename into a safe video basename (no extension).

    Strips path components, replaces filesystem-unsafe characters with
    underscores, and falls back to 'video' if the result is empty.
    """
    base = Path(name or "").name
    base = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', base)
    base = base.strip().strip('.')
    return base or "video"


def _unique_video_path(stem: str) -> Path:
    """Return a non-colliding path in _VIDEOS_DIR for the given stem."""
    candidate = _VIDEOS_DIR / f"{stem}.mp4"
    if not candidate.exists():
        return candidate
    i = 1
    while True:
        candidate = _VIDEOS_DIR / f"{stem}_{i}.mp4"
        if not candidate.exists():
            return candidate
        i += 1


def _get_recent_videos(limit: int = 20) -> list[dict]:
    videos = []
    if _VIDEOS_DIR.exists():
        for f in sorted(_VIDEOS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.suffix == ".mp4":
                videos.append({
                    "name": f.name,
                    "url": f"/video/videos/{f.name}",
                    "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                })
            if len(videos) >= limit:
                break
    return videos


# ---------------------------------------------------------------------------
# Original single-file endpoints (kept for backward compatibility)
# ---------------------------------------------------------------------------



@router.post("/video/generate")
async def generate_video(
    request: Request,
    audio: UploadFile = File(...),
    image: UploadFile | None = File(default=None),
    resolution: str = Form(default="1920x1080"),
    fps: int = Form(default=30),
    codec: str = Form(default="libx264"),
    audio_bitrate: str = Form(default="192k"),
    image_type: str = Form(default="none"),
    crf: int = Form(default=23),
):
    audio_ext = Path(audio.filename or "").suffix.lower()
    if audio_ext not in ALLOWED_AUDIO_EXTENSIONS:
        raise HTTPException(400, f"Unsupported audio format: {audio_ext}")

    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]

    audio_path = _TMP_DIR / f"{job_id}_audio{audio_ext}"
    with open(audio_path, "wb") as f:
        shutil.copyfileobj(audio.file, f)

    image_path = None
    if image is not None and image.filename:
        img_ext = Path(image.filename).suffix.lower()
        if img_ext not in ALLOWED_BACKGROUND_EXTENSIONS:
            audio_path.unlink(missing_ok=True)
            raise HTTPException(400, f"Unsupported background format: {img_ext}")
        image_path = _TMP_DIR / f"{job_id}_image{img_ext}"
        with open(image_path, "wb") as f:
            shutil.copyfileobj(image.file, f)
    else:
        default = settings.default_background_image
        if Path(default).exists():
            image_path = Path(default)
        else:
            audio_path.unlink(missing_ok=True)
            raise HTTPException(400, "Please upload an image or configure a default background image")

    cfg = _validate_config(resolution, fps, codec, audio_bitrate, image_type, crf)
    tmp_out = _TMP_DIR / f"{job_id}.mp4"

    progress_cb = _make_progress_logger(
        "video_creator.single", job_key=f"single:{job_id}",
        job_id=job_id, mode="single",
    )
    try:
        await asyncio.to_thread(
            video_gen.generate_standalone_video,
            str(audio_path), str(image_path), str(tmp_out),
            on_progress=progress_cb,
            **cfg,
        )
    except Exception as exc:
        tmp_out.unlink(missing_ok=True)
        raise HTTPException(500, f"Video generation failed: {exc}")

    _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    final_name = f"{job_id}.mp4"
    final_path = _VIDEOS_DIR / final_name
    shutil.move(str(tmp_out), str(final_path))

    return {"video_url": f"/video/videos/{final_name}", "filename": final_name}


# ---------------------------------------------------------------------------
# Batch upload endpoints
# ---------------------------------------------------------------------------

@router.post("/video/upload-batch")
async def upload_batch(
    files: list[UploadFile] = File(...),
):
    """Accept multiple audio files, save to a batch dir, return file metadata."""
    _cleanup_old_tmp_files()
    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    batch_id = uuid.uuid4().hex[:12]
    batch_dir = _TMP_DIR / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    saved: list[dict] = []
    errors: list[str] = []

    for idx, f in enumerate(files):
        ext = Path(f.filename or "").suffix.lower()
        if ext not in ALLOWED_AUDIO_EXTENSIONS:
            errors.append(f"{f.filename}: unsupported format ({ext})")
            continue
        safe_name = f"audio_{idx}{ext}"
        dest = batch_dir / safe_name
        with open(dest, "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append({
            "index": idx,
            "original_name": f.filename or safe_name,
            "saved_name": safe_name,
            "size_bytes": dest.stat().st_size,
            "path": str(dest),
        })

    meta = {"batch_id": batch_id, "files": saved, "created_at": time.time()}
    with open(batch_dir / "meta.json", "w") as mf:
        json.dump(meta, mf)

    return JSONResponse({
        "batch_id": batch_id,
        "status": "audio_ready",
        "message": "Audio loaded. Generate videos to add MP4 files to Video Library.",
        "files": [
            {"index": s["index"], "name": s["original_name"], "size_mb": round(s["size_bytes"] / (1024 * 1024), 2)}
            for s in saved
        ],
        "errors": errors,
    })


@router.post("/video/batch/{batch_id}/greetings")
async def upload_batch_greetings(
    batch_id: str,
    intro_audio: UploadFile | None = File(default=None),
    outro_audio: UploadFile | None = File(default=None),
):
    batch_dir = _TMP_DIR / batch_id
    meta_path = batch_dir / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Batch not found or expired")
    with open(meta_path) as mf:
        meta = json.load(mf)
    for key, upload in (("intro_audio", intro_audio), ("outro_audio", outro_audio)):
        if upload is None:
            continue
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in ALLOWED_AUDIO_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"Unsupported audio format: {ext}")
        path = batch_dir / f"{key}{ext}"
        with open(path, "wb") as out:
            shutil.copyfileobj(upload.file, out)
        meta[key] = str(path)
    with open(meta_path, "w") as mf:
        json.dump(meta, mf)
    return JSONResponse({"status": "ready"})


@router.post("/video/generate-batch")
async def generate_batch(request: Request, background_tasks: BackgroundTasks):
    """Generate videos for selected items in a batch.

    JSON body:
    {
        "batch_id": "...",
        "selected": [0, 1, 2],          // indices to process
        "backgrounds": {                 // per-row background overrides
            "0": "/path/to/image.jpg",
            "1": null                     // use default
        },
        "config": {
            "resolution": "1920x1080",
            "fps": 30,
            "codec": "libx264",
            "audio_bitrate": "192k",
            "image_type": "none",
            "crf": 23
        }
    }

    Returns immediately with job IDs; client should poll /video/progress/{batch_id}
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    batch_id = body.get("batch_id", "")
    selected = body.get("selected", [])
    backgrounds: dict = body.get("backgrounds", {})
    raw_cfg = body.get("config", {})
    max_concurrent = max(1, min(8, int(raw_cfg.get("max_concurrent", 3))))

    # Get database connection and lock from app state
    conn = request.app.state.conn
    db_lock = request.app.state.db_lock

    batch_dir = _TMP_DIR / batch_id
    meta_path = batch_dir / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Batch not found or expired")

    with open(meta_path) as mf:
        meta = json.load(mf)

    cfg = _validate_config(
        raw_cfg.get("resolution", "1920x1080"),
        raw_cfg.get("fps", 30),
        raw_cfg.get("codec", "libx264"),
        raw_cfg.get("audio_bitrate", "192k"),
        raw_cfg.get("image_type", "none"),
        raw_cfg.get("crf", 23),
    )

    # Music: resolve library id -> file path (batch-wide, optional)
    music_path: str | None = None
    music_attribution = ""
    music_volume = max(0, min(100, int(raw_cfg.get("music_volume", 15)))) / 100.0
    raw_music_id = raw_cfg.get("music_id")
    if raw_music_id is not None and str(raw_music_id).strip().isdigit():
        with locked_conn(request) as conn:
            music = repository.get_music(conn, int(raw_music_id))
        if music and Path(music.file_path).exists():
            music_path = music.file_path
            music_attribution = "\n".join(filter(None, [music.description, f"Music license: {music.license}" if music.license else ""]))
        else:
            logger.warning("video_creator: music id %s not found/missing on disk", raw_music_id)

    intro_audio = meta.get("intro_audio")
    outro_audio = meta.get("outro_audio")

    files_map = {f["index"]: f for f in meta["files"]}
    _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    # Build task list
    tasks = []
    for idx in selected:
        idx = int(idx)
        finfo = files_map.get(idx)
        if not finfo:
            continue

        audio_path = Path(finfo["path"])
        if not audio_path.exists():
            continue

        bg_path = backgrounds.get(str(idx))
        image_path = Path(bg_path) if bg_path and Path(bg_path).exists() else _resolve_background_image(bg_path)
        if image_path is None:
            continue

        job_key = f"{batch_id}:{idx}"
        _progress_store.pop(job_key, None)
        _record_step(job_key, "job.start", {"file": finfo["original_name"]})
        if music_path:
            _record_step(job_key, "music.resolved", {"path": Path(music_path).name,
                                                     "volume": music_volume})

        tasks.append((idx, finfo, audio_path, image_path, job_key))

    if not tasks:
        return JSONResponse({"batch_id": batch_id, "scheduled": 0})

    # Record batch-level info
    batch_key = f"{batch_id}:_batch"
    _record_step(batch_key, "batch.start", {
        "total": len(tasks), "max_concurrent": max_concurrent,
    })

    async def run_with_semaphore(sem, idx, finfo, audio_path, image_path, job_key):
        async with sem:
            return await _run_single_video(
                idx, finfo, audio_path, image_path, job_key,
                intro_audio, outro_audio,
                music_path, music_volume, music_attribution, cfg, batch_id,
                conn, db_lock,
            )

    async def run_batch():
        sem = asyncio.Semaphore(max_concurrent)
        coros = [
            run_with_semaphore(sem, *task)
            for task in tasks
        ]
        results = await asyncio.gather(*coros, return_exceptions=True)
        done_count = sum(1 for r in results if r is True)
        _record_step(batch_key, "batch.done", {
            "total": len(tasks), "succeeded": done_count,
            "failed": len(tasks) - done_count,
        })
        _progress_store[batch_key]["status"] = "done"

    background_tasks.add_task(run_batch)

    return JSONResponse({"batch_id": batch_id, "scheduled": len(tasks),
                         "max_concurrent": max_concurrent})


async def _run_single_video(
    idx, finfo, audio_path, image_path, job_key,
    intro_audio, outro_audio,
    music_path, music_volume, music_attribution, cfg, batch_id,
    conn, db_lock,
):
    stem = _sanitize_basename(finfo["original_name"])
    final_path = _unique_video_path(stem)
    tmp_out = _TMP_DIR / f"{batch_id}_{idx}_{uuid.uuid4().hex[:6]}.mp4"
    progress_cb = _make_progress_logger(
        "video_creator.batch", job_key=job_key,
        batch_id=batch_id, index=idx, mode="batch",
    )
    started = time.time()
    try:
        await asyncio.to_thread(
            video_gen.generate_standalone_video,
            str(audio_path), str(image_path), str(tmp_out),
            on_progress=progress_cb,
            music_path=music_path,
            music_volume=music_volume,
            intro_audio=intro_audio,
            outro_audio=outro_audio,
            **cfg,
        )
        from app.video_integrity import validate_video
        from app.video_publish import VideoValidationError
        validation = await asyncio.to_thread(validate_video, tmp_out)
        if not validation.valid:
            raise VideoValidationError(validation)
        tmp_out.replace(final_path)
        source_dir = Path(settings.data_root) / "video_sources" / f"{batch_id}_{idx}"
        source_dir.mkdir(parents=True, exist_ok=True)
        durable_audio = source_dir / Path(audio_path).name
        durable_background = source_dir / Path(image_path).name
        shutil.copy2(audio_path, durable_audio)
        shutil.copy2(image_path, durable_background)
        # Register video in database
        from app.video_repository import insert_video
        try:
            with db_lock:
                video_record = insert_video(
                    conn,
                    filename=final_path.name,
                    original_name=finfo["original_name"],
                    file_path=str(final_path),
                    file_size_bytes=final_path.stat().st_size,
                    resolution=cfg.get("resolution", "1920x1080"),
                    batch_id=batch_id,
                    source_audio=str(durable_audio),
                    background_path=str(durable_background),
                    render_config={
                        **cfg,
                        "music_path": music_path,
                        "music_volume": music_volume,
                        "intro_audio": intro_audio,
                        "outro_audio": outro_audio,
                    },
                    title=finfo["original_name"], description=music_attribution,
                )
                video_db_id = video_record["id"]
        except Exception as e:
            logger.warning("Failed to register video in database: %s", e)
            video_db_id = 0
        _record_step(job_key, "job.done", {"video": final_path.name})
        completed_at = datetime.now()
        _progress_store[job_key]["result"] = {
            "index": idx,
            "status": "done",
            "name": final_path.name,
            "video_url": f"/video/videos/{final_path.name}",
            "size_mb": round(final_path.stat().st_size / (1024 * 1024), 1),
            "elapsed_seconds": round(time.time() - started, 1),
            "completed_at": completed_at.isoformat(timespec="seconds"),
        }
        # Auto-upload to YouTube if configured
        from app import youtube
        if settings.youtube_auto_upload and youtube.is_configured():
            try:
                from app.jobqueue import store
                upload_id = youtube.enqueue_upload(
                    conn, str(final_path), finfo["original_name"], music_attribution,
                    [t.strip() for t in settings.youtube_default_tags.split(",") if t.strip()],
                    settings.youtube_default_privacy, video_id=video_db_id or None,
                    render_source_type="standalone" if video_db_id else "external",
                    render_source_id=video_db_id or None,
                )
                store.enqueue(conn, "youtube_upload", payload={"upload_id": upload_id, "video_id": video_db_id or None},
                              dedupe_key=f"youtube_upload:upload={upload_id}")
            except Exception as e:
                logger.warning("Auto-upload enqueue failed: %s", e)
        return True
    except Exception as exc:
        tmp_out.unlink(missing_ok=True)
        _record_step(job_key, "job.failed", {"error": str(exc)[:300]})
        _progress_store[job_key]["result"] = {
            "index": idx,
            "status": "error",
            "message": str(exc),
            "elapsed_seconds": round(time.time() - started, 1),
        }
        return False
@router.get("/video/progress/{batch_id}")
def get_batch_progress(batch_id: str):
    """Per-file step progress for a running/finished batch (debug UI)."""
    prefix = f"{batch_id}:"
    jobs = {
        key[len(prefix):]: {"status": v["status"], "steps": v["steps"], "result": v.get("result")}
        for key, v in _progress_store.items()
        if key.startswith(prefix)
    }
    return JSONResponse({"jobs": jobs})


_AUDIO_MIME_MAP = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}


@router.get("/video/batch/{batch_id}/audio/{index}")
def serve_batch_audio(batch_id: str, index: int):
    """Stream one uploaded batch audio file, for the studio's mix-preview player."""
    meta_path = _TMP_DIR / batch_id / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404, detail="Batch not found or expired")
    with open(meta_path) as mf:
        meta = json.load(mf)
    finfo = next((f for f in meta["files"] if f["index"] == index), None)
    if finfo is None:
        raise HTTPException(status_code=404, detail="File not found in batch")
    p = Path(finfo["path"])
    if not p.exists():
        raise HTTPException(status_code=404, detail="Audio file missing")
    media = _AUDIO_MIME_MAP.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(str(p), media_type=media)


# ---------------------------------------------------------------------------
# Background image management
# ---------------------------------------------------------------------------

@router.get("/video/backgrounds")
def list_backgrounds(request: Request):
    """List available background images (user-uploaded + default)."""
    _BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    items: list[dict] = []

    default = settings.default_background_image
    safe_default = _safe_background_path(default)
    if safe_default:
        items.append({"name": "__default__", "path": default, "is_default": True,
                      "is_video": video_gen.is_video_background(safe_default)})

    for f in sorted(_BACKGROUNDS_DIR.iterdir()):
        safe_path = _safe_background_path(str(f))
        if safe_path and safe_path.suffix.lower() in ALLOWED_BACKGROUND_EXTENSIONS:
            items.append({"name": f.name, "path": str(safe_path), "is_default": False,
                          "is_video": video_gen.is_video_background(safe_path)})

    return JSONResponse({"backgrounds": items})


def _safe_background_path(path: str) -> Path | None:
    """Resolve a background image path, restricted to the backgrounds dir or
    the configured default. Returns None (never raises) for anything else, so
    callers can fall back to the default background instead of erroring."""
    if not path:
        return None
    p = Path(path)
    if not p.is_absolute():
        p = (Path(settings.data_root) / path).resolve()

    default = Path(settings.default_background_image).resolve()
    backgrounds = _BACKGROUNDS_DIR.resolve()
    try:
        p_resolved = p.resolve()
    except OSError:
        return None
    if p_resolved != default and backgrounds not in p_resolved.parents:
        return None
    if not p_resolved.exists() or not p_resolved.is_file():
        return None
    return p_resolved


@router.get("/video/backgrounds/preview")
def preview_background(path: str = ""):
    """Serve a background image file for the Configure Each File preview thumbnail.
    Only paths inside the backgrounds dir or matching the configured default are
    allowed; everything else returns 404 to avoid serving arbitrary disk files."""
    if not path:
        raise HTTPException(status_code=400, detail="path query parameter required")
    p_resolved = _safe_background_path(path)
    if p_resolved is None:
        raise HTTPException(status_code=404, detail="background not found")

    media = _BACKGROUND_MIME_MAP.get(p_resolved.suffix.lower(), "image/jpeg")
    return FileResponse(str(p_resolved), media_type=media)


@router.post("/video/upload-background")
async def upload_background(request: Request, file: UploadFile = File(...)):
    """Upload a new background (image or looping video) to the backgrounds directory."""
    ext = Path(file.filename or "").suffix.lower()
    if ext not in ALLOWED_BACKGROUND_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported background format: {ext}")

    _BACKGROUNDS_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename).name}"
    dest = _BACKGROUNDS_DIR / safe_name
    with open(dest, "wb") as out:
        shutil.copyfileobj(file.file, out)

    return JSONResponse({"name": safe_name, "path": str(dest)})


@router.post("/video/backgrounds/delete")
def delete_background(request: Request, path: str = Form(...)):
    """Delete a media file from the library by absolute path, the way the video
    config modal addresses backgrounds. Books and patches pointing at it are
    cleared first so nothing renders against a missing file."""
    resolved = _safe_background_path(path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy file media")
    if resolved == Path(settings.default_background_image).resolve():
        raise HTTPException(status_code=400, detail="Không thể xóa background mặc định")
    with locked_conn(request) as conn:
        media_library.delete_background(conn, resolved)
    return JSONResponse({"status": "deleted", "name": resolved.name})


# ---------------------------------------------------------------------------
# Video serving
# ---------------------------------------------------------------------------

@router.get("/video/videos/{filename}")
def serve_video(filename: str):
    safe = Path(filename).name
    file_path = _VIDEOS_DIR / safe
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(str(file_path), media_type="video/mp4")


@router.get("/video/output/{filename}")
def serve_video_output(filename: str):
    safe = Path(filename).name
    file_path = _TMP_DIR / safe
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Video not found")
    return FileResponse(str(file_path), media_type="video/mp4")
