"""Sinh audio cho một patch bằng engine TTS đã chọn (mọi model trong catalog).

Handler dùng chung cho toàn bộ pipeline audiobook: voxcpm2 / omnivoice /
vieneu-fast / edge-tts / gtts đều chạy cùng mã này — viết chunk WAV, resume theo
chunk, gộp patch, gộp sách và xếp hàng video khi xong.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import soundfile as sf

from app import audio_merge, chunk_store, repository, subtitle_gen
from app.audio_mastering import level_chunk_loudness, normalize_wav_in_place
from app.config import settings
from app.jobqueue import store
from app.jobqueue.models import JobFatalError

_engines = {}


def get_engine(engine_id: str | None = None, voice: str | None = None, tts_options: dict | None = None):
    from app.tts_engine import create_tts_engine

    engine_id = engine_id or settings.tts_engine
    tts_options = tts_options or {}
    key = (engine_id, voice, json.dumps(tts_options, sort_keys=True))
    if key not in _engines:
        _engines[key] = create_tts_engine(engine_id, voice=voice, **tts_options)
    return _engines[key]


def handle(ctx) -> dict:
    from app.tts_engine import normalize_tt_payload

    payload = normalize_tt_payload(ctx.job.payload, default_engine=settings.tts_engine)
    patch_id = payload.get("patch_id")
    if patch_id is None:
        raise JobFatalError("payload thiếu patch_id")
    patch = repository.get_patch(ctx.conn, patch_id)
    if patch is None:
        raise JobFatalError(f"patch {patch_id} không tồn tại")

    engine_id = payload["tts_engine"]
    voice = payload.get("voice") or None
    max_chars = int(payload.get("max_chars") or 0)
    with_effects = bool(payload.get("with_effects"))
    tts_options = payload.get("tts_options") or {}
    pause_config = (payload["chunk_pause_ms"], payload["chapter_pause_ms"])

    # Lưu giọng đọc theo patch khi sử dụng: patch "nhớ" đúng combo đã tổng hợp,
    # lần chạy sau giọng riêng này thắng cấu hình chung của sách.
    repository.set_patch_audio_settings(ctx.conn, patch_id, engine_id, voice)

    # A retry can be claimed after synthesis succeeded but before the queue row was
    # committed as done (process crash, DB lock, shutdown). The patch audio is the
    # durable result, so never synthesize it a second time. Continue the downstream
    # automation instead, which is idempotent through queue dedupe keys.
    if patch.status in {"failed", "done"} and patch.audio_path and Path(patch.audio_path).is_file():
        try:
            info = sf.info(patch.audio_path)
            if info.frames > 0 and info.samplerate > 0:
                ctx.log(f"audio đã tồn tại, bỏ qua TTS -> {patch.audio_path}")
                return _finish_audio_result(ctx, patch, patch.audio_path, payload, skipped=True)
        except (OSError, RuntimeError):
            ctx.log(f"audio result không đọc được, tạo lại -> {patch.audio_path}")

    if settings.tts_write_chunk_files:
        snapshot_dir = repository.get_patch_chunk_dir(patch.book_id, patch.patch_index)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / ".tts_request.json").write_text(json.dumps({
            "tts_engine": engine_id, "voice": voice, "max_chars": max_chars,
            "with_effects": with_effects, "tts_options": tts_options,
            "chunk_pause_ms": pause_config[0], "chapter_pause_ms": pause_config[1],
        }), encoding="utf-8")

    ctx.log(f"synthesize patch {patch_id} (book {patch.book_id}) engine={engine_id}")
    try:
        engine = get_engine(engine_id, voice, tts_options)
        audio_path, chunk_count = synthesize_patch(
            ctx, patch, engine, Path(settings.data_root),
            engine_id=engine_id,
            effective_max_chars=max_chars if max_chars > 0 else None,
            with_effects=with_effects, pause_config=pause_config,
        )
    except asyncio.CancelledError:
        raise
    except JobFatalError:
        repository.mark_patch_failed(ctx.conn, patch_id, "không có nội dung đọc được")
        raise
    except Exception as exc:
        repository.mark_patch_failed(ctx.conn, patch_id, str(exc))
        raise

    return _finish_audio_result(ctx, patch, audio_path, payload, chunk_count=chunk_count)


def _finish_audio_result(ctx, patch, audio_path: str, payload: dict, *,
                         chunk_count: int = 0, skipped: bool = False) -> dict:
    """Persist a usable audio result and idempotently continue the media pipeline."""
    from app.patch_publishing import enqueue_patch_video, fetch_thumbnail_inputs, warm_patch_thumbnail

    thumbnail_inputs = fetch_thumbnail_inputs(ctx.conn, patch.id)
    warm_patch_thumbnail(thumbnail_inputs)
    repository.mark_patch_done(ctx.conn, patch.id, audio_path)

    # Cờ auto_create_video/auto_upload_youtube trong request TTS ưu tiên hơn cờ đã lưu
    # của sách; payload không có cờ (None) => dùng cờ persisted. enqueue_patch_video
    # chạy preflight (waiting_config/waiting_timeline/awaiting_republish...) và chỉ
    # enqueue job patch_video khi thật sự sẵn sàng — snapshot render được đóng băng lúc đó.
    request_policy = {}
    if payload.get("auto_create_video") is not None:
        request_policy["auto_create_video"] = payload["auto_create_video"]
    if payload.get("auto_upload_youtube") is not None:
        request_policy["auto_upload_youtube"] = payload["auto_upload_youtube"]
    outcome = enqueue_patch_video(ctx.conn, patch.id, request_policy=request_policy)

    ctx.log(f"patch {patch.id} xong -> {audio_path}; automation={outcome['state']}")
    final_path = finalize_book_if_ready(ctx, patch.book_id)
    if chunk_count:
        ctx.progress(chunk_count, chunk_count, phase="synthesizing")
    return {"audio_path": audio_path, "chunks": chunk_count,
            "final_audio_path": final_path, "skipped": skipped,
            "automation": outcome["state"]}


def synthesize_patch(
    ctx, patch, engine, data_root: Path, *,
    engine_id: str, effective_max_chars: int | None, with_effects: bool,
    pause_config: tuple[int, int] | None = None,
) -> tuple[str, int]:
    plan_inputs = repository.fetch_patch_chunk_inputs(ctx.conn, patch, max_chars=effective_max_chars)
    book = repository.get_book(ctx.conn, patch.book_id)
    plan = repository.build_chunk_plan_from_inputs(plan_inputs)
    if not plan:
        raise JobFatalError("patch không có nội dung đọc được")
    chunk_pause_ms, chapter_pause_ms = pause_config or (
        audio_merge.DEFAULT_CHUNK_PAUSE_MS, audio_merge.DEFAULT_CHAPTER_PAUSE_MS)
    # One gap per chunk instead of one for the whole patch: a chunk that opens a
    # chapter gets the longer chapter pause. Timeline, audio and captions all
    # take the same plan, so they cannot drift apart.
    pauses = audio_merge.build_pause_plan(plan, chunk_pause_ms, chapter_pause_ms)

    ref_wav = book.voice_clip_path if book else None
    ref_text = book.voice_transcript if book else None
    book_dir = data_root / "books" / str(patch.book_id)
    audio_path = str(repository.get_patch_audio_path(patch.book_id, patch.patch_index))
    Path(audio_path).parent.mkdir(parents=True, exist_ok=True)
    timeline_path = Path(audio_path).with_suffix(".timeline.json")
    subtitle_path = Path(audio_path).with_suffix(".ass")
    total = len(plan)
    ctx.progress(patch.next_chunk_index, total, phase="synthesizing")

    if not settings.tts_write_chunk_files:
        wavs = []
        for index, item in enumerate(plan):
            if ctx.should_cancel():
                raise asyncio.CancelledError()
            ctx.log(f"chunk {index + 1}/{total} văn bản: {item['text']}")
            arr = engine.synthesize_chunk(item["text"], reference_wav_path=ref_wav, prompt_text=ref_text)
            arr = level_chunk_loudness(arr)
            ctx.log(f"chunk {index + 1}/{total} xong: {len(arr) / engine.sample_rate:.1f}s audio")
            wavs.append(arr)
            ctx.progress(index + 1, total)
        frame_counts = [len(a) for a in wavs]
        chapters, _ = audio_merge.build_chapter_marks(plan, frame_counts, engine.sample_rate, pauses)
        audio_merge.atomic_write_wav(audio_path, lambda p: audio_merge.concat_chunks_to_wav(wavs, engine.sample_rate, p, pause_ms=pauses))
        _apply_effects(ctx, with_effects, audio_path, plan)
        normalize_wav_in_place(audio_path)
        audio_merge.try_write_timeline(timeline_path, engine.sample_rate, chapters, sf.info(audio_path).frames)
        subtitle_gen.try_generate(subtitle_path, plan, frame_counts, engine.sample_rate, pauses)
        ctx.progress(total, total, phase="synthesizing")
        return audio_path, total

    repository.update_patch_chunk_count(ctx.conn, patch.id, total)

    # Chunk sống theo chương (books/{id}/chapters/NNNN, xem app/chunk_store.py), không
    # theo patch: build lại patch, sửa một chương hay một chương lỗi giữa chừng đều chỉ
    # phải tổng hợp những chunk thiếu/lệch key — phần còn lại được ghép lại nguyên trạng.
    patch_dir = repository.get_patch_chunk_dir(patch.book_id, patch.patch_index)
    targets = chunk_store.resolve_plan_chunks(patch.book_id, patch_dir, plan)
    model_cfg = getattr(engine, "config_fingerprint", lambda: engine_id)()
    voice = getattr(engine, "voice", None)
    voice = voice if isinstance(voice, str) else None
    frame_counts: list[int] = []
    sample_rates: set[int] = set()
    reused = 0
    chapters_by_index = {c.chapter_index: c for c in plan_inputs.get("chapters") or []}
    for index, (item, (cache, local)) in enumerate(zip(plan, targets)):
        key = chunk_store.chunk_key(engine_id, model_cfg, item["text"])
        if cache.is_valid(local, key):
            entry = cache.entry(local) or {}
            frames = int(entry.get("frames") or 0)
            rate = int(entry.get("sample_rate") or 0)
            if not frames or not rate:
                info = sf.info(str(cache.path(local)))
                frames, rate = info.frames, info.samplerate
            reused += 1
        else:
            if ctx.should_cancel():
                raise asyncio.CancelledError()
            ctx.log(f"chunk {index + 1}/{total} văn bản: {item['text']}")
            arr = engine.synthesize_chunk(item["text"], reference_wav_path=ref_wav, prompt_text=ref_text)
            arr = level_chunk_loudness(arr)
            rate = engine.sample_rate
            ctx.log(f"chunk {index + 1}/{total} xong: {len(arr) / rate:.1f}s audio")
            cache.dir.mkdir(parents=True, exist_ok=True)
            # Ghi qua file tạm: chunk ghi dở do crash không bao giờ mang key hợp lệ.
            audio_merge.atomic_write_wav(str(cache.path(local)), lambda p, a=arr, r=rate: sf.write(p, a, r, format="WAV"))
            frames = len(arr)
            cache.record(local, key=key, text=item["text"], frames=frames, sample_rate=rate,
                         engine=engine_id, voice=voice, max_chars=plan_inputs["limit"])
        frame_counts.append(frames)
        sample_rates.add(rate)
        repository.update_patch_chunk_progress(ctx.conn, patch.id, index + 1)
        ctx.progress(index + 1, total)
        last_of_store = index + 1 == total or targets[index + 1][0] is not cache
        chapter = chapters_by_index.get(item.get("chapter_index"))
        if last_of_store and chapter is not None:
            cache.prune(local + 1)
            cache.mark_complete(repository.chapter_tts_source_key(
                chapter, plan_inputs["opts"], plan_inputs["rules"], plan_inputs["limit"]), local + 1)
    if reused:
        ctx.log(f"dùng lại {reused}/{total} chunk đã có")
    if len(sample_rates) != 1:
        raise RuntimeError(
            f"chunk có sample rate lệch nhau {sorted(sample_rates)}; xoá audio các chương liên quan rồi tạo lại")
    sample_rate = sample_rates.pop()

    chapters, _ = audio_merge.build_chapter_marks(plan, frame_counts, sample_rate, pauses)
    ctx.progress(total, total, phase="merging")
    chunk_paths = [str(cache.path(local)) for cache, local in targets]
    audio_merge.atomic_write_wav(audio_path, lambda p: audio_merge.concat_wavs(chunk_paths, p, pause_ms=pauses))
    _apply_effects(ctx, with_effects, audio_path, plan)
    normalize_wav_in_place(audio_path)
    audio_merge.try_write_timeline(timeline_path, sample_rate, chapters, sf.info(audio_path).frames)
    subtitle_gen.try_generate(subtitle_path, plan, frame_counts, sample_rate, pauses)
    ctx.progress(total, total, phase="synthesizing")
    return audio_path, total


def _apply_effects(ctx, with_effects: bool, audio_path: str, plan: list[dict]) -> None:
    """Overlay sound-effect clips onto the merged patch audio. Shared with the
    LightTTS preview path so effects behave identically for every engine."""
    if not with_effects:
        return
    from app.routes.text_studio import _mix_effects

    mixed = _mix_effects(Path(audio_path).read_bytes(), "\n\n".join(i["text"] for i in plan), ctx.conn)
    Path(audio_path).write_bytes(mixed)


def finalize_book_if_ready(ctx, book_id: int) -> str | None:
    if not repository.all_patches_done(ctx.conn, book_id):
        return None
    patches = repository.list_patches(ctx.conn, book_id)
    book = repository.get_book(ctx.conn, book_id)
    paths = [p.audio_path for p in patches if p.audio_path]
    if len(paths) != len(patches):
        return None

    book_dir = Path(settings.data_root) / "books" / str(book_id)
    book_dir.mkdir(parents=True, exist_ok=True)
    final_path = str(book_dir / "final.wav")
    ctx.progress(ctx.job.progress_current, ctx.job.progress_total, phase="merging_book")
    audio_merge.concat_wavs(paths, final_path)
    normalize_wav_in_place(final_path)
    repository.set_book_final_audio(ctx.conn, book_id, final_path)
    ctx.log(f"gộp xong final.wav cho sách {book_id}")
    if book is None:
        return final_path
    has_image = bool(book.background_image_path) or any(p.image_path for p in patches)
    if not has_image:
        ctx.log("sách không có ảnh nào dùng được — bỏ qua bước tạo video")
        return final_path
    if book.auto_create_video:
        # Automation per-patch (enqueue_patch_video) đã render + upload từng patch rồi;
        # tự enqueue thêm video toàn sách ở đây sẽ tạo video/upload trùng lặp (video
        # toàn sách + upload YouTube thứ 2 cho cùng nội dung). Bật auto_create_video
        # nghĩa là pipeline patch SỞ HỮU việc render/upload cho sách này — theo đúng
        # thiết kế ở docs/superpowers/specs/2026-07-26-automated-patch-video-youtube-pipeline-design.md.
        # Video toàn sách vẫn tạo được thủ công qua nút "Tạo video" (routes/books.py, routes/queue.py).
        ctx.log("auto_create_video đang bật — pipeline patch đã tự render/upload, bỏ qua video toàn sách tự động")
        return final_path
    book_job = repository.enqueue_book_job(ctx.conn, book_id, "video")
    job_id = store.enqueue(ctx.conn, "video", payload={"book_job_id": book_job.id}, book_id=book_id, dedupe_key=f"video:book_job={book_job.id}")
    ctx.log(f"đã xếp hàng job video (book_job={book_job.id}, job={job_id})")
    return final_path
