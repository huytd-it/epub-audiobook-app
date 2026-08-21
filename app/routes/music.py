"""Music library routes: upload, list, edit, delete, serve.

Tracks are editable the same way voice clips are (see app/routes/voices.py):
``/music/{id}/info`` reports what ffprobe can read and ``/music/{id}/process``
runs one ffmpeg pass of trims/cleanup from app/audio_process.py - either over
the file in place (every book pointing at the track picks the edit up, since
books reference music by id) or into a new library entry.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app import audio_process, repository
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


def _music_file(request: Request, music_id: int):
    """The library row plus its on-disk path, refusing anything outside the
    music directory (the stored path is data, and data can be wrong)."""
    with locked_conn(request) as conn:
        music = repository.get_music(conn, music_id)
    if music is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy nhạc")
    path = Path(music.file_path).resolve()
    root = _MUSIC_DIR.resolve()
    if root not in path.parents:
        raise HTTPException(status_code=403, detail="Đường dẫn không hợp lệ")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File nhạc không tồn tại")
    return music, path


def _unique_music_path(base: str) -> Path:
    """First free path for ``base`` in the music dir, suffixing _1, _2, ... on
    clash (same convention as the voice library)."""
    candidate = _MUSIC_DIR / base
    if not candidate.exists():
        return candidate
    stem, suffix = Path(base).stem, Path(base).suffix
    for index in range(1, 1000):
        candidate = _MUSIC_DIR / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=400, detail="Không tìm được tên file khả dụng")


def _clean_copy_name(raw: str, suffix: str) -> str:
    cleaned = re.sub(r"[^\w\-. ]", "", (raw or "").strip()).strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Tên mới không hợp lệ")
    if Path(cleaned).suffix.lower() != suffix.lower():
        cleaned += suffix.lower()
    return cleaned


@router.get("/music/{music_id}/info")
def music_info(music_id: int, request: Request):
    """Technical details + metadata for one track, for the audio editor."""
    music, path = _music_file(request, music_id)
    return {
        "id": music.id, "name": music.name,
        "description": music.description, "license": music.license,
        **audio_process.probe(path), "size": path.stat().st_size,
    }


@router.post("/music/{music_id}/process")
async def process_music(music_id: int, request: Request):
    """Trim/clean a track, in place or into a new library entry.

    Overwriting keeps both the path and the id stable, so every book already
    mixing this track picks up the edit with nothing to re-point. A copy is
    registered as its own row and inherits the original's notes/licence, so a
    cleaned track never lands back in the library unattributed.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Dữ liệu không hợp lệ")
    music, src = _music_file(request, music_id)

    info = audio_process.probe(src)
    try:
        ops = audio_process.parse_ops(body.get("ops"), info.get("duration_sec"))
    except audio_process.InvalidOps as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if ops.is_empty():
        raise HTTPException(status_code=400, detail="Chưa chọn thao tác xử lý nào")

    save_as_copy = body.get("save_as") == "copy"
    if save_as_copy:
        requested = str(body.get("new_name") or "").strip()
        base = _clean_copy_name(requested, src.suffix) if requested else f"{src.stem}_edited{src.suffix}"
        dest = _unique_music_path(base)
    else:
        dest = src

    try:
        audio_process.process(src, dest, ops, info.get("sample_rate"))
    except audio_process.AudioProcessError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    probed = audio_process.probe(dest)
    with locked_conn(request) as conn:
        if save_as_copy:
            display = str(body.get("new_name") or "").strip() or f"{music.name} (đã xử lý)"
            record = repository.create_music(
                conn, name=Path(display).stem, file_path=str(dest),
                duration_sec=probed.get("duration_sec"),
                description=music.description or "", license=music.license or "",
            )
        else:
            repository.update_music_duration(conn, music.id, probed.get("duration_sec"))
            record = repository.get_music(conn, music.id)
    return {
        "status": "ok", "applied": ops.summary(),
        "id": record.id, "name": record.name,
        "description": record.description, "license": record.license,
        **probed, "size": dest.stat().st_size,
    }


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
