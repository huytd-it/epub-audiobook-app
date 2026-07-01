from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import audio_merge, drive_export, google_drive, repository, video_gen
from app.chunker import split_into_tts_chunks
from app.config import settings
from app.deps import locked_conn

logger = logging.getLogger(__name__)

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


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
async def generate_patch_video(request: Request, book_id: int, patch_id: int):
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
        await asyncio.to_thread(
            video_gen.generate_segment,
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


# ---------------------------------------------------------------------------
# Chunk manager: per-chunk view, max_chars override, resume-from-chunk,
# Google Drive export/import for Colab/Kaggle synthesis.
# ---------------------------------------------------------------------------


@router.get("/books/{book_id}/patches/{patch_id}/chunks", response_class=HTMLResponse)
def chunk_manager_page(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        book = repository.get_book(conn, book_id)
        worker = request.app.state.worker
        chunks = repository.get_patch_chunk_view(conn, patch, worker)
        exports = repository.list_patch_exports(conn, patch_id)
        drive_connected = google_drive.get_creds_from_db(conn) is not None
    return templates.TemplateResponse(request, "chunk_manager.html", {
        "request": request,
        "book": book,
        "patch": patch,
        "chunks": chunks,
        "exports": exports,
        "drive_connected": drive_connected,
        "drive_configured": google_drive.is_configured(),
    })


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


@router.post("/books/{book_id}/patches/{patch_id}/export")
def export_patch_to_drive(request: Request, book_id: int, patch_id: int):
    # Mirrors worker.py's convention of holding db_lock for the full duration of a Google
    # API call (see the youtube upload in _process_book_job) - simpler than fine-grained
    # locking, acceptable for a single-user local app.
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        if patch.status == "processing":
            raise HTTPException(status_code=400, detail="cannot export a patch that is currently processing")
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        if google_drive.get_creds_from_db(conn) is None:
            raise HTTPException(status_code=400, detail="Google Drive not connected. Connect it at /drive first.")

        package_dir = drive_export.build_export_package(conn, patch)
        try:
            service = google_drive.get_drive_service(conn)
            root_id = google_drive.get_or_create_root_folder(service)
            folder_name = drive_export.folder_name_for_patch(book.title, patch)
            folder = google_drive.create_folder(service, folder_name, parent_id=root_id)
            for f in sorted(package_dir.iterdir()):
                google_drive.upload_file(service, folder["id"], str(f))
            chunk_count = sum(1 for f in package_dir.iterdir() if f.name.startswith("chunk_") and f.suffix == ".txt")
            repository.create_patch_export(conn, patch_id, folder["id"], folder["link"], chunk_count)
        except Exception as exc:
            logger.exception("export to Google Drive failed for patch %s", patch_id)
            raise HTTPException(status_code=500, detail=f"Drive export failed: {exc}")
        finally:
            shutil.rmtree(package_dir, ignore_errors=True)

    return RedirectResponse(url=f"/books/{book_id}/patches/{patch_id}/chunks", status_code=303)


@router.get("/books/{book_id}/patches/{patch_id}/export/download")
def download_patch_export(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        zip_path = drive_export.build_export_zip(conn, patch)
    return FileResponse(
        str(zip_path),
        media_type="application/zip",
        filename=f"patch_{patch_id}_export.zip",
    )


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
            raise HTTPException(status_code=400, detail="this patch has never been exported to Drive")

        text = repository.build_patch_text(conn, patch)
        max_chars = patch.max_chars or settings.tts_max_chars
        expected_chunk_count = len(split_into_tts_chunks(text, max_chars=max_chars))

        chunk_dir = Path(settings.data_root) / "books" / str(book_id) / "patches" / f"{patch_id}_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)

        try:
            service = google_drive.get_drive_service(conn)
            drive_files = {f["name"]: f["id"] for f in google_drive.list_files(service, export.drive_folder_id)}

            imported = 0
            for i in range(expected_chunk_count):
                name = f"chunk_{i:03d}.wav"
                local_path = chunk_dir / name
                if local_path.exists():
                    imported += 1
                    continue
                if name not in drive_files:
                    break  # first missing chunk: stop here, contiguous prefix ends
                google_drive.download_file(service, drive_files[name], str(local_path))
                imported += 1

            if imported >= expected_chunk_count:
                book_dir = Path(settings.data_root) / "books" / str(book_id) / "patches"
                audio_path = str(book_dir / f"{patch_id}.wav")
                chunk_paths = [str(chunk_dir / f"chunk_{i:03d}.wav") for i in range(expected_chunk_count)]
                audio_merge.merge_chunk_files_to_patch(chunk_paths, audio_path)
                # Chunk files (downloaded from Drive) are intentionally kept on disk, same as
                # the local synthesis path in worker.py - not auto-deleted after merge.
                repository.mark_patch_done(conn, patch_id, audio_path)
                repository.update_patch_export(conn, export.id, status="imported", imported_chunk_count=imported)
            else:
                repository.update_patch_chunk_progress(conn, patch_id, imported)
                repository.update_patch_export(conn, export.id, status="partially_imported", imported_chunk_count=imported)
        except Exception as exc:
            logger.exception("import from Google Drive failed for patch %s", patch_id)
            repository.update_patch_export(conn, export.id, status="failed", error_message=str(exc))
            raise HTTPException(status_code=500, detail=f"Drive import failed: {exc}")

    return RedirectResponse(url=f"/books/{book_id}/patches/{patch_id}/chunks", status_code=303)
