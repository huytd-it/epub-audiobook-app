"""Photo (background image) library routes: page, upload, rename, delete, serve.

The library is the data/backgrounds directory - the same folder the Video
Creator (/video/backgrounds) and the book background picker read from. Files
are addressed by filename, and books reference them by absolute path in
book.background_image_path, so renaming/deleting a photo also updates the
books that pointed at it.
"""
from __future__ import annotations

import math
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from app import media_library
from app.config import settings
from app.deps import locked_conn

router = APIRouter()

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov"}
ALLOWED_MEDIA_EXTENSIONS = ALLOWED_IMAGE_EXTENSIONS | ALLOWED_VIDEO_EXTENSIONS
_MIME_MAP = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime"}


def _backgrounds_dir() -> Path:
    """Resolved at call time (not import time) so tests can repoint data_root."""
    d = Path(settings.data_root) / "backgrounds"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_photo_path(name: str) -> Path:
    """Resolve a filename inside the backgrounds dir, refusing path traversal."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")
    return _backgrounds_dir() / name


def _clean_new_name(new_name: str, suffix: str) -> str:
    """Sanitize a user-provided photo name and ensure it keeps the original
    (allowed) extension."""
    cleaned = new_name.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        raise HTTPException(status_code=400, detail="Tên mới không hợp lệ")
    cleaned = re.sub(r"[^\w\-. ]", "", cleaned).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Tên mới không hợp lệ")
    if Path(cleaned).suffix.lower() != suffix.lower():
        cleaned += suffix.lower()
    return cleaned




@router.get("/photos/file/{name}")
def serve_photo(name: str):
    p = _safe_photo_path(name)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh")
    media = _MIME_MAP.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(str(p), media_type=media)


@router.post("/photos/upload")
async def upload_photos(files: list[UploadFile] = File(...)):
    dest_dir = _backgrounds_dir()
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_MEDIA_EXTENSIONS:
            continue
        base = Path(file.filename or f"photo{ext}").name
        dest = dest_dir / base
        if dest.exists():
            dest = dest_dir / f"{uuid.uuid4().hex[:8]}_{base}"
        with open(dest, "wb") as out:
            shutil.copyfileobj(file.file, out)
    return RedirectResponse(url="/photos", status_code=303)


@router.post("/photos/rename")
def rename_photo(
    request: Request,
    old_name: str = Form(...),
    new_name: str = Form(default=""),
):
    src = _safe_photo_path(old_name)
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh")
    dest = _backgrounds_dir() / _clean_new_name(new_name, src.suffix)
    if dest == src:
        return RedirectResponse(url="/photos", status_code=303)
    if dest.exists():
        raise HTTPException(status_code=400, detail=f"Đã có ảnh tên '{dest.name}'")

    # Rename inside the db lock so a book can't grab the old path mid-rename;
    # then repoint every book that referenced the old file.
    with locked_conn(request) as conn:
        src.rename(dest)
        media_library.replace_background_reference(conn, str(src), str(dest))
        conn.commit()
    return RedirectResponse(url="/photos", status_code=303)


@router.post("/photos/delete")
def delete_photo(request: Request, name: str = Form(...)):
    p = _safe_photo_path(name)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy ảnh")
    with locked_conn(request) as conn:
        media_library.delete_background(conn, p)
    return RedirectResponse(url="/photos", status_code=303)
