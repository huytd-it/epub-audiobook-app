"""Sinh audio cho một patch bằng engine TTS đã chọn (mọi model trong catalog).

Handler dùng chung cho toàn bộ pipeline audiobook: voxcpm2 / omnivoice /
vieneu-fast / edge-tts / gtts đều chạy cùng mã này — viết chunk WAV, resume theo
chunk, gộp patch, gộp sách và xếp hàng video khi xong.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import soundfile as sf

from app import audio_merge, repository
from app.config import settings
from app.jobqueue import store
from app.jobqueue.models import JobFatalError

_CHUNK_PAUSE_MS = 300
_engines = {}

_META_FILE = ".tts_meta"


def get_engine(engine_id: str | None = None, voice: str | None = None):
    from app.tts_engine import create_tts_engine

    engine_id = engine_id or settings.tts_engine
    key = (engine_id, voice)
    if key not in _engines:
        _engines[key] = create_tts_engine(engine_id, voice=voice)
    return _engines[key]


def chunk_fingerprint(engine, engine_id: str, max_chars: int, plan: list[dict]) -> str:
    """Hash of everything that changes the audio: engine id + engine/model config +
    voice + chunk cap + the exact chunked text. Stored in ``.tts_meta`` next to the
    chunk files; a mismatch means the chunks on disk were produced under a different
    model/voice/text and must be regenerated (not resumed)."""
    model_cfg = getattr(engine, "config_fingerprint", lambda: engine_id)()
    text = "\n\n".join(item["text"] for item in plan)
    raw = f"{engine_id}|{model_cfg}|{max_chars}|{text}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


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
        snapshot_dir = Path(settings.data_root) / "books" / str(patch.book_id) / "patches" / f"{patch.id}_chunks"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        (snapshot_dir / ".tts_request.json").write_text(json.dumps({
            "tts_engine": engine_id, "voice": voice, "max_chars": max_chars,
            "with_effects": with_effects,
        }), encoding="utf-8")

    ctx.log(f"synthesize patch {patch_id} (book {patch.book_id}) engine={engine_id}")
    try:
        engine = get_engine(engine_id, voice)
        audio_path, chunk_count = synthesize_patch(
            ctx, patch, engine, Path(settings.data_root),
            engine_id=engine_id,
            effective_max_chars=max_chars if max_chars > 0 else None,
            with_effects=with_effects,
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
) -> tuple[str, int]:
    plan_inputs = repository.fetch_patch_chunk_inputs(ctx.conn, patch, max_chars=effective_max_chars)
    book = repository.get_book(ctx.conn, patch.book_id)
    plan = repository.build_chunk_plan_from_inputs(plan_inputs)
    if not plan:
        raise JobFatalError("patch không có nội dung đọc được")

    ref_wav = book.voice_clip_path if book else None
    ref_text = book.voice_transcript if book else None
    book_dir = data_root / "books" / str(patch.book_id) / "patches"
    book_dir.mkdir(parents=True, exist_ok=True)
    audio_path = str(book_dir / f"{patch.id}.wav")
    timeline_path = Path(audio_path).with_suffix(".timeline.json")
    total = len(plan)
    ctx.progress(patch.next_chunk_index, total, phase="synthesizing")

    if not settings.tts_write_chunk_files:
        wavs = []
        for index, item in enumerate(plan):
            if ctx.should_cancel():
                raise asyncio.CancelledError()
            wavs.append(engine.synthesize_chunk(item["text"], reference_wav_path=ref_wav, prompt_text=ref_text))
            ctx.progress(index + 1, total)
        chapters, _ = audio_merge.build_chapter_marks(plan, [len(a) for a in wavs], engine.sample_rate, _CHUNK_PAUSE_MS)
        audio_merge.concat_chunks_to_wav(wavs, engine.sample_rate, audio_path, pause_ms=_CHUNK_PAUSE_MS)
        _apply_effects(ctx, with_effects, audio_path, plan)
        audio_merge.try_write_timeline(timeline_path, engine.sample_rate, chapters, sf.info(audio_path).frames)
        ctx.progress(total, total, phase="synthesizing")
        return audio_path, total

    chunk_dir = book_dir / f"{patch.id}_chunks"
    chunk_dir.mkdir(parents=True, exist_ok=True)
    repository.update_patch_chunk_count(ctx.conn, patch.id, total)

    # Fingerprint the run against the chunk files already on disk. Same config+text
    # => resume from the persisted next_chunk_index; anything else => the chunks are
    # stale, so wipe them and restart at zero.
    meta_path = chunk_dir / _META_FILE
    meta_key = chunk_fingerprint(engine, engine_id, effective_max_chars or 0, plan)
    try:
        reusable = meta_path.read_text(encoding="utf-8").strip() == meta_key
    except OSError:
        reusable = False
    if not reusable:
        for stale in chunk_dir.glob("chunk_*.wav"):
            stale.unlink(missing_ok=True)
        (chunk_dir / ".light_tts_meta").unlink(missing_ok=True)
        meta_path.write_text(meta_key, encoding="utf-8")
        if patch.next_chunk_index:
            repository.update_patch_chunk_progress(ctx.conn, patch.id, 0)
        ctx.log(f"model/config đổi (fingerprint {meta_key[:8]}); xóa chunk cũ, chạy lại từ đầu")
        start_index = 0
    else:
        start_index = max(0, min(patch.next_chunk_index, total))
    if start_index > 0:
        ctx.log(f"resume từ chunk {start_index}/{total}")

    frame_counts = []
    for index, item in enumerate(plan):
        chunk_path = chunk_dir / f"chunk_{index:03d}.wav"
        if index >= start_index:
            if ctx.should_cancel():
                raise asyncio.CancelledError()
            arr = engine.synthesize_chunk(item["text"], reference_wav_path=ref_wav, prompt_text=ref_text)
            sf.write(chunk_path, arr, engine.sample_rate)
            repository.update_patch_chunk_progress(ctx.conn, patch.id, index + 1)
            ctx.progress(index + 1, total)
        frame_counts.append(sf.info(str(chunk_path)).frames)

    chapters, _ = audio_merge.build_chapter_marks(plan, frame_counts, engine.sample_rate, _CHUNK_PAUSE_MS)
    chunk_paths = [str(chunk_dir / f"chunk_{i:03d}.wav") for i in range(total)]
    ctx.progress(total, total, phase="merging")
    audio_merge.concat_wavs(chunk_paths, audio_path, pause_ms=_CHUNK_PAUSE_MS)
    _apply_effects(ctx, with_effects, audio_path, plan)
    audio_merge.try_write_timeline(timeline_path, engine.sample_rate, chapters, sf.info(audio_path).frames)
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
    repository.set_book_final_audio(ctx.conn, book_id, final_path)
    ctx.log(f"gộp xong final.wav cho sách {book_id}")
    if book is None:
        return final_path
    has_image = bool(book.background_image_path) or any(p.image_path for p in patches)
    if not has_image:
        ctx.log("sách không có ảnh nào dùng được — bỏ qua bước tạo video")
        return final_path
    book_job = repository.enqueue_book_job(ctx.conn, book_id, "video")
    job_id = store.enqueue(ctx.conn, "video", payload={"book_job_id": book_job.id}, book_id=book_id, dedupe_key=f"video:book_job={book_job.id}")
    ctx.log(f"đã xếp hàng job video (book_job={book_job.id}, job={job_id})")
    return final_path
