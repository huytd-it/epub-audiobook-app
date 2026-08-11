"""Render or recover one patch video through the unified queue."""
from __future__ import annotations

from pathlib import Path
import json
import tempfile

from app import image_overlay, repository, video_gen
from app.config import settings
from app.jobqueue import store
from app.jobqueue.models import JobFatalError
from app.video_config import get_book_video_config
from app.video_integrity import validate_video
from app.video_publish import publish_validated_video
from app.video_recovery import resume_upload_after_render
from app.video_repository import upsert_patch_video


def handle(ctx) -> dict:
    patch_id = ctx.job.payload.get("patch_id")
    if patch_id is None:
        raise JobFatalError("payload missing patch_id")
    patch = repository.get_patch(ctx.conn, patch_id)
    if patch is None:
        raise JobFatalError(f"patch {patch_id} not found")
    book = repository.get_book(ctx.conn, patch.book_id)
    pipeline = ctx.conn.execute("SELECT * FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone()
    if book is None:
        raise JobFatalError("source_unavailable: book missing")
    if not patch.audio_path or not Path(patch.audio_path).is_file():
        raise JobFatalError(f"source_unavailable: audio missing: {patch.audio_path}")
    ctx.progress(0, 6, phase="preparing")
    recovery_upload_id = ctx.job.payload.get("recovery_upload_id")
    recovery_pipeline = pipeline if recovery_upload_id is not None else None
    output = (recovery_pipeline["video_path"] if recovery_pipeline else None) or str(
        Path(settings.data_root) / "books" / str(book.id) / "patch_videos" / f"{patch_id}.mp4"
    )
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    media = json.loads(recovery_pipeline["media_snapshot"] or "{}") if recovery_pipeline else {}
    render_config = media.get("render_config")
    if recovery_pipeline:
        image = recovery_pipeline["thumbnail_path"]
        if not image or not Path(image).is_file():
            raise JobFatalError(f"source_unavailable: thumbnail missing: {image}")
        if render_config is not None and not isinstance(render_config, dict):
            raise JobFatalError("source_unavailable: invalid render config snapshot")
        render_config = render_config or {
            "resolution": book.video_resolution or "1920x1080",
            "fps": book.video_fps or 30,
        }
        for key in ("music_path", "intro_audio", "outro_audio"):
            value = render_config.get(key)
            if value and not Path(value).is_file():
                raise JobFatalError(f"source_unavailable: {key} missing: {value}")
        ctx.progress(1, 6, phase="overlay")
        ctx.progress(2, 6, phase="encoding")
        with ctx.keep_alive():
            publish_validated_video(
                output,
                lambda temp: video_gen.generate_standalone_video(
                    patch.audio_path, image, temp, **render_config),
                validator=validate_video,
            )
    else:
        config = get_book_video_config(ctx.conn, book)
        fallback = video_gen.resolve_patch_image(patch, book, settings.default_background_image)
        raw_bg = video_gen.resolve_configured_patch_image(patch, config, fallback or "")
        if not raw_bg:
            raise JobFatalError("source_unavailable: background missing")
        backgrounds = [p for p in config.get("backgrounds", []) if isinstance(p, str)]
        sequence = len(backgrounds) > 1 and not (patch.image_path and Path(patch.image_path).exists())
        ctx.progress(1, 6, phase="overlay")
        if sequence or video_gen.is_video_background(raw_bg):
            image, image_type = raw_bg, "none"
        else:
            image = image_overlay.ensure_patch_overlay(
                book, patch, settings.default_font_path or None, background_path=raw_bg,
            ) or raw_bg
            image_type = patch.image_type if patch.image_type and patch.image_type != "static" else (book.default_image_animation or "none")
        music_path = None
        if book.music_id is not None:
            music = repository.get_music(ctx.conn, book.music_id)
            if music and Path(music.file_path).is_file():
                music_path = music.file_path
        voices = Path(settings.data_root) / "voices"
        intro = voices / config["intro_voice"] if config.get("intro_voice") else None
        outro = voices / config["outro_voice"] if config.get("outro_voice") else None
        intro = str(intro) if intro and intro.is_file() else None
        outro = str(outro) if outro and outro.is_file() else None
        width, height = (book.video_resolution or "1920x1080").split("x")
        common = {
            "resolution": (int(width), int(height)), "fps": book.video_fps or 30,
            "codec": config["codec"], "quality": config["quality"],
            "audio_bitrate": config["audio_bitrate"],
        }

        def render_main(target: str) -> None:
            if sequence:
                video_gen.generate_background_sequence(
                    backgrounds, patch.audio_path, target,
                    image_duration=float(config.get("image_duration_seconds", 15)),
                    mode=config.get("background_mode", "sequential"), seed=f"{book.id}-{patch.id}",
                    start_index=patch.patch_index, music_path=music_path,
                    music_volume=book.music_volume,
                    crossfade=bool(config.get("crossfade_enabled")),
                    crossfade_seconds=float(config.get("crossfade_seconds", 1)),
                    ken_burns=bool(config.get("ken_burns_enabled")),
                    progress_bar=bool(config.get("progress_bar_enabled")), **common,
                    waveform_config=config,
                )
            else:
                video_gen.generate_segment(
                    image, patch.audio_path, target, image_type=image_type,
                    use_nvenc=settings.use_nvenc, music_path=music_path,
                    music_volume=book.music_volume, **common,
                    waveform_config=config,
                )

        def render(target: str) -> None:
            if not intro and not outro:
                render_main(target)
                return
            with tempfile.TemporaryDirectory(prefix="patch_video_") as tmp:
                segments = []
                if intro:
                    intro_video = str(Path(tmp) / "intro.mp4")
                    video_gen.generate_segment(raw_bg, intro, intro_video, image_type="none", **common)
                    segments.append(intro_video)
                main_video = str(Path(tmp) / "main.mp4")
                render_main(main_video); segments.append(main_video)
                if outro:
                    outro_video = str(Path(tmp) / "outro.mp4")
                    video_gen.generate_segment(raw_bg, outro, outro_video, image_type="none", **common)
                    segments.append(outro_video)
                video_gen.concat_segments(segments, target)

        ctx.log(f"render patch {patch_id}: audio={patch.audio_path} background={raw_bg}")
        ctx.progress(2, 6, phase="encoding")
        with ctx.keep_alive():
            publish_validated_video(output, render, validator=validate_video)

    ctx.progress(4, 6, phase="registering")
    video = upsert_patch_video(ctx.conn, book_id=book.id, patch_id=patch_id,
                               file_path=output, resolution=book.video_resolution)
    if recovery_pipeline:
        ctx.conn.execute(
            """UPDATE patch_pipeline SET stage='upload', video_status='done', video_path=?,
               video_id=?, last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE patch_id=?""",
            (output, video["id"], patch_id),
        )
    ctx.conn.commit()
    youtube_status = None
    if recovery_upload_id is not None:
        resume_upload_after_render(ctx.conn, recovery_upload_id)
    elif ctx.job.payload.get("upload_youtube"):
        from app.patch_publishing import run_patch_publish_stage, seed_patch_video
        ctx.progress(5, 6, phase="publishing")
        seed_patch_video(ctx.conn, patch_id, video["id"], output)
        youtube_status = run_patch_publish_stage(ctx.conn, patch_id)
        upload_id = youtube_status.get("youtube_upload_id")
        if upload_id:
            privacy = ctx.job.payload.get("privacy")
            if privacy in {"private", "unlisted", "public"}:
                ctx.conn.execute(
                    "UPDATE youtube_uploads SET privacy_status=? WHERE id=?",
                    (privacy, upload_id),
                )
                ctx.conn.commit()
            store.enqueue(
                ctx.conn, "youtube_upload", payload={"upload_id": upload_id},
                book_id=book.id, dedupe_key=f"youtube_upload:upload={upload_id}",
            )
    ctx.progress(6, 6, phase="done")
    ctx.log(f"video xong -> {output}")
    return {"output_path": output, "video_id": video["id"], "youtube": youtube_status}
