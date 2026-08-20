"""Voice (reference clip) library routes: page, upload, classify, edit, serve.

The library is the data/voices directory. Books reference voice clips by
absolute path in book.voice_clip_path, so renaming/deleting a voice clip also
updates the books that pointed at it - mirroring the photo manager
(app/routes/photos.py) for the backgrounds directory.

On top of the file management, clips carry a classification (gender + story
genres, see app/voice_taxonomy.py) so the library can be filtered, and can be
trimmed/cleaned in place through app/audio_process.py.
"""
from __future__ import annotations

import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from app import audio_process, repository, voice_taxonomy
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




def _repoint_voice_selection(conn, old_name: str, new_name: str | None) -> None:
    """Follow a renamed (or deleted) clip through every place a voice is *selected*.

    Since the audio settings' voice id is the filename of the clip a cloning model clones,
    a stale id is no longer harmless: the next TTS job would fail with "unknown reference
    voice". Books carry their own id in tts_voice_id / export_tts_voice_id, and the ones
    that inherit read it from the global audio defaults, so all three have to follow the
    file. Does not commit - the caller owns the transaction, exactly like the
    book.voice_clip_path sweep next to each call."""
    from app.production_defaults import (
        get_global_production_defaults,
        save_global_production_defaults,
    )

    for column in ("tts_voice_id", "export_tts_voice_id"):
        conn.execute(f"UPDATE book SET {column} = ? WHERE {column} = ?", (new_name, old_name))
    audio = dict(get_global_production_defaults(conn)["audio"])
    if audio.get("voice_id") == old_name:
        audio["voice_id"] = new_name or ""
        save_global_production_defaults(conn, {"audio": audio})


def _meta_payload(name: str, meta: dict | None) -> dict:
    """Shape one clip's metadata for the API (genre as a list, not a raw column)."""
    return {
        "name": name,
        "description": (meta or {}).get("description", ""),
        "gender": (meta or {}).get("gender", ""),
        "genre": voice_taxonomy.split_genres((meta or {}).get("genre", "")),
    }


def _unique_dest(dest_dir: Path, name: str) -> Path:
    """First free path for `name` in dest_dir, suffixing _1, _2, ... on clash."""
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    for index in range(1, 1000):
        candidate = dest_dir / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise HTTPException(status_code=400, detail="Không tìm được tên file khả dụng")


@router.get("/voices/taxonomy")
def voice_taxonomy_options():
    """Gender/genre vocabulary for the classification pickers and filters."""
    return {"genders": voice_taxonomy.GENDERS, "genres": voice_taxonomy.GENRES}


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
        _repoint_voice_selection(conn, src.name, dest.name)
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


@router.post("/voices/{name}/meta")
async def update_voice_meta(name: str, request: Request):
    """Save description + classification in one call.

    Every field is optional and an omitted one is left untouched, so the editor
    can save just the tags without resending the description.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Dữ liệu không hợp lệ")
    p = _safe_voice_path(name)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy voice")

    try:
        gender = (
            voice_taxonomy.normalize_gender(body["gender"]) if "gender" in body else None
        )
        genre = (
            voice_taxonomy.normalize_genres(body["genre"]) if "genre" in body else None
        )
    except voice_taxonomy.InvalidTag as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    description = body.get("description")
    if description is not None:
        description = str(description).strip()

    with locked_conn(request) as conn:
        repository.set_voice_meta(conn, name, description, gender, genre)
        meta = repository.get_voice_meta(conn, name)
    return {"status": "ok", **_meta_payload(name, meta)}


@router.get("/voices/{name}/info")
def voice_info(name: str, request: Request):
    """Technical details + metadata for one clip, for the audio editor."""
    p = _safe_voice_path(name)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy voice")
    with locked_conn(request) as conn:
        meta = repository.get_voice_meta(conn, name)
    # stat() last: it is always right, while probe's size is absent when ffprobe
    # is unavailable.
    return {**_meta_payload(name, meta), **audio_process.probe(p), "size": p.stat().st_size}


@router.post("/voices/{name}/process")
async def process_voice(name: str, request: Request):
    """Trim/clean a clip, either in place or into a new file.

    Overwriting keeps the path stable, so every book already pointing at the
    clip picks up the cleaned audio with no reference rewriting. Saving a copy
    inherits the original's classification (repository.copy_voice_meta) so a
    cleaned clip does not land back in the library untagged.
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Dữ liệu không hợp lệ")
    src = _safe_voice_path(name)
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail="Không tìm thấy voice")

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
        base = _clean_new_name(requested, src.suffix) if requested else f"{src.stem}_edited{src.suffix}"
        dest = _unique_dest(_voices_dir(), base)
    else:
        dest = src

    try:
        audio_process.process(src, dest, ops, info.get("sample_rate"))
    except audio_process.AudioProcessError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    with locked_conn(request) as conn:
        if save_as_copy:
            repository.copy_voice_meta(conn, name, dest.name)
        meta = repository.get_voice_meta(conn, dest.name)
    return {
        "status": "ok",
        "applied": ops.summary(),
        **_meta_payload(dest.name, meta),
        **audio_process.probe(dest),
        "size": dest.stat().st_size,
    }


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
        _repoint_voice_selection(conn, p.name, None)
        conn.commit()
        p.unlink(missing_ok=True)
    return RedirectResponse(url="/voices", status_code=303)
