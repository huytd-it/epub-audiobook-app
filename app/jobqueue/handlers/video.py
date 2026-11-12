"""Render video cho một book_job."""
from __future__ import annotations

import logging
from pathlib import Path

from app import repository, video_gen, youtube
from app.config import settings
from app.image_overlay import save_video_branding
from app.jobqueue import store
from app.jobqueue.models import JobFatalError
from app.music_bed import probe_duration
from app.production_defaults import get_effective_branding_config, get_effective_video_config, resolve_voice_clip
from app.video_integrity import validate_video
from app.video_publish import publish_validated_video
from app.video_recovery import resume_upload_after_render

logger = logging.getLogger(__name__)
_ENCODING_EVENTS = ("segment.ffmpeg_start", "concat.ffmpeg_start")


def _youtube_ready() -> bool:
    return youtube.is_configured()


def handle(ctx) -> dict:
    book_job_id = ctx.job.payload.get("book_job_id")
    if book_job_id is None:
        raise JobFatalError("payload thiếu book_job_id")
    row = ctx.conn.execute(
        "SELECT id, book_id, job_type FROM book_job WHERE id=?", (book_job_id,)
    ).fetchone()
    if row is None:
        raise JobFatalError(f"book_job {book_job_id} không tồn tại")
    book_id = row["book_id"]

    ctx.progress(0, 1, phase="preparing")
    try:
        output_path = _render(ctx, book_job_id, book_id)
    except JobFatalError as exc:
        repository.mark_book_job_failed(ctx.conn, book_job_id, str(exc))
        raise
    except Exception as exc:
        repository.mark_book_job_failed(ctx.conn, book_job_id, str(exc))
        raise

    repository.mark_book_job_done(ctx.conn, book_job_id, output_path)
    repository.set_book_final_video(ctx.conn, book_id, output_path)
    ctx.progress(1, 1, phase="done")
    ctx.log(f"video xong -> {output_path}")
    recovery_upload_id = ctx.job.payload.get("recovery_upload_id")
    if recovery_upload_id is not None:
        resume_upload_after_render(ctx.conn, recovery_upload_id)
    else:
        _maybe_enqueue_upload(ctx, book_id, book_job_id, output_path)
    return {"output_path": output_path}


def _render(ctx, book_job_id: int, book_id: int) -> str:
    book = repository.get_book(ctx.conn, book_id)
    patches = repository.list_patches(ctx.conn, book_id)
    if book is None or not book.final_audio_path:
        raise JobFatalError(f"sách {book_id} chưa có final_audio_path")

    music_path = None
    video_config = get_effective_video_config(ctx.conn, book)
    branding = get_effective_branding_config(ctx.conn, book)
    if book.music_id is not None:
        music = repository.get_music(ctx.conn, book.music_id)
        if music and Path(music.file_path).exists():
            music_path = music.file_path

    intro_audio = resolve_voice_clip(video_config, "intro_voice")
    outro_audio = resolve_voice_clip(video_config, "outro_voice")
    done_patches = [p for p in patches if p.status == "done" and p.audio_path]
    # Weight each chapter by its expected encoded workload. This makes ETA
    # respond to audio length and the selected output settings, while elapsed
    # render speed accounts for the actual machine/encoder performance.
    width, height = (int(value) for value in str(video_config.get("resolution") or book.video_resolution or "1920x1080").split("x"))
    fps = int(video_config.get("fps") or book.video_fps or 30)
    pixels_factor = (width * height / (1920 * 1080)) ** 0.65
    fps_factor = max(1, fps) / 30
    codec = str(video_config.get("codec") or "libx264")
    quality = int(video_config.get("quality") or 20)
    codec_factor = 0.65 if codec == "h264_nvenc" or settings.use_nvenc else 1.0
    quality_factor = max(0.75, min(1.5, 1 + (20 - quality) * 0.018))
    complexity = pixels_factor * fps_factor * codec_factor * quality_factor
    patch_weights = {}
    for patch in done_patches:
        duration = probe_duration(patch.audio_path) or 60
        patch_weights[patch.patch_index] = max(1, round(duration * complexity * 1000))
    concat_weight = max(1000, round(sum(patch_weights.values()) * 0.08))
    total_work = sum(patch_weights.values()) + concat_weight
    completed_work = 0
    completed_patches = set()
    patch_numbers = {patch.patch_index: index for index, patch in enumerate(done_patches, 1)}
    book_dir = Path(settings.data_root) / "books" / str(book_id)
    book_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(book_dir / f"video_{book_job_id}.mp4")

    # Transparent branding layers (plus a moving-watermark sidecar) for FFmpeg.
    w, h = (book.video_resolution or "1920x1080").split("x")
    branding_overlay_path = save_video_branding(
        (int(w), int(h)), branding, book_dir / ".branding" / f"branding_book_{book_job_id}.png")

    def _on_progress(event: str, fields: dict) -> None:
        nonlocal completed_work
        patch_index = fields.get("patch_index")
        if event in _ENCODING_EVENTS and isinstance(patch_index, int):
            patch = next((item for item in done_patches if item.patch_index == patch_index), None)
            patch_number = patch_numbers.get(patch_index, len(completed_patches) + 1)
            title = getattr(patch, "name", None) if patch else None
            detail = f"Video · đoạn {patch_number}/{len(done_patches)}"
            if title:
                detail += f" · {title}"
            detail += f" · {width}×{height} · {fps}fps · {codec}"
            ctx.progress(completed_work, total_work, phase=detail)
        elif event in {"video.segment_done", "video.segment_skipped"} and isinstance(patch_index, int):
            if patch_index not in completed_patches:
                completed_patches.add(patch_index)
                completed_work += patch_weights.get(patch_index, 0)
            ctx.progress(completed_work, total_work,
                         phase=f"Video · xong {len(completed_patches)}/{len(done_patches)} đoạn · {width}×{height} · {fps}fps")
        elif event == "concat.start":
            ctx.progress(completed_work, total_work,
                         phase=f"Video · ghép {len(done_patches)} đoạn cuối · {width}×{height} · {fps}fps")
        elif event == "concat.done":
            completed_work = total_work
            ctx.progress(completed_work, total_work, phase="Video · hoàn tất ghép")
        detail = " ".join(f"{k}={v}" for k, v in fields.items())
        level = logging.ERROR if event.endswith(".failed") else logging.INFO
        ctx.log(f"{event} {detail}".strip(), level=level)
        ctx.heartbeat()

    def render(temp_path: str) -> None:
        video_gen.generate_full_video(
            done_patches, book, temp_path,
            default_image=settings.default_background_image,
            use_nvenc=settings.use_nvenc,
            music_path=music_path,
            music_volume=book.music_volume,
            music_mix=video_config,
            codec=video_config["codec"],
            quality=video_config["quality"],
            audio_bitrate=video_config["audio_bitrate"],
            video_config=video_config,
            intro_audio=intro_audio,
            outro_audio=outro_audio,
            font_path=settings.default_font_path or None,
            on_progress=_on_progress,
            branding_overlay_path=branding_overlay_path,
        )
    ctx.progress(0, 1, phase="encoding")
    publish_validated_video(out_path, render, validator=validate_video)
    return out_path


def _maybe_enqueue_upload(ctx, book_id: int, book_job_id: int, output_path: str) -> None:
    if not settings.youtube_auto_upload or not _youtube_ready():
        return
    try:
        book = repository.get_book(ctx.conn, book_id)
        if book is None:
            return
        info = repository.build_youtube_description(ctx.conn, book_id)
        upload_id = youtube.enqueue_upload(
            ctx.conn,
            video_path=output_path,
            title=book.title,
            description=info["description"],
            tags=info["tags"],
            privacy_status=settings.youtube_default_privacy,
            render_source_type="book",
            render_source_id=book_job_id,
        )
        store.enqueue(
            ctx.conn, "youtube_upload", payload={"upload_id": upload_id},
            book_id=book_id, dedupe_key=f"youtube_upload:upload={upload_id}",
        )
        ctx.log(f"đã xếp hàng upload YouTube (upload_id={upload_id})")
    except Exception as exc:
        ctx.log(f"không xếp hàng được upload YouTube: {exc}", level=logging.WARNING)
