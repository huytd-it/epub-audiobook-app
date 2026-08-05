"""Build batch packages for Colab/Kaggle synthesis and result re-import."""
from __future__ import annotations

import json
import re
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app import repository
from app.config import settings
from app.models import Book, Patch

_BATCH_NOTEBOOK_TEMPLATE = Path(__file__).parent / "assets" / "colab_kaggle_batch_tts_template.ipynb"
_TMP_DIR = Path(settings.data_root) / "tmp" / "patch_export"


def validate_sync_folder(folder_path: str) -> Path:
    raw = Path(folder_path.strip()).expanduser()
    if not raw.is_absolute():
        raise ValueError("Folder path must be absolute")
    path = raw.resolve()
    if not path.exists():
        raise ValueError("Folder path does not exist")
    if not path.is_dir():
        raise ValueError("Folder path is not a directory")
    try:
        with tempfile.NamedTemporaryFile(dir=path, prefix=".epub-audiobook-write-test-", delete=True):
            pass
    except OSError as exc:
        raise ValueError(f"Folder is not writable: {exc}") from exc
    return path


def publish_package(package_dir: Path, target_folder: str, folder_name: str) -> Path:
    target = validate_sync_folder(target_folder)
    final = target / folder_name
    if final.exists():
        raise FileExistsError(f"Export folder already exists: {final}")
    temp = Path(tempfile.mkdtemp(prefix=".epub-audiobook-export-", dir=target))
    try:
        shutil.copytree(package_dir, temp, dirs_exist_ok=True)
        temp.rename(final)
        return final
    except Exception:
        shutil.rmtree(temp, ignore_errors=True)
        raise


def _sanitize_name(name: str) -> str:
    """Strip characters that are unsafe in Drive/Windows file and folder names."""
    return re.sub(r"[^\w\- ]", "", name).strip()


def _voice_clip_or_raise(book: Book) -> Path:
    """The voice reference clip is mandatory for Colab/Kaggle exports: without it
    VoxCPM picks a different random voice per chunk/session, so chunks synthesized
    remotely would not match each other (or audio already generated locally).
    Called before any package files are written so a refused export leaves nothing
    behind."""
    if not book.voice_clip_path or not Path(book.voice_clip_path).exists():
        raise ValueError(
            f"book '{book.title}' has no voice reference clip - upload one on the "
            "book page before exporting, so every chunk is synthesized with the "
            "same voice"
        )
    return Path(book.voice_clip_path)


def _export_tts_config(book: Book, model_id: str, voice_id: str | None = None) -> tuple[dict, Path | None]:
    from app.tts_engine import list_tts_models

    models = {model["id"]: model for model in list_tts_models()}
    if model_id not in models:
        raise ValueError(f"unknown TTS model: {model_id}")
    model = models[model_id]
    reference = None
    if model["supports_reference"]:
        if voice_id:
            voice_name = Path(voice_id).name
            reference = Path(settings.data_root) / "voices" / voice_name
            if voice_name != voice_id or not reference.is_file():
                raise ValueError(f"unknown reference voice: {voice_id}")
        else:
            reference = _voice_clip_or_raise(book)
    if not model["supports_reference"] and not voice_id:
        voice_id = model.get("default_voice")
    if not model["supports_reference"] and not voice_id:
        raise ValueError(f"model '{model_id}' requires a voice or language")
    return {
        "model_id": model_id,
        "voice_id": voice_id,
        "options": {},
    }, reference


def _write_patch_files(
    conn: sqlite3.Connection,
    book: Book,
    patch: Patch,
    dest_dir: Path,
    reference_rel: str | None,
    tts: dict | None = None,
    max_chars: int = 0,
) -> dict:
    """Write manifest.json (with chunk text inlined) for one patch into dest_dir and
    return the manifest dict. The manifest is the only file a patch folder needs: the
    notebook synthesizes audio from it, and video is rendered back in the app."""
    plan = repository.build_patch_chunk_plan(conn, patch, max_chars=max_chars or None)
    if not plan:
        raise ValueError(f"patch {patch.id} has no text to export")

    dest_dir.mkdir(parents=True, exist_ok=True)

    chunk_metadata = []
    chapter_titles: dict[str, str] = {}
    for item in plan:
        # The text travels inside the manifest, so a whole patch is one file to
        # download instead of one per chunk. Everything derivable stays out of it too:
        # chunk_NNN names come from the position, and chapter_title is de-duplicated
        # into chapter_titles - the notebook and the import code rebuild both.
        if item["is_chapter_start"]:
            chapter_titles.setdefault(str(item["chapter_index"]), item["chapter_title"])
        chunk_metadata.append(
            {
                "chapter_index": item["chapter_index"],
                "is_chapter_start": item["is_chapter_start"],
                "text": item["text"],
            }
        )

    manifest = {
        "patch_id": patch.id,
        "book_id": patch.book_id,
        "book_title": book.title,
        "patch_name": patch.name or str(patch.patch_index),
        "chapter_start": patch.chapter_start,
        "chapter_end": patch.chapter_end,
        "max_chars": max_chars or patch.max_chars or settings.tts_max_chars,
        "chunk_count": len(plan),
        "chapter_titles": chapter_titles,
        "chunk_metadata": chunk_metadata,
        "reference_wav": reference_rel,
        "reference_transcript": book.voice_transcript or None,
        "voxcpm_model_id": "openbmb/VoxCPM2",
        "tts": tts or {"model_id": "voxcpm2", "voice_id": None, "options": {}},
    }
    (dest_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest


def folder_name_for_batch(book_title: str, patches: list[Patch]) -> str:
    safe_title = _sanitize_name(book_title) or "book"
    indices = [p.patch_index for p in patches]
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return (
        f"{safe_title} - batch {min(indices):03d}-{max(indices):03d} "
        f"({len(patches)} patches) - {timestamp}"
    )


def result_wav_name(patch: Patch) -> str:
    """Filename of the merged per-patch wav the batch notebook writes into result/."""
    label = _sanitize_name(patch.name or str(patch.patch_index)) or "patch"
    return f"{patch.patch_index:03d} - {label}.wav"


def result_mp4_name(patch: Patch) -> str:
    """Filename of the rendered per-patch MP4 the batch notebook writes into result/."""
    label = _sanitize_name(patch.name or str(patch.patch_index)) or "patch"
    return f"{patch.patch_index:03d} - {label}.mp4"


def build_batch_export_package(
    conn: sqlite3.Connection,
    patches: list[Patch],
    drive_folder_name: str | None = None,
    hf_token: str | None = None,
    model_id: str = "voxcpm2",
    voice_id: str | None = None,
    max_chars: int = 0,
    with_effects: bool = False,
) -> tuple[Path, dict]:
    """Write a multi-patch package: batch_manifest.json + the batch notebook at the
    root, one shared voice reference clip, and one manifest.json per patch under
    patches/. Nothing else travels: background images and music are only ever used by
    the app's own video rendering, so keeping them out keeps the Drive sync small.
    Returns (package_dir, batch_manifest); caller is responsible for deleting the directory."""
    if not patches:
        raise ValueError("no patches to export")
    book_ids = {p.book_id for p in patches}
    if len(book_ids) != 1:
        raise ValueError("all patches in a batch must belong to the same book")
    book = repository.get_book(conn, patches[0].book_id)
    if book is None:
        raise ValueError(f"book {patches[0].book_id} not found")
    tts, voice_clip = _export_tts_config(book, model_id, voice_id)
    tts["options"]["with_effects"] = with_effects

    patches = sorted(patches, key=lambda p: p.patch_index)

    _TMP_DIR.mkdir(parents=True, exist_ok=True)
    package_dir = _TMP_DIR / f"batch_{uuid.uuid4().hex[:8]}"
    package_dir.mkdir(parents=True, exist_ok=True)

    reference_wav_name = None
    if voice_clip is not None:
        reference_wav_name = "reference" + voice_clip.suffix
        shutil.copyfile(voice_clip, package_dir / reference_wav_name)

    patch_entries = []
    for patch in patches:
        folder_rel = f"patches/patch_{patch.patch_index:03d}"
        reference_rel = f"../../{reference_wav_name}" if reference_wav_name else None
        manifest = _write_patch_files(
            conn, book, patch, package_dir / folder_rel, reference_rel,
            tts=tts, max_chars=max_chars,
        )
        patch_entries.append({
            "patch_id": patch.id,
            "patch_index": patch.patch_index,
            "folder": folder_rel,
            "patch_name": manifest["patch_name"],
            "chapter_start": patch.chapter_start,
            "chapter_end": patch.chapter_end,
            "max_chars": manifest["max_chars"],
            "chunk_count": manifest["chunk_count"],
            "result_wav": f"result/{result_wav_name(patch)}",
            "result_mp4": f"result/{result_mp4_name(patch)}",
        })

    timestamp = datetime.now(timezone.utc)
    batch_id = f"{timestamp.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    # Render settings only - the media they refer to stays in the app, which is where
    # video is rendered from the imported WAV.
    video_config = {
        "resolution": book.video_resolution or "1920x1080",
        "fps": book.video_fps or 30,
        "youtube_privacy": settings.youtube_default_privacy,
    }
    batch_manifest = {
        "format": "epub-audiobook-batch-v1",
        "batch_id": batch_id,
        "book_id": book.id,
        "book_title": book.title,
        "created_at": timestamp.isoformat(),
        "voxcpm_model_id": "openbmb/VoxCPM2",
        "tts": tts,
        "reference_wav": reference_wav_name,
        "reference_transcript": book.voice_transcript or None,
        "patch_count": len(patch_entries),
        "patches": patch_entries,
        "video_config": video_config,
    }
    (package_dir / "batch_manifest.json").write_text(
        json.dumps(batch_manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    folder_name = drive_folder_name or folder_name_for_batch(book.title, patches)
    notebook_src = _BATCH_NOTEBOOK_TEMPLATE.read_text(encoding="utf-8")
    notebook_src = notebook_src.replace("__BATCH_ID__", batch_id)
    notebook_src = notebook_src.replace(
        "__DEFAULT_FOLDER_NAME__", json.dumps(folder_name)[1:-1]
    )
    notebook_src = notebook_src.replace("__HF_TOKEN__", (hf_token or settings.hf_token) or "")
    (package_dir / "colab_kaggle_batch_tts_template.ipynb").write_text(notebook_src, encoding="utf-8")

    return package_dir, batch_manifest


def build_batch_export_zip(
    conn: sqlite3.Connection,
    patches: list[Patch],
    drive_folder_name: str | None = None,
    hf_token: str | None = None,
    **tts_options,
) -> Path:
    """Same package as build_batch_export_package, zipped up for local download."""
    package_dir, _ = build_batch_export_package(
        conn, patches, drive_folder_name=drive_folder_name, hf_token=hf_token,
        **tts_options,
    )
    try:
        zip_path = shutil.make_archive(str(package_dir), "zip", root_dir=package_dir)
    finally:
        shutil.rmtree(package_dir, ignore_errors=True)
    return Path(zip_path)
