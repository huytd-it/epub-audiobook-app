"""Render video cho một book_job."""
from __future__ import annotations

import logging
from pathlib import Path

from app import repository, video_gen, youtube
from app.config import settings
from app.jobqueue import store
from app.jobqueue.models import JobFatalError
from app.production_defaults import get_effective_video_config, resolve_voice_clip
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
    if book.music_id is not None:
        music = repository.get_music(ctx.conn, book.music_id)
        if music and Path(music.file_path).exists():
            music_path = music.file_path

    intro_audio = resolve_voice_clip(video_config, "intro_voice")
    outro_audio = resolve_voice_clip(video_config, "outro_voice")
    done_patches = [p for p in patches if p.status == "done" and p.audio_path]
    book_dir = Path(settings.data_root) / "books" / str(book_id)
    book_dir.mkdir(parents=True, exist_ok=True)
    out_path = str(book_dir / f"video_{book_job_id}.mp4")

    def _on_progress(event: str, fields: dict) -> None:
        if event in _ENCODING_EVENTS:
            ctx.progress(0, 1, phase="encoding")
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
            music_gaps=video_config,
            codec=video_config["codec"],
            quality=video_config["quality"],
            audio_bitrate=video_config["audio_bitrate"],
            video_config=video_config,
            intro_audio=intro_audio,
            outro_audio=outro_audio,
            font_path=settings.default_font_path or None,
            on_progress=_on_progress,
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
