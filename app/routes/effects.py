"""Global sound effect library routes: CRUD, bulk add/edit."""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app import repository
from app.config import settings
from app.deps import locked_conn

logger = logging.getLogger(__name__)

router = APIRouter()

_EFFECTS_DIR = Path(settings.data_root) / "effects"
_MIME_MAP = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".ogg": "audio/ogg"}




@router.get("/effects/list")
def list_effects(request: Request):
    with locked_conn(request) as conn:
        effects = repository.list_sound_effects(conn)
    return JSONResponse({"effects": effects})


@router.post("/effects/add")
async def add_effect(
    request: Request,
    marker: str = Form(...),
    file: UploadFile = File(...),
    description: str = Form(default=""),
):
    _EFFECTS_DIR.mkdir(parents=True, exist_ok=True)
    ext = Path(file.filename or "").suffix or ".wav"
    safe_name = marker.strip("[]").replace(" ", "_").replace("/", "_")
    dest = _EFFECTS_DIR / f"{safe_name}{ext}"
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    with locked_conn(request) as conn:
        eid = repository.create_sound_effect(conn, None, marker, str(dest), description)
    return JSONResponse({"ok": True, "id": eid})


@router.post("/effects/{effect_id}/edit")
async def edit_effect(request: Request, effect_id: int):
    body = await request.json()
    marker = body.get("marker", "").strip()
    description = body.get("description", "")
    if not marker:
        raise HTTPException(status_code=400, detail="marker required")
    with locked_conn(request) as conn:
        effect = repository.get_sound_effect(conn, effect_id)
        if effect is None:
            raise HTTPException(status_code=404, detail="effect not found")
        repository.update_sound_effect(conn, effect_id, marker, description)
    return JSONResponse({"ok": True})


@router.post("/effects/{effect_id}/delete")
def delete_effect(request: Request, effect_id: int):
    with locked_conn(request) as conn:
        effect = repository.get_sound_effect(conn, effect_id)
        if effect is None:
            raise HTTPException(status_code=404, detail="effect not found")
        p = Path(effect["file_path"])
        if p.exists():
            p.unlink(missing_ok=True)
        repository.delete_sound_effect(conn, effect_id)
    return JSONResponse({"ok": True})


@router.get("/effects/{effect_id}/audio")
def serve_effect_audio(request: Request, effect_id: int):
    with locked_conn(request) as conn:
        effect = repository.get_sound_effect(conn, effect_id)
    if effect is None:
        raise HTTPException(status_code=404, detail="effect not found")
    p = Path(effect["file_path"]).resolve()
    if not p.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(p), media_type=_MIME_MAP.get(p.suffix.lower(), "application/octet-stream"))


@router.post("/effects/bulk-add")
async def bulk_add_effects(request: Request, files: list[UploadFile] = File(...)):
    """Upload multiple audio files. Marker = filename without extension."""
    _EFFECTS_DIR.mkdir(parents=True, exist_ok=True)

    # Write every upload to disk first, off the lock and off the event loop; the shared
    # connection is only taken for the short inserts afterwards.
    def _save_all() -> list[tuple[str, Path]]:
        saved = []
        for f in files:
            stem = Path(f.filename or "unknown").stem
            ext = Path(f.filename or "").suffix or ".wav"
            dest = _EFFECTS_DIR / f"{stem}{ext}"
            with open(dest, "wb") as out:
                shutil.copyfileobj(f.file, out)
            saved.append((f"[{stem}]", dest))
        return saved

    saved = await asyncio.to_thread(_save_all)

    added = []
    with locked_conn(request) as conn:
        for marker, dest in saved:
            eid = repository.create_sound_effect(conn, None, marker, str(dest), "")
            added.append({"id": eid, "marker": marker})
    return JSONResponse({"ok": True, "added": len(added), "effects": added})
