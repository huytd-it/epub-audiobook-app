"""Music library routes: upload, list, delete, serve."""
from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app import repository
from app.config import settings
from app.deps import locked_conn


router = APIRouter()

_MUSIC_DIR = Path(settings.data_root) / "music"
_ALLOWED_EXTENSIONS = {".mp3", ".wav", ".ogg", ".m4a"}
_MIME_MAP = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg", ".m4a": "audio/mp4"}


def _probe_duration(file_path: str) -> float | None:
    try:
        result = subprocess.run(
            [settings.get_ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            capture_output=True, text=True, timeout=30,
        )
        val = result.stdout.strip()
        return float(val) if val else None
    except Exception:
        return None




@router.get("/music/list")
def list_music_api(request: Request):
    with locked_conn(request) as conn:
        music_list = repository.list_music(conn)
    return JSONResponse({"music": [
        {"id": m.id, "name": m.name, "duration_sec": m.duration_sec,
         "description": m.description, "license": m.license}
        for m in music_list
    ]})


@router.post("/music/upload")
async def upload_music(request: Request, files: list[UploadFile] = File(...)):
    max_bytes = settings.music_max_size_mb * 1024 * 1024
    _MUSIC_DIR.mkdir(parents=True, exist_ok=True)

    with locked_conn(request) as conn:
        for file in files:
            ext = Path(file.filename or "").suffix.lower()
            if ext not in _ALLOWED_EXTENSIONS:
                continue
            safe_name = f"{uuid.uuid4().hex[:8]}_{Path(file.filename or 'music').name}"
            dest = _MUSIC_DIR / safe_name
            size = 0
            with open(dest, "wb") as out:
                while chunk := await file.read(65536):
                    size += len(chunk)
                    if size > max_bytes:
                        out.close()
                        dest.unlink(missing_ok=True)
                        break
                    out.write(chunk)
            if size > max_bytes:
                continue
            duration = _probe_duration(str(dest))
            display_name = Path(file.filename or safe_name).stem
            repository.create_music(conn, name=display_name, file_path=str(dest), duration_sec=duration)

    return RedirectResponse(url="/music", status_code=303)


@router.post("/music/{music_id}/metadata")
def update_music_metadata(
    request: Request, music_id: int,
    description: str = Form(default=""), license: str = Form(default=""),
):
    with locked_conn(request) as conn:
        if repository.get_music(conn, music_id) is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy nhạc")
        repository.update_music_metadata(conn, music_id, description.strip(), license.strip())
    return RedirectResponse(url="/music", status_code=303)


@router.post("/music/{music_id}/rename")
def rename_music(request: Request, music_id: int, name: str = Form(...)):
    new_name = name.strip()
    if not new_name:
        raise HTTPException(status_code=400, detail="Tên không được để trống")
    with locked_conn(request) as conn:
        music = repository.get_music(conn, music_id)
        if music is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy nhạc")
        repository.rename_music(conn, music_id, new_name)
    return RedirectResponse(url="/music", status_code=303)


@router.post("/music/{music_id}/delete")
def delete_music(request: Request, music_id: int):
    with locked_conn(request) as conn:
        music = repository.get_music(conn, music_id)
        if music is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy nhạc")
        Path(music.file_path).unlink(missing_ok=True)
        repository.delete_music(conn, music_id)
    return RedirectResponse(url="/music", status_code=303)


@router.get("/music/{music_id}/file")
def serve_music_file(music_id: int, request: Request):
    with locked_conn(request) as conn:
        music = repository.get_music(conn, music_id)
    if music is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhạc")

    p = Path(music.file_path).resolve()
    allowed_root = _MUSIC_DIR.resolve()
    if allowed_root not in p.parents and p != allowed_root:
        raise HTTPException(status_code=403, detail="Đường dẫn không hợp lệ")
    if not p.exists():
        raise HTTPException(status_code=404, detail="File nhạc không tồn tại")

    ext = p.suffix.lower()
    media_type = _MIME_MAP.get(ext, "application/octet-stream")
    return FileResponse(str(p), media_type=media_type)
