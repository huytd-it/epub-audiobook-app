"""Build the exportable package (manifest + chunk texts + notebook template) for a
patch, so it can be synthesized on Google Colab/Kaggle and the resulting audio
re-imported. See app/google_drive.py for the Drive API calls and
app/routes/patches.py for the export/import routes that tie it together.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from pathlib import Path

from app import repository
from app.chunker import split_into_tts_chunks
from app.config import settings
from app.models import Patch

_NOTEBOOK_TEMPLATE = Path(__file__).parent / "assets" / "colab_kaggle_tts_template.ipynb"
_TMP_DIR = Path(settings.data_root) / "tmp" / "patch_export"


def build_export_package(
    conn: sqlite3.Connection,
    patch: Patch,
    drive_folder_name: str | None = None,
    hf_token: str | None = None,
) -> Path:
    """Write manifest.json + chunk_NNN.txt + (optional) voice reference + the notebook
    template into a fresh temp directory. Caller is responsible for deleting it.

    ``drive_folder_name`` is baked into the notebook so its Colab cell can locate the
    exported folder automatically. If not given (e.g. the plain local-download path),
    the deterministic per-patch name is used as the fallback default.
    """
    book = repository.get_book(conn, patch.book_id)
    if book is None:
        raise ValueError(f"book {patch.book_id} not found")

    text = repository.build_patch_text(conn, patch)
    max_chars = patch.max_chars or settings.tts_max_chars
    chunks = split_into_tts_chunks(text, max_chars=max_chars)
    if not chunks:
        raise ValueError("patch has no text to export")

    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    package_dir = _TMP_DIR / f"patch_{patch.id}_{uuid.uuid4().hex[:8]}"
    package_dir.mkdir(parents=True, exist_ok=True)

    chunk_filenames = []
    for i, chunk_text in enumerate(chunks):
        filename = f"chunk_{i:03d}.txt"
        (package_dir / filename).write_text(chunk_text, encoding="utf-8")
        chunk_filenames.append(filename)

    reference_wav_name = None
    if book.voice_clip_path and Path(book.voice_clip_path).exists():
        reference_wav_name = "reference" + Path(book.voice_clip_path).suffix
        shutil.copyfile(book.voice_clip_path, package_dir / reference_wav_name)

    manifest = {
        "patch_id": patch.id,
        "book_id": patch.book_id,
        "book_title": book.title,
        "patch_name": patch.name or str(patch.patch_index),
        "chapter_start": patch.chapter_start,
        "chapter_end": patch.chapter_end,
        "max_chars": max_chars,
        "chunk_count": len(chunks),
        "chunks": chunk_filenames,
        "reference_wav": reference_wav_name,
        "reference_transcript": book.voice_transcript or None,
        "voxcpm_model_id": "openbmb/VoxCPM2",
        "expected_outputs": [f"chunk_{i:03d}.wav" for i in range(len(chunks))],
    }
    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Bake the patch id + folder name + HF token into the notebook so its cells can find
    # the exported folder and authenticate automatically. Kept as simple placeholder
    # substitutions rather than parsing/rewriting nbformat cells.
    folder_name = drive_folder_name or folder_name_for_patch(book.title, patch)
    notebook_src = _NOTEBOOK_TEMPLATE.read_text(encoding="utf-8")
    notebook_src = notebook_src.replace("__PATCH_ID__", str(patch.id))
    notebook_src = notebook_src.replace(
        "__DEFAULT_FOLDER_NAME__", json.dumps(folder_name)[1:-1]  # escape for JSON string literal
    )
    notebook_src = notebook_src.replace("__HF_TOKEN__", (hf_token or settings.hf_token) or "")
    (package_dir / "colab_kaggle_tts_template.ipynb").write_text(notebook_src, encoding="utf-8")

    return package_dir


def build_export_zip(
    conn: sqlite3.Connection,
    patch: Patch,
    hf_token: str | None = None,
) -> Path:
    """Same package as build_export_package, zipped up for local download (the safety
    net that works even without connecting Google Drive)."""
    package_dir = build_export_package(conn, patch, hf_token=hf_token)
    try:
        zip_path = shutil.make_archive(str(package_dir), "zip", root_dir=package_dir)
    finally:
        shutil.rmtree(package_dir, ignore_errors=True)
    return Path(zip_path)


def folder_name_for_patch(book_title: str, patch: Patch) -> str:
    import re as _re
    from datetime import datetime, timezone

    safe_title = _re.sub(r"[^\w\- ]", "", book_title).strip() or "book"
    label = patch.name or str(patch.patch_index)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{safe_title} - patch {label} - {timestamp}"
