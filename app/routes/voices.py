"""Voice (reference clip) library routes: page, upload, rename, delete, serve.

The library is the data/voices directory. Books reference voice clips by
absolute path in book.voice_clip_path, so renaming/deleting a voice clip also
updates the books that pointed at it - mirroring the photo manager
(app/routes/photos.py) for the backgrounds directory.
"""
from __future__ import annotations

import math
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from app import repository
from app.config import settings
from app.deps import locked_conn

router = APIRouter()

ALLOWED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".ogg"}
_MIME_MAP = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg"}


def _voices_dir() -> Path:
    """Resolved at call time (not import time) so tests can repoint data_root."""
    d = Path(settings.data_root) / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_voice_path(name: str) -> Path:
    """Resolve a filename inside the voices dir, refusing path traversal."""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="Tên file không hợp lệ")
    return _voices_dir() / name


def _clean_new_name(new_name: str, suffix: str) -> str:
    """Sanitize a user-provided voice name and ensure it keeps the original
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




@router.get("/voices/file/{name}")
def serve_voice(name: str):
    p = _safe_voice_path(name)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy voice")
    media = _MIME_MAP.get(p.suffix.lower(), "application/octet-stream")
    return FileResponse(str(p), media_type=media)


@router.post("/voices/upload")
async def upload_voices(files: list[UploadFile] = File(...)):
    dest_dir = _voices_dir()
    for file in files:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in ALLOWED_AUDIO_EXTENSIONS:
            continue
        base = Path(file.filename or f"voice{ext}").name
        dest = dest_dir / base
        if dest.exists():
            dest = dest_dir / f"{uuid.uuid4().hex[:8]}_{base}"
        with open(dest, "wb") as out:
            shutil.copyfileobj(file.file, out)
    return RedirectResponse(url="/voices", status_code=303)


@router.post("/voices/rename")
def rename_voice(
    request: Request,
    old_name: str = Form(...),
    new_name: str = Form(default=""),
):
    src = _safe_voice_path(old_name)
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy voice")
    dest = _voices_dir() / _clean_new_name(new_name, src.suffix)
    if dest == src:
        return RedirectResponse(url="/voices", status_code=303)
    if dest.exists():
        raise HTTPException(status_code=400, detail=f"Đã có voice tên '{dest.name}'")

    # Rename inside the db lock so a book can't grab the old path mid-rename;
    # then repoint every book that referenced the old file.
    with locked_conn(request) as conn:
        src.rename(dest)
        conn.execute(
            "UPDATE book SET voice_clip_path = ?, updated_at = ? "
            "WHERE voice_clip_path = ?",
            (str(dest), datetime.now(timezone.utc).isoformat(), str(src)),
        )
        repository.rename_voice_meta(conn, old_name, dest.name)
        conn.commit()
    return RedirectResponse(url="/voices", status_code=303)


@router.post("/voices/{name}/description")
async def update_voice_description(name: str, request: Request):
    body = await request.json()
    description = body.get("description", "")
    p = _safe_voice_path(name)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy voice")
    with locked_conn(request) as conn:
        repository.set_voice_meta(conn, name, description)
    return {"status": "ok"}


@router.post("/voices/delete")
def delete_voice(request: Request, name: str = Form(...)):
    p = _safe_voice_path(name)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy voice")
    with locked_conn(request) as conn:
        repository.delete_voice_meta(conn, name)
        conn.execute(
            "UPDATE book SET voice_clip_path = NULL, updated_at = ? "
            "WHERE voice_clip_path = ?",
            (datetime.now(timezone.utc).isoformat(), str(p)),
        )
        conn.commit()
        p.unlink(missing_ok=True)
    return RedirectResponse(url="/voices", status_code=303)
