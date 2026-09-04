"""Sinh audio cho một patch bằng LightTTS."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from pathlib import Path

import soundfile as sf

from app import audio_merge, repository
from app.config import settings
from app.jobqueue.models import JobFatalError

logger = logging.getLogger(__name__)


def dedupe_key(patch_id: int) -> str:
    return f"light_tts:patch={patch_id}"


def _build_engine(backend: str, voice: str):
    from app.light_tts import LightTTSEngine
    return LightTTSEngine(backend=backend, voice=voice or None)


def _synth_with_retries(engine, text: str, voice: str | None) -> bytes:
    attempts = max(1, settings.light_tts_chunk_retries)
    last = None
    for _ in range(attempts):
        try:
            wav_bytes, _sr = engine.synthesize_to_wav_bytes(text, voice)
            return wav_bytes
        except Exception as exc:  # noqa: BLE001
            last = exc
    raise last if last else RuntimeError("synthesize thất bại")


def handle(ctx) -> dict:
    from app.tts_engine import normalize_tt_payload

    payload = normalize_tt_payload(ctx.job.payload, default_engine=settings.light_tts_backend)
    patch_id = payload.get("patch_id")
    if patch_id is None:
        raise JobFatalError("payload thiếu patch_id")
    patch = repository.get_patch(ctx.conn, patch_id)
    if patch is None:
        raise JobFatalError(f"patch {patch_id} không tồn tại")
    book_id = patch.book_id
    backend = payload["tts_engine"]
    voice = payload.get("voice") or settings.light_tts_voice
    max_chars = payload["max_chars"]
    with_effects = payload["with_effects"]
    effective_max_chars = max_chars if max_chars > 0 else (patch.max_chars or settings.tts_max_chars)
    plan_inputs = repository.fetch_patch_chunk_inputs(ctx.conn, patch, max_chars=effective_max_chars)
    plan = repository.build_chunk_plan_from_inputs(plan_inputs)
    total = len(plan)
    if total == 0:
        ctx.emit({"type": "error", "message": "Patch này không có chunk nào"})
        raise JobFatalError("patch không có chunk nào")

    # Cùng thư mục chunk với pipeline TTS chính (audio/{book}_{episode}_chunks) để
    # endpoint phát thử chunk chỉ phải đọc một chỗ; hai bên phân biệt chunk của nhau
    # bằng marker riêng (.light_tts_meta ở đây, fingerprint ở audiobook_tts).
    chunk_dir = repository.get_patch_chunk_dir(book_id, patch.patch_index)
    chunk_dir.mkdir(parents=True, exist_ok=True)
    joined = "\n\n".join(item["text"] for item in plan)
    meta_key = hashlib.md5(f"{backend}|{voice}|{effective_max_chars}|{joined}".encode("utf-8")).hexdigest()
    meta_path = chunk_dir / ".light_tts_meta"
    try:
        reusable = meta_path.read_text(encoding="utf-8").strip() == meta_key
    except OSError:
        reusable = False
    if not reusable:
        for stale in chunk_dir.glob("chunk_*.wav"):
            stale.unlink(missing_ok=True)
        meta_path.write_text(meta_key, encoding="utf-8")
    if max_chars == 0 and patch.chunk_count != total:
        repository.update_patch_chunk_count(ctx.conn, patch_id, total)
    try:
        engine = _build_engine(backend, voice)
    except RuntimeError as exc:
        ctx.emit({"type": "error", "message": str(exc)})
        raise JobFatalError(str(exc))

    cache_bust = uuid.uuid4().hex[:8]
    ctx.progress(0, total, phase="synthesizing")
    ok_count = fail_count = contiguous = 0
    prefix_open = True
    for index, item in enumerate(plan):
        if ctx.should_cancel():
            raise asyncio.CancelledError()
        chunk_path = chunk_dir / f"chunk_{index:03d}.wav"
        chunk_url = f"/books/{book_id}/patches/{patch_id}/chunk-audio/{index}?v={cache_bust}"
        present = False
        if chunk_path.is_file():
            ok_count += 1; present = True
            ctx.emit({"type": "chunk", "index": index, "total": total, "url": chunk_url, "reused": True})
        else:
            try:
                wav_bytes = _synth_with_retries(engine, item["text"], voice or None)
            except Exception as exc:  # noqa: BLE001
                fail_count += 1
                ctx.log(f"chunk {index} lỗi: {exc}", level=logging.WARNING)
                ctx.emit({"type": "chunk_error", "index": index, "total": total, "message": str(exc)})
            else:
                chunk_path.write_bytes(wav_bytes); ok_count += 1; present = True
                ctx.emit({"type": "chunk", "index": index, "total": total, "url": chunk_url})
        if prefix_open:
            if present: contiguous = index + 1
            else: prefix_open = False
            repository.update_patch_chunk_progress(ctx.conn, patch_id, contiguous)
        ctx.progress(index + 1, total)
    if ok_count == 0:
        ctx.emit({"type": "error", "message": "Tất cả chunk đều lỗi, không có audio để lưu"})
        return {"audio_path": None, "ok": 0, "failed": fail_count}
    if fail_count:
        ctx.emit({"type": "done", "saved": False, "complete": False, "ok": ok_count, "failed": fail_count})
        return {"audio_path": None, "ok": ok_count, "failed": fail_count}
    ctx.progress(total, total, phase="merging")
    audio_path = repository.get_patch_audio_path(book_id, patch.patch_index)
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path = str(audio_path)
    chunk_paths = [str(chunk_dir / f"chunk_{i:03d}.wav") for i in range(total)]
    pauses = audio_merge.build_pause_plan(
        plan, payload["chunk_pause_ms"], payload["chapter_pause_ms"])
    audio_merge.concat_wavs(chunk_paths, audio_path, pause_ms=pauses)
    _finish_patch_audio(ctx, plan, chunk_paths, audio_path, patch_id, with_effects, pauses)
    from app.jobqueue.handlers.audiobook_tts import finalize_book_if_ready
    final_path = finalize_book_if_ready(ctx, book_id)
    ctx.emit({"type": "done", "saved": True, "complete": True, "ok": ok_count, "failed": 0})
    ctx.progress(total, total, phase="done")
    return {"audio_path": audio_path, "ok": ok_count, "failed": 0, "final_audio_path": final_path}


def _finish_patch_audio(ctx, plan, chunk_paths, audio_path, patch_id, with_effects, pauses) -> None:
    info = sf.info(audio_path)
    chapters, _ = audio_merge.build_chapter_marks(plan, [sf.info(p).frames for p in chunk_paths], info.samplerate, pauses)
    audio_merge.try_write_timeline(Path(audio_path).with_suffix(".timeline.json"), info.samplerate, chapters, info.frames)
    if with_effects:
        from app.routes.text_studio import _mix_effects
        mixed = _mix_effects(Path(audio_path).read_bytes(), "\n\n".join(i["text"] for i in plan), ctx.conn)
        Path(audio_path).write_bytes(mixed)
    repository.mark_patch_done(ctx.conn, patch_id, audio_path)
