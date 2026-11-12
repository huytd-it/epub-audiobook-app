"""Nạp chunk WAV làm sẵn bên ngoài vào kho chunk theo chương, rồi ghép patch từ kho.

Định dạng thư mục người dùng chọn (trình duyệt gửi kèm đường dẫn tương đối)::

    <thư mục bất kỳ>/
        0001/                  số chương, đánh số từ 1 như cột "Ch." trên Mục lục
            chunk_000.wav      số thứ tự chunk trong chương, đánh số từ 0
            chunk_001.wav
        0002/ ...

Tên thư mục chương chỉ cần *bắt đầu* bằng số chương (``0001``, ``12``,
``0012 - Chương 12`` đều được); tên file nhận ``chunk_NNN.wav`` hoặc ``NNN.wav``. Chunk
thứ N phải đọc đúng đoạn văn N của cách tách hiện tại (chuẩn hoá + max_chars của sách),
tức danh sách chunk ở tab Audio của chương.

Chunk được cài như chunk tổng hợp tại chỗ (chuẩn âm lượng, manifest, mark_complete khi
đủ) nhưng mang key ``upload:``: lần TTS local sau không coi chúng là kết quả của
engine thật, nên patch phủ chương được ghép trực tiếp ở đây (assemble_patch_from_stores)
thay vì qua hàng đợi TTS.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from typing import BinaryIO

import numpy as np
import soundfile as sf

from app import audio_merge, chunk_store, repository
from app.audio_mastering import level_chunk_loudness

UPLOAD_ENGINE = "upload"
_CHAPTER_DIR = re.compile(r"^(\d+)")
_CHUNK_FILE = re.compile(r"^(?:chunk_)?(\d+)\.wav$", re.IGNORECASE)


class ChapterChunkUploadError(ValueError):
    """User-facing reason a patch cannot be merged from the chapter stores."""


def parse_upload_path(name: str) -> tuple[int, int] | str:
    """``…/0001/chunk_000.wav`` -> (chapter_index 0, chunk index 0), or the reason the
    path does not follow the format."""
    parts = [part for part in (name or "").replace("\\", "/").split("/") if part]
    if len(parts) < 2:
        return "file phải nằm trong thư mục chương (vd. 0001/chunk_000.wav)"
    file_match = _CHUNK_FILE.match(parts[-1])
    if not file_match:
        return "tên file phải là chunk_NNN.wav"
    dir_match = _CHAPTER_DIR.match(parts[-2])
    if not dir_match:
        return f"thư mục '{parts[-2]}' không bắt đầu bằng số chương"
    chapter_no = int(dir_match.group(1))
    if chapter_no < 1:
        return "số chương đánh từ 1"
    return chapter_no - 1, int(file_match.group(1))


def chapter_plan(chapter, opts, rules, limit: int) -> list[dict]:
    """Chapter split exactly as a patch plan splits it (same inputs as the Audio tab)."""
    return repository.build_chunk_plan_from_inputs(
        {"limit": limit, "clean_text": None, "chapters": [chapter], "rules": rules, "opts": opts})


def _read_wav(handle: BinaryIO) -> tuple[np.ndarray, int]:
    audio, rate = sf.read(handle, dtype="float32", always_2d=False)
    audio = np.asarray(audio)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if not len(audio):
        raise ValueError("file rỗng")
    return audio.astype(np.float32), int(rate)


def install_chapter_chunks(
    book_id: int, chapter, opts, rules, limit: int,
    files: dict[int, tuple[str, BinaryIO]], *, replace: bool,
) -> dict:
    """Install one chapter's uploaded chunks (``files``: chunk index -> (label, stream)).

    Every file is decoded and checked before anything is written, so a bad file or a
    sample-rate mismatch leaves the chapter store untouched. ``replace`` clears the
    store first; otherwise chunks not in the upload are kept."""
    plan = chapter_plan(chapter, opts, rules, limit)
    report = {
        "chapter_index": chapter.chapter_index,
        "chapter_no": chapter.chapter_index + 1,
        "title": chapter.title,
        "expected": len(plan),
        "received": len(files),
        "installed": 0,
        "rejected": [],
        "missing": [],
        "complete": False,
    }
    if not plan:
        report.update(status="error", detail="chương không có nội dung đọc được")
        return report

    decoded: dict[int, tuple[np.ndarray, int]] = {}
    for index in sorted(files):
        label, handle = files[index]
        if index >= len(plan):
            report["rejected"].append({"filename": label, "reason": f"chương chỉ có {len(plan)} chunk (0–{len(plan) - 1})"})
            continue
        try:
            decoded[index] = _read_wav(handle)
        except (OSError, RuntimeError, ValueError, sf.SoundFileError) as exc:
            report["rejected"].append({"filename": label, "reason": f"không đọc được WAV: {exc}"})

    store = chunk_store.ChunkStore(chunk_store.get_chapter_chunk_dir(book_id, chapter.chapter_index))
    rates = {rate for _, rate in decoded.values()}
    if not replace:
        manifest = store.manifest()
        rates |= {
            int(entry.get("sample_rate") or 0)
            for key, entry in manifest["chunks"].items()
            if isinstance(entry, dict) and key.isdigit() and int(key) not in decoded
            and int(key) < len(plan) and store.path(int(key)).is_file() and entry.get("sample_rate")
        }
    if len(rates) > 1:
        report.update(status="error", detail=(
            f"sample rate lệch nhau {sorted(rates)} — mọi chunk của chương phải cùng sample rate"
            + ("" if replace else " (kể cả chunk cũ đang giữ; bật 'Thay toàn bộ' để bỏ chunk cũ)")))
        return report
    if not decoded:
        report.update(status="error", detail="không có chunk hợp lệ")
        return report

    if replace:
        store.clear()
    store.dir.mkdir(parents=True, exist_ok=True)
    for index, (audio, rate) in decoded.items():
        text = plan[index]["text"]
        audio = level_chunk_loudness(audio)
        audio_merge.atomic_write_wav(
            str(store.path(index)), lambda p, a=audio, r=rate: sf.write(p, a, r, format="WAV"))
        store.record(index, key=chunk_store.chunk_key(UPLOAD_ENGINE, "", text), text=text,
                     frames=len(audio), sample_rate=rate, engine=UPLOAD_ENGINE, max_chars=limit)
    store.prune(len(plan))

    manifest = store.manifest()
    missing = [
        index for index, item in enumerate(plan)
        if not store.path(index).is_file()
        or (manifest["chunks"].get(str(index)) or {}).get("text") != item["text"]
    ]
    if not missing:
        store.mark_complete(repository.chapter_tts_source_key(chapter, opts, rules, limit), len(plan))
    report.update(
        installed=len(decoded),
        missing=missing,
        complete=not missing,
        status="ok" if not missing and not report["rejected"] else "partial",
        detail=None if not missing else f"còn thiếu {len(missing)}/{len(plan)} chunk",
    )
    return report


def assemble_patch_from_stores(
    db: Callable[[], AbstractContextManager], patch, limit: int,
) -> tuple[str, int]:
    """Merge a patch's audio from its chapters' chunk stores, whatever produced the
    chunks. Refuses unless every chunk is on disk and speaks the current text.

    Returns (audio path, chunk count); the caller marks the patch done."""
    from app.jobqueue.handlers.audiobook_tts import merge_patch_audio
    from app.production_defaults import get_effective_audio_config

    if patch.clean_text:
        raise ChapterChunkUploadError("patch dùng văn bản Text Studio, không ghép từ chunk theo chương được")
    with db() as conn:
        book = repository.get_book(conn, patch.book_id)
        plan_inputs = repository.fetch_patch_chunk_inputs(conn, patch, max_chars=limit)
        audio_config = get_effective_audio_config(conn, book)
    plan = repository.build_chunk_plan_from_inputs(plan_inputs)
    if not plan:
        raise ChapterChunkUploadError("patch không có nội dung đọc được")
    patch_dir = repository.get_patch_chunk_dir(patch.book_id, patch.patch_index)
    targets = chunk_store.resolve_plan_chunks(patch.book_id, patch_dir, plan)

    frame_counts: list[int] = []
    sample_rates: set[int] = set()
    missing = 0
    for item, (cache, local) in zip(plan, targets):
        entry = cache.entry(local) or {}
        if not cache.path(local).is_file() or entry.get("text") != item["text"]:
            missing += 1
            continue
        frames, rate = int(entry.get("frames") or 0), int(entry.get("sample_rate") or 0)
        if not frames or not rate:
            info = sf.info(str(cache.path(local)))
            frames, rate = info.frames, info.samplerate
        frame_counts.append(frames)
        sample_rates.add(rate)
    if missing:
        raise ChapterChunkUploadError(f"thiếu {missing}/{len(plan)} chunk")
    if len(sample_rates) != 1:
        raise ChapterChunkUploadError(f"chunk các chương có sample rate lệch nhau {sorted(sample_rates)}")

    pauses = audio_merge.build_pause_plan(plan, audio_config["chunk_pause_ms"], audio_config["chapter_pause_ms"])
    audio_path = repository.get_patch_audio_path(patch.book_id, patch.patch_index)
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    with_effects = bool(audio_config.get("with_effects"))
    with db() if with_effects else nullcontext(None) as conn:
        merge_patch_audio(conn, plan, [str(cache.path(local)) for cache, local in targets],
                          frame_counts, sample_rates.pop(), pauses, str(audio_path), with_effects=with_effects)
    return str(audio_path), len(plan)
