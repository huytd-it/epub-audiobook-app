"""Audio concatenation: chunk wavs -> patch wav (in-memory), patch wavs -> final book wav (streamed)."""
from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

_BLOCK_FRAMES = 65536

# Pause inserted between two chunks of the same chapter, and between the last
# chunk of one chapter and the first of the next. The chapter pause is the
# audible "end of chapter" beat - and, with gap music on, the slot the
# background track is placed in (see app/music_bed.py).
DEFAULT_CHUNK_PAUSE_MS = 300
DEFAULT_CHAPTER_PAUSE_MS = 1500


def build_pause_plan(
    plan: list[dict], chunk_pause_ms: int = DEFAULT_CHUNK_PAUSE_MS,
    chapter_pause_ms: int = DEFAULT_CHAPTER_PAUSE_MS,
) -> list[int]:
    """Gap in ms to insert *before* each chunk; the first is always 0.

    A chunk that opens a chapter gets the longer chapter pause, every other
    chunk the normal one. Text Studio plans carry no chapter markers, so they
    come back uniform - exactly the old behaviour.
    """
    return [
        0 if index == 0 else (chapter_pause_ms if item.get("is_chapter_start") else chunk_pause_ms)
        for index, item in enumerate(plan)
    ]


def resolve_pauses(pause_ms, count: int) -> list[int]:
    """Normalize the ``pause_ms`` argument the concat helpers accept.

    An int means one uniform gap between every pair (the original API); a
    sequence is a per-chunk plan from :func:`build_pause_plan`, where element
    ``i`` is the gap *before* chunk ``i``. Element 0 is forced to 0 either way:
    nothing is ever prepended to the first chunk.
    """
    if isinstance(pause_ms, (int, float)):
        return [0] + [int(pause_ms)] * max(0, count - 1)
    pauses = [int(value) for value in pause_ms]
    if len(pauses) != count:
        raise ValueError("pause plan length must match the number of chunks")
    return [0] + pauses[1:]


def concat_chunks_to_wav(
    chunks: list[np.ndarray], sample_rate: int, out_path: str, pause_ms=0
) -> None:
    """Small scale (tens of chunks, seconds each) - safe to hold in memory.

    ``pause_ms`` is either one uniform gap or a per-chunk plan (see
    :func:`resolve_pauses`).
    """
    if chunks:
        channel_shape = None
        for chunk in chunks:
            if chunk.ndim not in (1, 2):
                raise ValueError("audio chunks must have one or two dimensions")
            shape = chunk.shape[1:]
            if channel_shape is None:
                channel_shape = shape
            elif shape != channel_shape:
                raise ValueError("audio chunks must have the same channel shape")
        pauses = resolve_pauses(pause_ms, len(chunks))
        parts = []
        for i, chunk in enumerate(chunks):
            frames = round(sample_rate * pauses[i] / 1000)
            if frames:
                parts.append(np.zeros((frames,) + chunks[0].shape[1:], dtype=chunks[0].dtype))
            parts.append(chunk)
        audio = np.concatenate(parts)
    else:
        audio = np.zeros(0, dtype=np.float32)
    sf.write(out_path, audio, sample_rate)


def concat_wavs(input_paths: list[str], out_path: str, pause_ms=0) -> None:
    if not input_paths:
        raise ValueError("no input paths to merge")
    headers = []
    for path in input_paths:
        with sf.SoundFile(path) as probe:
            headers.append((probe.samplerate, probe.channels))
    sample_rate, channels = headers[0]
    if any(header != (sample_rate, channels) for header in headers[1:]):
        raise ValueError("input samplerate/channels mismatch")
    pauses = resolve_pauses(pause_ms, len(input_paths))
    with sf.SoundFile(out_path, mode="w", samplerate=sample_rate, channels=channels, subtype="PCM_16") as out_f:
        for index, path in enumerate(input_paths):
            with sf.SoundFile(path, mode="r") as in_f:
                pause_frames = round(sample_rate * pauses[index] / 1000)
                if pause_frames:
                    out_f.write(np.zeros((pause_frames, channels), dtype=np.float32) if channels > 1 else np.zeros(pause_frames, dtype=np.float32))
                while True:
                    block = in_f.read(frames=_BLOCK_FRAMES, dtype="float32")
                    if block.size == 0:
                        break
                    out_f.write(block)


def build_chapter_marks(
    plan: list[dict], frame_counts: list[int], sample_rate: int, pause_ms
) -> tuple[list[dict], int]:
    """Map a chunk plan onto merged-audio frame offsets.

    Mirrors how ``concat_chunks_to_wav`` / ``concat_wavs`` lay chunks out: the gap
    from ``pause_ms`` before every chunk but the first, so a chapter marker lands
    *after* the chapter pause, on the first frame of speech. Returns the chapter
    markers plus the total frame count the merge will produce, so the timeline
    sidecar describes the exact file on disk. A plan with no chapter starts (a Text
    Studio edit, where the boundaries are gone) yields no markers."""
    pauses = resolve_pauses(pause_ms, len(plan))
    chapters: list[dict] = []
    frame = 0
    for index, (item, frames) in enumerate(zip(plan, frame_counts)):
        frame += round(sample_rate * pauses[index] / 1000)
        if item.get("is_chapter_start"):
            chapters.append({
                "chapter_index": item["chapter_index"],
                "title": item["chapter_title"],
                "start_frame": frame,
                "start_seconds": frame / sample_rate,
            })
        frame += frames
    return chapters, frame


def write_timeline(path, sample_rate: int, chapters: list[dict], total_frames: int) -> None:
    """Write the chapter timeline sidecar atomically — a half-written sidecar would
    silently mis-describe the audio, and readers validate it against the WAV."""
    path = Path(path)
    payload = {"version": 1, "sample_rate": sample_rate, "total_frames": total_frames, "chapters": chapters}
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp:
            json.dump(payload, temp, ensure_ascii=False, indent=2)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            Path(temp_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def try_write_timeline(path, sample_rate: int, chapters: list[dict], total_frames: int) -> None:
    """Best-effort ``write_timeline`` — a missing sidecar only costs YouTube chapter
    markers, which must never fail an otherwise-good patch."""
    try:
        write_timeline(path, sample_rate, chapters, total_frames)
    except Exception:
        logger.warning("failed to write timeline sidecar %s", path, exc_info=True)


def cleanup_chunk_dir(chunk_dir: str) -> None:
    """Delete a chunk working directory and all contents.  Best-effort — logs a warning
    if removal fails, but does not raise (the patch may already be complete)."""
    try:
        shutil.rmtree(chunk_dir, ignore_errors=True)
    except Exception:
        logger.warning("failed to clean up chunk directory %s", chunk_dir, exc_info=True)



