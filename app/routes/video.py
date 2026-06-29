"""Standalone Video Creator page: upload audio + image + ffmpeg settings -> mp4."""
from __future__ import annotations

import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from app import video_gen
from app.config import settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

_TMP_DIR = Path(settings.data_root) / "tmp" / "video_creator"
_VIDEOS_DIR = Path(settings.data_root) / "videos"

ALLOWED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg"}
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _cleanup_old_tmp_files(max_age_seconds: int = 3600) -> None:
    """Delete tmp files older than max_age_seconds. Best-effort."""
    if not _TMP_DIR.exists():
        return
    now = time.time()
    for f in _TMP_DIR.iterdir():
        try:
            if f.is_file() and (now - f.stat().st_mtime) > max_age_seconds:
                f.unlink()
        except OSError:
            pass


@router.get("/video", response_class=HTMLResponse)
def video_creator_page(request: Request):
    _cleanup_old_tmp_files()
    recent_videos = []
    if _VIDEOS_DIR.exists():
        for f in sorted(_VIDEOS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if f.suffix == ".mp4":
                recent_videos.append({
                    "name": f.name,
                    "url": f"/video/videos/{f.name}",
                    "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
                })
    return templates.TemplateResponse(request, "video_creator.html", {
        "request": request,
        "video_url": None,
        "error": None,
        "recent_videos": recent_videos,
    })


@router.post("/video/generate", response_class=HTMLResponse)
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
        return templates.TemplateResponse(request, "video_creator.html", {
            "request": request,
            "video_url": None,
            "error": f"Unsupported audio format: {audio_ext}",
            "recent_videos": [],
        })

    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    job_id = uuid.uuid4().hex[:12]

    audio_path = _TMP_DIR / f"{job_id}_audio{audio_ext}"
    with open(audio_path, "wb") as f:
        shutil.copyfileobj(audio.file, f)

    image_path = None
    if image is not None and image.filename:
        img_ext = Path(image.filename).suffix.lower()
        if img_ext not in ALLOWED_IMAGE_EXTENSIONS:
            audio_path.unlink(missing_ok=True)
            return templates.TemplateResponse(request, "video_creator.html", {
                "request": request,
                "video_url": None,
                "error": f"Unsupported image format: {img_ext}",
                "recent_videos": [],
            })
        image_path = _TMP_DIR / f"{job_id}_image{img_ext}"
        with open(image_path, "wb") as f:
            shutil.copyfileobj(image.file, f)
    else:
        default = settings.default_background_image
        if Path(default).exists():
            image_path = Path(default)
        else:
            audio_path.unlink(missing_ok=True)
            return templates.TemplateResponse(request, "video_creator.html", {
                "request": request,
                "video_url": None,
                "error": "Please upload an image or configure a default background image",
                "recent_videos": [],
            })

    if resolution not in {"1920x1080", "1280x720", "854x480"}:
        resolution = "1920x1080"
    if fps not in {24, 30, 60}:
        fps = 30
    if codec not in {"libx264", "h264_nvenc"}:
        codec = "libx264"
    if audio_bitrate not in {"128k", "192k", "256k", "320k"}:
        audio_bitrate = "192k"
    if image_type not in {"none", "zoom-in", "zoom-out", "pan-left", "pan-right"}:
        image_type = "none"
    if not (18 <= crf <= 28):
        crf = 23

    tmp_out = _TMP_DIR / f"{job_id}.mp4"

    try:
        video_gen.generate_standalone_video(
            str(audio_path), str(image_path), str(tmp_out),
            resolution=resolution,
            fps=fps,
            codec=codec,
            audio_bitrate=audio_bitrate,
            image_type=image_type,
            crf=crf,
        )
    except Exception as exc:
        tmp_out.unlink(missing_ok=True)
        return templates.TemplateResponse(request, "video_creator.html", {
            "request": request,
            "video_url": None,
            "error": f"Video generation failed: {exc}",
            "recent_videos": [],
        })

    _VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    final_name = f"{job_id}.mp4"
    final_path = _VIDEOS_DIR / final_name
    shutil.move(str(tmp_out), str(final_path))

    recent_videos = []
    for f in sorted(_VIDEOS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if f.suffix == ".mp4":
            recent_videos.append({
                "name": f.name,
                "url": f"/video/videos/{f.name}",
                "size_mb": round(f.stat().st_size / (1024 * 1024), 1),
            })

    return templates.TemplateResponse(request, "video_creator.html", {
        "request": request,
        "video_url": f"/video/videos/{final_name}",
        "error": None,
        "recent_videos": recent_videos,
    })


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
