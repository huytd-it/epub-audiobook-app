from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app.config import settings

router = APIRouter(prefix="/local-bridge")


def _local_only(request: Request) -> None:
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(403, detail="native bridge is available on localhost only")


@router.get("/health")
def health(request: Request):
    _local_only(request)
    return {"status": "ok", "capabilities": ["pick-files", "pick-folder"]}


@router.post("/pick-files")
def pick_files(request: Request):
    _local_only(request)
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        paths = list(filedialog.askopenfilenames(parent=root, title="Chọn file để upload"))
        root.destroy()
    except Exception as exc:
        raise HTTPException(503, detail=f"native picker unavailable: {exc}") from exc
    return {"paths": paths}


@router.post("/pick-folder")
def pick_folder(request: Request):
    _local_only(request)
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        path = filedialog.askdirectory(parent=root, title="Chọn thư mục")
        root.destroy()
    except Exception as exc:
        raise HTTPException(503, detail=f"native picker unavailable: {exc}") from exc
    return {"path": path or None}


@router.post("/books/{book_id}/patches/{patch_id}/open-folder")
def open_patch_media_folder(request: Request, book_id: int, patch_id: int):
    """Open the local directory containing a patch's audio, chunks and sidecars."""
    _local_only(request)
    parent = Path(settings.data_root) / "books" / str(book_id) / "patches"
    parent.mkdir(parents=True, exist_ok=True)
    folder = parent / f"{patch_id}_chunks"
    # Chunk snapshots are the only per-patch media directory. Fall back to the
    # shared audio directory when this patch has not produced chunk files yet.
    if not folder.is_dir():
        folder = parent
    try:
        os.startfile(str(folder))
    except OSError as exc:
        raise HTTPException(503, detail=f"cannot open patch folder: {exc}") from exc
    return {"path": str(folder), "patch_id": patch_id}
