"""Shared batch-import helpers: resolving a completed batch package's result WAV,
building a chapter timeline from imported chunks, and installing an imported WAV +
timeline sidecar atomically.

Extracted out of `app.routes.patches` so a background job (the Kaggle kernel-output
importer) can reuse the exact same install/validate logic the Drive Desktop import
route uses, without importing a FastAPI route module. Pure move: no behavior changed
from the original private functions in `app.routes.patches`."""
from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from pathlib import Path

import soundfile as sf

from app.youtube_metadata import load_timeline

logger = logging.getLogger(__name__)


def safe_batch_path(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def resolve_batch_result(patch_folder: Path, patch_id: int) -> Path | None:
    root = patch_folder.resolve()
    for parent in [root, *root.parents]:
        manifest_path = parent / "batch_manifest.json"
        if not manifest_path.is_file():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entry = next((item for item in manifest.get("patches", []) if item.get("patch_id") == patch_id), None)
            if not entry or not isinstance(entry.get("result_wav"), str):
                return None
            result = safe_batch_path(parent, entry["result_wav"])
            patch_manifest = safe_batch_path(parent, str(entry.get("folder", "")) + "/manifest.json")
            return result if result and patch_manifest and patch_manifest.is_file() else None
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None
    return None


def build_import_timeline(chunk_paths: list[Path], metadata: list[dict], pause_ms: int) -> dict | None:
    if not chunk_paths or len(chunk_paths) != len(metadata):
        return None
    try:
        infos = [sf.info(str(path)) for path in chunk_paths]
        rate = infos[0].samplerate
        # Chunks pair with chunk_paths by position - both are built from the same
        # chunk_NNN ordering - so the metadata carries no filename of its own.
        keys = {"chapter_index", "chapter_title", "is_chapter_start"}
        if any(set(item) != keys or info.samplerate != rate or info.channels != infos[0].channels
               for info, item in zip(infos, metadata)):
            return None
        pause = round(rate * pause_ms / 1000)
        starts, chapters = [], []
        frame = 0
        previous_index = None
        for index, (info, item) in enumerate(zip(infos, metadata)):
            chapter_index = item["chapter_index"]
            title = item["chapter_title"]
            marker = item["is_chapter_start"]
            if (isinstance(chapter_index, bool) or not isinstance(chapter_index, int) or
                    (previous_index is not None and chapter_index <= previous_index) or
                    not isinstance(title, str) or not title.strip() or not isinstance(marker, bool) or
                    (index == 0 and not marker) or
                    (index > 0 and marker != (chapter_index != previous_index))):
                return None
            previous_index = chapter_index
            starts.append(frame)
            if marker:
                chapters.append({"chapter_index": chapter_index, "start_frame": frame,
                                 "start_seconds": frame / rate, "title": title.strip()})
            frame += info.frames + pause
        total_frames = frame - pause
        if any(b - a < rate * 10 for a, b in zip(starts, starts[1:])) or total_frames - starts[-1] < rate * 10:
            return None
        return {"version": 1, "sample_rate": rate, "total_frames": total_frames, "chapters": chapters}
    except (OSError, TypeError, ValueError, KeyError, sf.SoundFileError):
        return None


def timeline_metadata(manifest: dict) -> list[dict]:
    """Reduce a patch manifest's chunk_metadata to the three fields
    build_import_timeline validates.

    Current exports are compact: entries carry no chapter_title (titles are
    de-duplicated into the chapter_titles map) and no filename. Older packages carry
    both, plus the chunk text - dropping the extras here is what lets them import with
    a chapter timeline too."""
    titles = manifest.get("chapter_titles") or {}
    metadata = []
    for item in manifest.get("chunk_metadata") or []:
        if not isinstance(item, dict):
            return []
        title = item.get("chapter_title")
        if not isinstance(title, str) or not title.strip():
            title = titles.get(str(item.get("chapter_index")))
        metadata.append({
            "chapter_index": item.get("chapter_index"),
            "chapter_title": title,
            "is_chapter_start": item.get("is_chapter_start"),
        })
    return metadata


def _atomic_copy(source: Path, target: Path) -> None:
    shutil.copy2(source, target)


def install_imported_wav(source: Path, audio_path: Path, timeline: dict | None = None) -> None:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    local_sidecar = audio_path.with_suffix(".timeline.json")
    temp_wav = audio_path.with_name(f".{audio_path.name}.{uuid.uuid4().hex}.tmp")
    temp_sidecar = local_sidecar.with_name(f".{local_sidecar.name}.{uuid.uuid4().hex}.tmp")
    try:
        sf.info(str(source))
        _atomic_copy(source, temp_wav)
        if timeline is None:
            timeline = load_timeline(source)
        if timeline is not None:
            temp_sidecar.write_text(json.dumps(timeline), encoding="utf-8")
        os.replace(temp_wav, audio_path)
        if timeline is not None:
            try:
                os.replace(temp_sidecar, local_sidecar)
            except OSError:
                logger.warning("Timeline persistence failed after local install", exc_info=True)
                try:
                    local_sidecar.unlink(missing_ok=True)
                except OSError:
                    logger.warning("Failed to remove stale timeline sidecar %s", local_sidecar, exc_info=True)
        else:
            try:
                local_sidecar.unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to remove stale timeline sidecar %s", local_sidecar, exc_info=True)
    except Exception:
        temp_wav.unlink(missing_ok=True)
        temp_sidecar.unlink(missing_ok=True)
        raise
    finally:
        for path in (temp_wav, temp_sidecar):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("failed to clean import staging path %s", path, exc_info=True)
