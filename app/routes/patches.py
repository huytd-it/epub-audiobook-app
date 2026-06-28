from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from app import repository, video_gen
from app.config import settings
from app.deps import locked_conn

router = APIRouter()

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


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
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported image format: {ext}")

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
        with open(dest, "wb") as f:
            shutil.copyfileobj(image.file, f)

        repository.save_patch_image(conn, patch_id, str(dest))

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


@router.post("/books/{book_id}/patches/{patch_id}/generate-video")
def generate_patch_video(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        if patch.status != "done" or not patch.audio_path:
            raise HTTPException(status_code=400, detail="Patch audio not ready")
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")

    image = video_gen.resolve_patch_image(patch, book, settings.default_background_image)
    if not image:
        raise HTTPException(status_code=400, detail="No background image available")

    w, h = (book.video_resolution or "1920x1080").split("x")
    resolution = (int(w), int(h))
    fps = book.video_fps or 30
    image_type = patch.image_type if patch.image_type and patch.image_type != "static" else "none"

    out_dir = Path(settings.data_root) / "books" / str(book_id) / "patch_videos"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(out_dir / f"{patch_id}.mp4")

    try:
        video_gen.generate_segment(
            image, patch.audio_path, out_path,
            image_type=image_type,
            resolution=resolution,
            fps=fps,
            use_nvenc=settings.use_nvenc,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return RedirectResponse(url=f"/books/{book_id}/patches/build", status_code=303)


@router.get("/books/{book_id}/patches/{patch_id}/video")
def get_patch_video(request: Request, book_id: int, patch_id: int):
    video_path = Path(settings.data_root) / "books" / str(book_id) / "patch_videos" / f"{patch_id}.mp4"
    if not video_path.exists():
        raise HTTPException(status_code=404, detail="Video not generated yet")
    return FileResponse(str(video_path), media_type="video/mp4")
