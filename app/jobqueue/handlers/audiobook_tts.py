"""Sinh audio cho một patch bằng engine TTS đã chọn (mọi model trong catalog).

Handler dùng chung cho toàn bộ pipeline audiobook: voxcpm2 / omnivoice /
vieneu-fast / edge-tts / gtts đều chạy cùng mã này — viết chunk WAV, resume theo
chunk, gộp patch, gộp sách và xếp hàng video khi xong.
"""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import soundfile as sf

from app import audio_merge, chunk_store, repository, subtitle_gen
from app.audio_mastering import level_chunk_loudness, normalize_wav_in_place
from app.config import settings
from app.jobqueue import store
from app.jobqueue.models import JobFatalError

_engines = {}
_thread_local = threading.local()


def get_engine(engine_id: str | None = None, voice: str | None = None, tts_options: dict | None = None):
    from app.tts_engine import create_tts_engine

    engine_id = engine_id or settings.tts_engine
    tts_options = tts_options or {}
    key = (engine_id, voice, json.dumps(tts_options, sort_keys=True))
    if key not in _engines:
        _engines[key] = create_tts_engine(engine_id, voice=voice, **tts_options)
    return _engines[key]


_ORIGINAL_GET_ENGINE = get_engine


def get_worker_engine(engine_id: str | None = None, voice: str | None = None, tts_options: dict | None = None):
    """Mỗi worker thread sở hữu một instance engine riêng để tránh race condition trên runtime."""
    if get_engine is not _ORIGINAL_GET_ENGINE:
        return get_engine(engine_id, voice, tts_options)
    from app.tts_engine import create_tts_engine

    engine_id = engine_id or settings.tts_engine
    tts_options = tts_options or {}
    key = (engine_id, voice, json.dumps(tts_options, sort_keys=True))
    engines = getattr(_thread_local, "engines", None)
    if engines is None:
        engines = _thread_local.engines = {}
    if key not in engines:
        engines[key] = create_tts_engine(engine_id, voice=voice, **tts_options)
    return engines[key]


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
    dialogue_voice = payload.get("dialogue_voice") or None
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
            "tts_engine": engine_id, "voice": voice, "dialogue_voice": dialogue_voice,
            "max_chars": max_chars,
            "with_effects": with_effects, "tts_options": tts_options,
            "chunk_pause_ms": pause_config[0], "chapter_pause_ms": pause_config[1],
        }), encoding="utf-8")

    ctx.log(f"synthesize patch {patch_id} (book {patch.book_id}) engine={engine_id}")
    # Model load, từng chunk và bước ghép đều có thể chạy im lặng hàng phút trên
    # engine local/GPU — càng lâu khi nhiều worker cùng tranh CPU. Không có nhịp tim,
    # reaper (queue_reap_after_seconds=120) tưởng job đã chết và trả nó về 'pending'
    # trong khi nó vẫn đang chạy và ghi log; các lần ghi tiến độ sau đó bị fence theo
    # worker_id nên bị bỏ, và trang Queue hiển thị "đang chờ" ở 0% cho job đang chạy.
    with ctx.keep_alive():
        try:
            engine = get_engine(engine_id, voice, tts_options)
            # Giọng hội thoại chỉ là một engine thứ hai cùng model: cùng engine để
            # sample rate không lệch (audio_merge ghép theo một rate duy nhất).
            dialogue_engine = None
            if dialogue_voice and dialogue_voice != voice:
                dialogue_engine = get_engine(engine_id, dialogue_voice, tts_options)
            chunk_concurrency = int(payload.get("chunk_concurrency") or settings.tts_chunk_concurrency or 1)
            audio_path, chunk_count = synthesize_patch(
                ctx, patch, engine, Path(settings.data_root),
                engine_id=engine_id,
                effective_max_chars=max_chars if max_chars > 0 else None,
                with_effects=with_effects, pause_config=pause_config,
                dialogue_engine=dialogue_engine,
                chunk_concurrency=chunk_concurrency,
                tts_options=tts_options,
                voice=voice,
                dialogue_voice=dialogue_voice,
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


def audio_seconds(audio_path: str | None) -> float | None:
    """Thời lượng file audio kết quả, để trang Queue hiển thị chi tiết job TTS."""
    if not audio_path:
        return None
    try:
        info = sf.info(audio_path)
        return round(info.frames / info.samplerate, 1) if info.samplerate else None
    except (OSError, RuntimeError):
        return None


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
    # Gộp cả sách (concat + normalize) cũng chạy im lặng; giữ nhịp tim để reaper
    # không trả job về 'pending' giữa lúc nó đang nối final.wav.
    with ctx.keep_alive():
        final_path = finalize_book_if_ready(ctx, patch.book_id)
    if chunk_count:
        ctx.progress(chunk_count, chunk_count, phase="synthesizing")
    return {"audio_path": audio_path, "chunks": chunk_count,
            "audio_seconds": audio_seconds(audio_path),
            "final_audio_path": final_path, "skipped": skipped,
            "automation": outcome["state"]}


def _chunk_engine(item: dict, engine, dialogue_engine):
    """Engine đọc một chunk: giọng hội thoại cho lời thoại, giọng dẫn cho phần còn lại."""
    if dialogue_engine is not None and item.get("kind") == "dialogue":
        return dialogue_engine
    return engine


def _engine_fingerprint(engine, engine_id: str) -> str:
    return getattr(engine, "config_fingerprint", lambda: engine_id)()


def _engine_voice(engine) -> str | None:
    voice = getattr(engine, "voice", None)
    return voice if isinstance(voice, str) else None


def _intro_cta_vars(channel_name: str, book, chapter_title: str | None) -> dict:
    return {
        "channel_name": channel_name or "",
        "book_title": getattr(book, "title", "") or "",
        "chapter_title": chapter_title or "",
        "author": getattr(book, "author", "") or "",
    }


def _pick_cta_gaps(plan: list[dict], cta_max: int) -> list[int]:
    """Indices in plan where a new chapter starts (excluding 0)."""
    gaps = [i for i, item in enumerate(plan) if item.get("is_chapter_start") and i != 0]
    if not gaps or cta_max <= 0:
        return []
    # Deterministic single pick; for cta_max>1 pick non-adjacent would be added later.
    return gaps[:0]  # placeholder handled by caller


def _deterministic_cta_index(patch_id: int, cta_template: str, channel_name: str, num_gaps: int) -> int:
    import hashlib

    h = hashlib.md5(f"{patch_id}|{cta_template}|{channel_name}".encode("utf-8")).hexdigest()
    return int(h[:8], 16) % num_gaps if num_gaps else 0


def _inject_intro_cta(
    conn, patch, plan: list[dict], effective_max_chars: int | None
) -> list[dict]:
    """Return new plan with intro at head and one deterministic CTA at a chapter gap."""
    from app import chunker as _chunker

    try:
        from app.production_defaults import get_effective_intro_config
        from app.intro_templates import render_template
        from app.repository import has_speakable_text
    except Exception:
        return plan

    book = None
    try:
        from app import repository as _repo

        book = _repo.get_book(conn, patch.book_id)
    except Exception:
        book = None
    if book is None:
        return plan

    intro_cfg = get_effective_intro_config(conn, book)
    max_chars = int(effective_max_chars or getattr(book, "tts_max_chars", 0) or 400)
    if max_chars <= 0:
        max_chars = 400

    new_plan = list(plan)

    # --- Intro: đầu patch ---
    if intro_cfg.get("intro_enabled") and str(intro_cfg.get("intro_template") or "").strip():
        # chapter_title cho intro = chương đầu của patch
        first_title = None
        for item in plan:
            if item.get("chapter_title"):
                first_title = item["chapter_title"]
                break
        if first_title is None:
            # fallback: title chương đầu theo DB
            try:
                chapters = _repo.get_chapters_in_range(conn, patch.book_id, patch.chapter_start, patch.chapter_end)  # type: ignore[attr-defined]
                first_title = (chapters[0].title if chapters else "") if chapters else ""
            except Exception:
                first_title = ""
        vars_intro = _intro_cta_vars(intro_cfg.get("channel_name") or "", book, first_title)
        rendered = render_template(str(intro_cfg.get("intro_template") or ""), vars_intro).strip()
        if rendered and has_speakable_text(rendered):
            parts = _chunker._hard_split(rendered, max_chars)
            intro_items = []
            for part in parts:
                p = part.strip()
                if p and has_speakable_text(p):
                    intro_items.append({
                        "text": p,
                        "kind": "narration",
                        "chapter_index": None,
                        "chapter_title": None,
                        "is_chapter_start": False,
                        "is_intro": True,
                    })
            new_plan = intro_items + new_plan

    # --- CTA: ranh giới chương ---
    cta_max = int(intro_cfg.get("cta_max_per_patch") or 0)
    if intro_cfg.get("cta_enabled") and cta_max > 0 and str(intro_cfg.get("cta_template") or "").strip():
        # gaps sau khi đã chèn intro
        gaps = [i for i, it in enumerate(new_plan) if it.get("is_chapter_start") and i != 0]
        if gaps:
            # deterministic pick 1 gap (k=1). For cta_max>1 we pick min(k, len(gaps)) distinct.
            import hashlib

            h = hashlib.md5(
                f"{patch.id}|{intro_cfg.get('cta_template')}|{intro_cfg.get('channel_name')}".encode("utf-8")
            ).hexdigest()
            # pick up to cta_max distinct gaps deterministically
            k = min(cta_max, len(gaps))
            # order gaps by hash-derived priority
            scored = []
            for idx, gap in enumerate(gaps):
                hh = hashlib.md5(f"{h}|{gap}".encode("utf-8")).hexdigest()
                scored.append((int(hh[:8], 16), gap))
            scored.sort(key=lambda x: x[0])
            chosen_gaps = sorted(scored[:k], key=lambda x: x[1])
            # insert CTA chunks before each chosen gap, from last to first to keep indices
            for _, gap in reversed(chosen_gaps):
                # chapter_title cho CTA: tên chương mở tại gap
                cta_title = new_plan[gap].get("chapter_title") or ""
                vars_cta = _intro_cta_vars(intro_cfg.get("channel_name") or "", book, cta_title)
                rendered_cta = render_template(str(intro_cfg.get("cta_template") or ""), vars_cta).strip()
                if not rendered_cta or not has_speakable_text(rendered_cta):
                    continue
                parts = _chunker._hard_split(rendered_cta, max_chars)
                cta_items = []
                for part in parts:
                    p = part.strip()
                    if p and has_speakable_text(p):
                        cta_items.append({
                            "text": p,
                            "kind": "narration",
                            "chapter_index": None,
                            "chapter_title": None,
                            "is_chapter_start": False,
                            "is_cta": True,
                        })
                if cta_items:
                    new_plan[gap:gap] = cta_items

    return new_plan


def synthesize_patch(
    ctx, patch, engine, data_root: Path, *,
    engine_id: str, effective_max_chars: int | None, with_effects: bool,
    pause_config: tuple[int, int] | None = None,
    dialogue_engine=None,
    chunk_concurrency: int | None = None,
    tts_options: dict | None = None,
    voice: str | None = None,
    dialogue_voice: str | None = None,
) -> tuple[str, int]:
    """Tổng hợp một patch thành WAV.

    ``dialogue_engine`` (tùy chọn) là engine thứ hai cùng model, chỉ dùng cho
    các chunk có ``kind == "dialogue"`` — hội thoại đọc bằng một giọng khác
    người dẫn để người nghe phân biệt được lượt nói. Không truyền thì mọi
    chunk dùng ``engine`` như trước.
    """
    plan_inputs = repository.fetch_patch_chunk_inputs(ctx.conn, patch, max_chars=effective_max_chars)
    book = repository.get_book(ctx.conn, patch.book_id)
    plan = repository.build_chunk_plan_from_inputs(plan_inputs)
    if not plan:
        raise JobFatalError("patch không có nội dung đọc được")
    # Inject intro (đầu patch) và CTA (ranh giới chương) — deterministic, split theo max_chars
    plan = _inject_intro_cta(ctx.conn, patch, plan, effective_max_chars)
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
    progress_lock = threading.Lock()
    active_chunks: set[int] = set()

    def report_chunk_started(index: int) -> None:
        # Local engines can spend minutes inside one synthesize_chunk call. Keep
        # progress/detail fresh while the model works, without changing the
        # completed chunk count or writing to SQLite from a worker thread.
        with progress_lock:
            active_chunks.add(index)
            ctx.detail = {
                "kind": "tts",
                "active_chunk": min(active_chunks) + 1,
                "active_chunks": len(active_chunks),
                "total_chunks": total,
            }

    def report_chunk_finished(index: int) -> None:
        with progress_lock:
            active_chunks.discard(index)
            ctx.detail = {
                "kind": "tts",
                "active_chunk": min(active_chunks) + 1 if active_chunks else None,
                "active_chunks": len(active_chunks),
                "total_chunks": total,
            }
    from app.tts_api_providers import is_api_engine
    allow_parallel = (engine_id == "zerotts" or is_api_engine(engine_id))
    parallelism = max(1, min(int(chunk_concurrency or settings.tts_chunk_concurrency or 1), 16)) if allow_parallel else 1

    if not settings.tts_write_chunk_files:
        wavs = []
        rates: set[int] = set()
        if parallelism > 1:
            def synthesize_in_worker(index, item):
                if ctx.should_cancel():
                    raise asyncio.CancelledError()
                report_chunk_started(index)
                selected_voice = dialogue_voice if dialogue_engine is not None and item.get("kind") == "dialogue" else voice
                worker_engine = get_worker_engine(engine_id, selected_voice, tts_options)
                ctx.log(f"chunk {index + 1}/{total} văn bản: {item['text']}")
                try:
                    arr = level_chunk_loudness(worker_engine.synthesize_chunk(
                        item["text"], reference_wav_path=ref_wav, prompt_text=ref_text))
                    return index, arr, worker_engine.sample_rate
                finally:
                    report_chunk_finished(index)

            results = {}
            with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="zerotts-chunk") as pool:
                futures = [pool.submit(synthesize_in_worker, i, item) for i, item in enumerate(plan)]
                for future in as_completed(futures):
                    index, arr, rate = future.result()
                    results[index] = (arr, rate)
                    ctx.log(f"chunk {index + 1}/{total} xong: {len(arr) / rate:.1f}s audio")
                    # Progress reflects completed work; persisted resume cursor remains contiguous.
                    ctx.progress(len(results), total)
                    if ctx.should_cancel():
                        for pending in futures:
                            pending.cancel()
                        raise asyncio.CancelledError()
            for index in range(total):
                arr, rate = results[index]
                wavs.append(arr)
                rates.add(rate)
        else:
            for index, item in enumerate(plan):
                if ctx.should_cancel():
                    raise asyncio.CancelledError()
                chunk_engine = _chunk_engine(item, engine, dialogue_engine)
                report_chunk_started(index)
                ctx.log(f"chunk {index + 1}/{total} văn bản: {item['text']}")
                try:
                    arr = chunk_engine.synthesize_chunk(item["text"], reference_wav_path=ref_wav, prompt_text=ref_text)
                finally:
                    report_chunk_finished(index)
                arr = level_chunk_loudness(arr)
                rates.add(chunk_engine.sample_rate)
                ctx.log(f"chunk {index + 1}/{total} xong: {len(arr) / chunk_engine.sample_rate:.1f}s audio")
                wavs.append(arr)
                ctx.progress(index + 1, total)
        if len(rates) != 1:
            raise RuntimeError(
                f"hai giọng đọc có sample rate lệch nhau {sorted(rates)}; "
                "chọn hai giọng cùng model/rate rồi tạo lại")
        sample_rate = rates.pop()
        frame_counts = [len(a) for a in wavs]
        chapters, _ = audio_merge.build_chapter_marks(plan, frame_counts, sample_rate, pauses)
        audio_merge.atomic_write_wav(audio_path, lambda p: audio_merge.concat_chunks_to_wav(wavs, sample_rate, p, pause_ms=pauses))
        _apply_effects(ctx.conn, with_effects, audio_path, plan)
        normalize_wav_in_place(audio_path)
        audio_merge.try_write_timeline(timeline_path, sample_rate, chapters, sf.info(audio_path).frames)
        subtitle_gen.try_generate(subtitle_path, plan, frame_counts, sample_rate, pauses)
        ctx.progress(total, total, phase="synthesizing")
        return audio_path, total

    repository.update_patch_chunk_count(ctx.conn, patch.id, total)

    # Chunk sống theo chương (books/{id}/chapters/NNNN, xem app/chunk_store.py), không
    # theo patch: build lại patch, sửa một chương hay một chương lỗi giữa chừng đều chỉ
    # phải tổng hợp những chunk thiếu/lệch key — phần còn lại được ghép lại nguyên trạng.
    patch_dir = repository.get_patch_chunk_dir(patch.book_id, patch.patch_index)
    targets = chunk_store.resolve_plan_chunks(patch.book_id, patch_dir, plan)
    # Fingerprint/voice tính theo engine của từng chunk: key chunk đã gồm voice
    # (chunk_store.chunk_key), nên đổi giọng hội thoại chỉ làm lệch key của các
    # chunk thoại — phần tường thuật vẫn được dùng lại.
    fingerprints: dict[int, tuple[str, str | None]] = {
        id(e): (_engine_fingerprint(e, engine_id), _engine_voice(e))
        for e in ([engine] if dialogue_engine is None else [engine, dialogue_engine])
    }
    frame_counts: list[int] = []
    sample_rates: set[int] = set()
    reused = 0
    chapters_by_index = {c.chapter_index: c for c in plan_inputs.get("chapters") or []}
    tasks = []
    for index, (item, (cache, local)) in enumerate(zip(plan, targets)):
        chunk_engine = _chunk_engine(item, engine, dialogue_engine)
        chunk_voice = _engine_voice(chunk_engine)
        model_cfg, voice = fingerprints[id(chunk_engine)]
        key = chunk_store.chunk_key(engine_id, model_cfg, item["text"])
        if cache.is_valid(local, key):
            entry = cache.entry(local) or {}
            frames, rate = int(entry.get("frames") or 0), int(entry.get("sample_rate") or 0)
            if not frames or not rate:
                info = sf.info(str(cache.path(local)))
                frames, rate = info.frames, info.samplerate
            sample_rates.add(rate)
            frame_counts.append(frames)
            reused += 1
            continue
        tasks.append((index, item, cache, local, key, voice))
        frame_counts.append(0)

    # Cached chunks are real completed work too, including non-contiguous cache hits.
    ctx.progress(reused, total)

    def synthesize_and_save(task):
        index, item, cache, local, key, chunk_voice = task
        if ctx.should_cancel():
            raise asyncio.CancelledError()
        worker_engine = get_worker_engine(engine_id, chunk_voice, tts_options) if parallelism > 1 else _chunk_engine(item, engine, dialogue_engine)
        report_chunk_started(index)
        ctx.log(f"chunk {index + 1}/{total} văn bản: {item['text']}")
        try:
            arr = level_chunk_loudness(worker_engine.synthesize_chunk(
                item["text"], reference_wav_path=ref_wav, prompt_text=ref_text))
        finally:
            report_chunk_finished(index)
        rate = worker_engine.sample_rate
        cache.dir.mkdir(parents=True, exist_ok=True)
        audio_merge.atomic_write_wav(str(cache.path(local)), lambda p, a=arr, r=rate: sf.write(p, a, r, format="WAV"))
        cache.record(local, key=key, text=item["text"], frames=len(arr), sample_rate=rate,
                     engine=engine_id, voice=chunk_voice, max_chars=plan_inputs["limit"])
        ctx.log(f"chunk {index + 1}/{total} xong: {len(arr) / rate:.1f}s audio")
        return index, len(arr), rate

    completed = {}
    if parallelism > 1 and tasks:
        with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="zerotts-chunk") as pool:
            futures = [pool.submit(synthesize_and_save, task) for task in tasks]
            for future in as_completed(futures):
                index, frames, rate = future.result()
                completed[index] = (frames, rate)
                ctx.progress(reused + len(completed), total)
                if ctx.should_cancel():
                    for pending in futures:
                        pending.cancel()
                    raise asyncio.CancelledError()
    else:
        for task in tasks:
            index, frames, rate = synthesize_and_save(task)
            completed[index] = (frames, rate)
            # Publish real completed work as each sequential local chunk finishes.
            # Previously this branch waited until all chunks had synthesized before
            # updating the queue progress, leaving the bar at 0% for long jobs.
            ctx.progress(reused + len(completed), total)
    contiguous = 0
    for index, ((item, (cache, local)), frames_rate) in enumerate(zip(zip(plan, targets), frame_counts)):
        if index in completed:
            frame_counts[index], rate = completed[index]
            sample_rates.add(rate)
        else:
            rate = int((cache.entry(local) or {}).get("sample_rate") or 0)
        if index == contiguous and (frames_rate or index in completed):
            contiguous += 1
        repository.update_patch_chunk_progress(ctx.conn, patch.id, contiguous)
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

    ctx.progress(total, total, phase="merging")
    chunk_paths = [str(cache.path(local)) for cache, local in targets]
    merge_patch_audio(ctx.conn, plan, chunk_paths, frame_counts, sample_rate, pauses,
                      audio_path, with_effects=with_effects)
    ctx.progress(total, total, phase="synthesizing")
    return audio_path, total


def merge_patch_audio(conn, plan: list[dict], chunk_paths: list[str], frame_counts: list[int],
                      sample_rate: int, pauses: list[int], audio_path: str, *,
                      with_effects: bool) -> None:
    """Ghép chunk đã có trên đĩa thành audio patch: nối theo pause plan, effects,
    normalize, rồi timeline + phụ đề từ cùng frame_counts. Dùng chung cho chunk tổng
    hợp tại chỗ và chunk tải về từ Colab/Kaggle (app/chunk_import.py), nên hai nguồn
    cho ra cùng một kiểu audio."""
    audio_path_obj = Path(audio_path)
    chapters, _ = audio_merge.build_chapter_marks(plan, frame_counts, sample_rate, pauses)
    audio_merge.atomic_write_wav(audio_path, lambda p: audio_merge.concat_wavs(chunk_paths, p, pause_ms=pauses))
    _apply_effects(conn, with_effects, audio_path, plan)
    normalize_wav_in_place(audio_path)
    audio_merge.try_write_timeline(audio_path_obj.with_suffix(".timeline.json"), sample_rate, chapters,
                                   sf.info(audio_path).frames)
    subtitle_gen.try_generate(audio_path_obj.with_suffix(".ass"), plan, frame_counts, sample_rate, pauses)


def _apply_effects(conn, with_effects: bool, audio_path: str, plan: list[dict]) -> None:
    """Overlay sound-effect clips onto the merged patch audio. Shared with the
    LightTTS preview path so effects behave identically for every engine."""
    if not with_effects:
        return
    from app.routes.text_studio import _mix_effects

    mixed = _mix_effects(Path(audio_path).read_bytes(), "\n\n".join(i["text"] for i in plan), conn)
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
    # Chỉ đổi phase: ctx.job là snapshot lúc claim (0/0), ghi nó sẽ kéo tiến độ về 0.
    ctx.progress(None, phase="merging_book")
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
