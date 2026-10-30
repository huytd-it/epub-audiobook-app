"""Ghép patch ở local từ chunk WAV mà notebook Colab/Kaggle tổng hợp.

Notebook chỉ còn tổng hợp chunk (``patches/patch_NNN/output/chunk_NNN.wav``) và ghi
một marker ``result/<NNN - tên>.chunks.json`` khi patch đủ chunk; nó không ghép nữa.
App tải chunk về và ghép bằng đúng đường của TTS local (chuẩn âm lượng từng chunk,
pause theo chương, effects, normalize, timeline, phụ đề), nên audio từ notebook và
audio tổng hợp tại chỗ cùng một kiểu.

Chunk được cài vào kho chunk theo chương (app/chunk_store.py) như chunk tổng hợp tại
chỗ, nên bảng Mục lục / tab Audio của chương thấy chúng. Key mang tiền tố
``remote:``: lần TTS local sau với engine thật không coi chúng là kết quả của engine
đó, nhưng preview theo text vẫn dùng được.
"""
from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import soundfile as sf

from app import audio_merge, chunk_store, repository
from app.audio_mastering import level_chunk_loudness

CHUNK_MARKER_SUFFIX = ".chunks.json"
_PATCH_FOLDER_PREFIX = "patch_"


class ChunkImportError(ValueError):
    """User-facing reason a patch's chunks cannot be assembled.

    ``transient`` marks a state the next poll can outgrow (Drive not listing every
    chunk yet); everything else needs a person - a re-export, a re-run."""

    def __init__(self, message: str, *, transient: bool = False):
        super().__init__(message)
        self.transient = transient


@dataclass(frozen=True)
class PatchChunkSource:
    """One patch's notebook output on local disk: its manifest and output/ folder."""

    manifest: dict
    output_dir: Path


def marker_name(result_wav_name: str) -> str:
    """``NNN - name.wav`` -> ``NNN - name.chunks.json`` (the notebook's done marker)."""
    return Path(result_wav_name).stem + CHUNK_MARKER_SUFFIX


def read_patch_folder(folder: Path) -> PatchChunkSource:
    """Load ``folder/manifest.json`` and point at ``folder/output``."""
    manifest_path = folder / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ChunkImportError(f"không đọc được {manifest_path.name} của {folder.name}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ChunkImportError(f"{manifest_path.name} của {folder.name} không hợp lệ")
    return PatchChunkSource(manifest=manifest, output_dir=folder / "output")


def find_patch_folders(root: Path) -> Iterator[Path]:
    """Every ``patch_NNN`` folder holding a manifest.json anywhere under ``root``.

    Covers a downloaded batch folder, its ``patches/`` folder, a single patch folder,
    and Drive's "download folder" zips, which wrap everything in one extra level."""
    candidates = [root, *root.rglob(f"{_PATCH_FOLDER_PREFIX}*")]
    for path in candidates:
        if path.is_dir() and path.name.startswith(_PATCH_FOLDER_PREFIX) and (path / "manifest.json").is_file():
            yield path


def extract_chunk_zip(archive: Path, dest: Path) -> None:
    """Unpack only the files a patch import reads (manifest.json, output/chunk_*.wav,
    batch_manifest.json), refusing absolute or parent-relative member names.

    Drive splits a big folder download into several zips; extracting all of them into
    the same ``dest`` reassembles each patch folder."""
    with zipfile.ZipFile(archive) as zf:
        for member in zf.infolist():
            name = member.filename.replace("\\", "/")
            parts = name.split("/")
            if member.is_dir() or name.startswith("/") or ".." in parts or ":" in name:
                continue
            base = parts[-1]
            wanted = (base in {"manifest.json", "batch_manifest.json"}
                      or (base.startswith("chunk_") and base.endswith(".wav")))
            if not wanted:
                continue
            target = dest.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, open(target, "wb") as out:
                while block := src.read(1 << 20):
                    out.write(block)


def import_from_drive(db: Callable[[], AbstractContextManager], service, batch_folder_id: str,
                      patch, staging: Path) -> str:
    """Download one patch's chunks from the batch's Drive folder and assemble them."""
    from app import google_drive

    folder_rel = f"patches/{_PATCH_FOLDER_PREFIX}{patch.patch_index:03d}"
    try:
        local = google_drive.download_patch_chunks(service, batch_folder_id, folder_rel, staging)
    except FileNotFoundError as exc:
        raise ChunkImportError(str(exc), transient=True) from exc
    try:
        return assemble_patch_from_chunks(db, patch, read_patch_folder(local))
    finally:
        shutil.rmtree(local, ignore_errors=True)


def assemble_patch_from_chunks(
    db: Callable[[], AbstractContextManager],
    patch,
    source: PatchChunkSource,
) -> str:
    """Install ``source``'s chunks into the chapter stores and merge the patch audio.

    ``db()`` yields the connection and is entered only around the short DB reads
    (and the effects mix), so a route can pass its locked_conn and keep the heavy
    file I/O off the shared connection. Returns the patch audio path; the caller
    marks the patch done and runs its automation."""
    from app.jobqueue.handlers.audiobook_tts import merge_patch_audio
    from app.production_defaults import get_effective_audio_config

    manifest = source.manifest
    if manifest.get("patch_id") not in (None, patch.id):
        raise ChunkImportError(
            f"manifest.json thuộc patch {manifest.get('patch_id')}, không phải patch {patch.id}")
    tts = manifest.get("tts") if isinstance(manifest.get("tts"), dict) else {}
    model_id = str(tts.get("model_id") or "voxcpm2")
    voice_id = tts.get("voice_id") if isinstance(tts.get("voice_id"), str) else None
    options = tts.get("options") if isinstance(tts.get("options"), dict) else {}
    with_effects = bool(options.get("with_effects"))
    max_chars = int(manifest.get("max_chars") or 0)

    with db() as conn:
        book = repository.get_book(conn, patch.book_id)
        plan_inputs = repository.fetch_patch_chunk_inputs(conn, patch, max_chars=max_chars or None)
        audio_config = get_effective_audio_config(conn, book)
    plan = repository.build_chunk_plan_from_inputs(plan_inputs)
    if not plan:
        raise ChunkImportError("patch không có nội dung đọc được")
    # The chunks speak the text the patch had at export time. If it has been edited
    # since, chunk N no longer matches plan item N: refuse rather than install audio
    # that reads the wrong sentences under the new chapter timeline.
    exported = [item.get("text") if isinstance(item, dict) else None
                for item in manifest.get("chunk_metadata") or []]
    if exported != [item["text"] for item in plan]:
        raise ChunkImportError(
            "văn bản patch đã đổi kể từ lúc export (chunk không còn khớp) - export lại patch này")

    paths = [source.output_dir / f"chunk_{index:03d}.wav" for index in range(len(plan))]
    missing = [path.name for path in paths if not path.is_file()]
    if missing:
        raise ChunkImportError(f"thiếu {len(missing)}/{len(plan)} chunk (đầu tiên: {missing[0]})",
                               transient=True)

    patch_dir = repository.get_patch_chunk_dir(patch.book_id, patch.patch_index)
    targets = chunk_store.resolve_plan_chunks(patch.book_id, patch_dir, plan)
    chapters_by_index = {c.chapter_index: c for c in plan_inputs.get("chapters") or []}
    frame_counts: list[int] = []
    sample_rates: set[int] = set()
    for index, (item, path, (cache, local)) in enumerate(zip(plan, paths, targets)):
        try:
            audio, rate = sf.read(str(path), dtype="float32", always_2d=False)
        except (OSError, RuntimeError, sf.SoundFileError) as exc:
            raise ChunkImportError(f"{path.name} không đọc được: {exc}") from exc
        if np.asarray(audio).ndim > 1:
            audio = np.asarray(audio).mean(axis=1)
        # Same per-chunk leveling the local engines get before their chunk is stored.
        audio = level_chunk_loudness(audio)
        cache.dir.mkdir(parents=True, exist_ok=True)
        audio_merge.atomic_write_wav(
            str(cache.path(local)), lambda p, a=audio, r=rate: sf.write(p, a, r, format="WAV"))
        cache.record(local, key=chunk_store.chunk_key(f"remote:{model_id}", voice_id or "", item["text"]),
                     text=item["text"], frames=len(audio), sample_rate=rate,
                     engine=model_id, voice=voice_id, max_chars=plan_inputs["limit"])
        frame_counts.append(len(audio))
        sample_rates.add(int(rate))
        last_of_store = index + 1 == len(plan) or targets[index + 1][0] is not cache
        chapter = chapters_by_index.get(item.get("chapter_index"))
        if last_of_store and chapter is not None:
            cache.prune(local + 1)
            cache.mark_complete(repository.chapter_tts_source_key(
                chapter, plan_inputs["opts"], plan_inputs["rules"], plan_inputs["limit"]), local + 1)
    if len(sample_rates) != 1:
        raise ChunkImportError(f"chunk có sample rate lệch nhau {sorted(sample_rates)}")

    pauses = audio_merge.build_pause_plan(
        plan, audio_config["chunk_pause_ms"], audio_config["chapter_pause_ms"])
    audio_path = repository.get_patch_audio_path(patch.book_id, patch.patch_index)
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    chunk_paths = [str(cache.path(local)) for cache, local in targets]
    with db() if with_effects else nullcontext(None) as conn:
        merge_patch_audio(conn, plan, chunk_paths, frame_counts, sample_rates.pop(), pauses,
                          str(audio_path), with_effects=with_effects)
    return str(audio_path)
