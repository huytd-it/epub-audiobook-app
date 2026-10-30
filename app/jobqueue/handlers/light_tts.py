"""Sinh audio cho một patch bằng LightTTS."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path

import soundfile as sf

from app import audio_merge, chunk_store, repository
from app.audio_mastering import normalize_wav_in_place
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

    # Cùng kho chunk theo chương với pipeline TTS chính (app/chunk_store.py) để endpoint
    # phát thử chunk chỉ phải đọc một chỗ; key từng chunk (backend|voice|text) phân biệt
    # chunk của hai bên, nên chunk lệch key được tổng hợp lại thay vì dùng nhầm.
    patch_dir = repository.get_patch_chunk_dir(book_id, patch.patch_index)
    targets = chunk_store.resolve_plan_chunks(book_id, patch_dir, plan)
    model_cfg = f"light:{backend}:{voice or ''}"
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
    chapter_ok = True
    chapters_by_index = {c.chapter_index: c for c in plan_inputs.get("chapters") or []}
    for index, item in enumerate(plan):
        if ctx.should_cancel():
            raise asyncio.CancelledError()
        cache, local = targets[index]
        key = chunk_store.chunk_key(backend, model_cfg, item["text"])
        chunk_url = f"{chunk_store.chunk_audio_url(book_id, patch_id, item.get('chapter_index'), local)}?v={cache_bust}"
        present = False
        if cache.is_valid(local, key):
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
                cache.dir.mkdir(parents=True, exist_ok=True)
                cache.path(local).write_bytes(wav_bytes)
                info = sf.info(str(cache.path(local)))
                cache.record(local, key=key, text=item["text"], frames=info.frames, sample_rate=info.samplerate,
                             engine=backend, voice=voice or None, max_chars=effective_max_chars)
                ok_count += 1; present = True
                ctx.emit({"type": "chunk", "index": index, "total": total, "url": chunk_url})
        # Đánh dấu chương đủ chunk (cho bảng patch đếm chương sẵn sàng) chỉ khi mọi
        # chunk của chương trong lượt này đều có mặt.
        chapter_ok = present and (local == 0 or chapter_ok)
        chapter = chapters_by_index.get(item.get("chapter_index"))
        last_of_store = index + 1 == total or targets[index + 1][0] is not cache
        if last_of_store and chapter is not None and chapter_ok:
            cache.mark_complete(repository.chapter_tts_source_key(
                chapter, plan_inputs["opts"], plan_inputs["rules"], plan_inputs["limit"]), local + 1)
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
    chunk_paths = [str(cache.path(local)) for cache, local in targets]
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
    if with_effects:
        from app.routes.text_studio import _mix_effects
        mixed = _mix_effects(Path(audio_path).read_bytes(), "\n\n".join(i["text"] for i in plan), ctx.conn)
        Path(audio_path).write_bytes(mixed)
    normalize_wav_in_place(audio_path)
    info = sf.info(audio_path)
    chapters, _ = audio_merge.build_chapter_marks(plan, [sf.info(p).frames for p in chunk_paths], info.samplerate, pauses)
    audio_merge.try_write_timeline(Path(audio_path).with_suffix(".timeline.json"), info.samplerate, chapters, info.frames)
    repository.mark_patch_done(ctx.conn, patch_id, audio_path)
