"""Render or recover one patch video through the unified queue."""
from __future__ import annotations

from functools import partial
from pathlib import Path
import json
import tempfile

import soundfile as sf

from app import image_overlay, repository, video_gen
from app.config import settings
from app.jobqueue import store
from app.jobqueue.models import JobFatalError
from app.patch_publishing import MAX_PATCH_RENDER_ATTEMPTS, audio_fingerprint
from app.video_config import get_book_video_config
from app.video_integrity import (VideoExpectation, validate_video,
                                 validation_report_json)
from app.video_publish import VideoValidationError, publish_validated_video
from app.video_recovery import resume_upload_after_render
from app.video_repository import upsert_patch_video


def _expected_duration_seconds(audio_path: str, render_config: dict) -> float | None:
    """WAV length + intro/outro — what the muxed output should measure."""
    try:
        total = 0.0
        for path in [audio_path, *((render_config.get("intro_audio"), render_config.get("outro_audio"))
                                   if isinstance(render_config, dict) else ())]:
            if not path or not Path(path).is_file():
                continue
            info = sf.info(str(path))
            total += info.frames / info.samplerate
        return total
    except (OSError, RuntimeError, ValueError, sf.SoundFileError):
        return None


def _expected_video(audio_path: str, render_config: dict) -> VideoExpectation:
    """The frozen config a patch render was asked to produce; duration measured
    from the source WAV so a truncated output is caught at validation time."""
    width = height = None
    try:
        width, height = (int(v) for v in str(render_config.get("resolution") or "0x0").split("x"))
    except (TypeError, ValueError):
        width = height = None
    fps = None
    try:
        fps = float(render_config.get("fps"))
    except (TypeError, ValueError):
        fps = None
    return VideoExpectation(
        duration_seconds=_expected_duration_seconds(audio_path, render_config),
        width=width or None,
        height=height or None,
        fps=fps,
        pixel_format="yuv420p",
        rotation=0,
    )


def _persist_validation_report(ctx, patch_id: int, result,
                               expected: VideoExpectation | None = None) -> None:
    ctx.conn.execute(
        "UPDATE patch_pipeline SET validation_report_json=?, updated_at=CURRENT_TIMESTAMP WHERE patch_id=?",
        (validation_report_json(result, expected), patch_id),
    )
    ctx.conn.commit()


def _account_render_attempt(ctx, patch_id: int, pipeline, recovery_upload_id: int | None) -> int:
    """Đếm và khóa số lần render một patch video (tổng tối đa MAX_PATCH_RENDER_ATTEMPTS).

    Tăng 1 mỗi lần handler vào (kể cả retry sau lỗi và rerender phục hồi); vượt ngưỡng
    => định dạng job thành fatal và ghi trạng thái failed lên pipeline."""
    attempts = int(pipeline.get("render_attempts") or 0)
    if attempts >= MAX_PATCH_RENDER_ATTEMPTS:
        ctx.conn.execute(
            """UPDATE patch_pipeline SET preflight_status='failed',
               preflight_error_code='render_attempt_limit',
               preflight_error=?, updated_at=CURRENT_TIMESTAMP WHERE patch_id=?""",
            (f"Đã render {attempts}/{MAX_PATCH_RENDER_ATTEMPTS} lần — không chạy tiếp", patch_id),
        )
        if recovery_upload_id is not None:
            ctx.conn.execute(
                """UPDATE youtube_uploads SET status='failed', validation_status='failed',
                   validation_error_code='render_attempt_limit',
                   validation_error_message=? WHERE id=?""",
                (f"render attempt limit {MAX_PATCH_RENDER_ATTEMPTS} exhausted", recovery_upload_id),
            )
        ctx.conn.commit()
        raise JobFatalError(
            f"render_attempt_limit: patch {patch_id} đã render {attempts}/{MAX_PATCH_RENDER_ATTEMPTS} lần")
    next_attempts = attempts + 1
    ctx.conn.execute(
        """UPDATE patch_pipeline SET render_attempts=?,
           preflight_status=CASE WHEN ? >= ? THEN 'failed' ELSE preflight_status END,
           updated_at=CURRENT_TIMESTAMP WHERE patch_id=?""",
        (next_attempts, next_attempts, MAX_PATCH_RENDER_ATTEMPTS, patch_id),
    )
    ctx.conn.commit()
    return next_attempts


def _render_from_snapshot(ctx, patch, book, pipeline: dict, snapshot: dict) -> str:
    """Render theo snapshot đóng băng lúc enqueue — config/audio/media không được
    đọc lại từ DB; bất kỳ lệch lạc nào (audio đổi, media thiếu) là fatal."""
    audio = snapshot.get("audio_path")
    if not audio or snapshot.get("audio_fingerprint") != audio_fingerprint(patch):
        raise JobFatalError(
            "source_changed: audio khác với snapshot lúc enqueue — hãy xử lý inbox lại")
    if not Path(audio).is_file():
        raise JobFatalError(f"source_unavailable: audio missing: {audio}")
    thumbnail = snapshot.get("thumbnail_path")
    if not thumbnail or not Path(thumbnail).is_file():
        raise JobFatalError(f"source_unavailable: thumbnail missing: {thumbnail}")
    render_config = snapshot.get("render_config")
    if not isinstance(render_config, dict):
        raise JobFatalError("source_unavailable: invalid render config snapshot")
    for key in ("music_path", "intro_audio", "outro_audio"):
        value = render_config.get(key)
        if value and not Path(value).is_file():
            raise JobFatalError(f"source_unavailable: {key} missing: {value}")
    output = str(
        Path(settings.data_root) / "books" / str(book.id) / "patch_videos" / f"{patch.id}.mp4"
    )
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    ctx.progress(1, 6, phase="overlay")
    ctx.progress(2, 6, phase="encoding")
    image = snapshot.get("image") or thumbnail
    if not image or not Path(image).is_file():
        raise JobFatalError(f"source_unavailable: background missing: {image}")
    sequence = bool(snapshot.get("sequence"))
    backgrounds = [p for p in snapshot.get("backgrounds") or [] if isinstance(p, str)]
    if sequence and not backgrounds:
        raise JobFatalError("source_unavailable: sequence background list empty")

    width, height = str(render_config.get("resolution") or "1920x1080").split("x")
    common = {
        "resolution": (int(width), int(height)),
        "fps": int(render_config.get("fps") or 30),
        "codec": render_config.get("codec") or "libx264",
        "quality": int(render_config.get("crf") or 23),
        "audio_bitrate": render_config.get("audio_bitrate") or "192k",
    }
    intro = render_config.get("intro_audio")
    outro = render_config.get("outro_audio")
    music_path = render_config.get("music_path")
    music_volume = render_config.get("music_volume", 0.15)
    seq_config = snapshot.get("sequence_config") or {}
    waveform_config = seq_config.get("waveform_config") or {}

    def render_main(target: str) -> None:
        if sequence:
            for bg in backgrounds:
                if not Path(bg).is_file():
                    raise JobFatalError(f"source_unavailable: background missing: {bg}")
            video_gen.generate_background_sequence(
                backgrounds, audio, target,
                image_duration=float(seq_config.get("image_duration_seconds", 15)),
                mode=seq_config.get("background_mode", "sequential"),
                seed=f"{book.id}-{patch.id}",
                start_index=patch.patch_index,
                music_path=music_path, music_volume=music_volume,
                crossfade=bool(seq_config.get("crossfade_enabled")),
                crossfade_seconds=float(seq_config.get("crossfade_seconds", 1)),
                ken_burns=bool(seq_config.get("ken_burns_enabled")),
                progress_bar=bool(seq_config.get("progress_bar_enabled")),
                **common, waveform_config=waveform_config,
            )
        else:
            video_gen.generate_segment(
                image, audio, target,
                image_type=snapshot.get("image_type") or "none",
                use_nvenc=settings.use_nvenc, music_path=music_path,
                music_volume=music_volume, **common, waveform_config=waveform_config,
            )

    def render(target: str) -> None:
        if not intro and not outro:
            render_main(target)
            return
        with tempfile.TemporaryDirectory(prefix="patch_video_") as tmp:
            segments = []
            if intro:
                intro_video = str(Path(tmp) / "intro.mp4")
                video_gen.generate_segment(image, intro, intro_video, image_type="none", **common)
                segments.append(intro_video)
            main_video = str(Path(tmp) / "main.mp4")
            render_main(main_video)
            segments.append(main_video)
            if outro:
                outro_video = str(Path(tmp) / "outro.mp4")
                video_gen.generate_segment(image, outro, outro_video, image_type="none", **common)
                segments.append(outro_video)
            video_gen.concat_segments(segments, target)

    ctx.log(f"render patch {patch.id} từ snapshot (sequence={sequence})")
    expected = _expected_video(audio, render_config)
    validator = partial(validate_video, expected=expected)
    with ctx.keep_alive():
        try:
            result = publish_validated_video(output, render, validator=validator)
        except VideoValidationError as exc:
            _persist_validation_report(ctx, patch.id, exc.result, expected)
            raise
    _persist_validation_report(ctx, patch.id, result, expected)
    return output


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
    recovery_upload_id = ctx.job.payload.get("recovery_upload_id")
    recovery_pipeline = (dict(pipeline) if pipeline else None) if recovery_upload_id is not None else None
    ctx.progress(0, 6, phase="preparing")

    snapshot = ctx.job.payload.get("snapshot")
    if isinstance(snapshot, dict):
        if ctx.job.payload.get("schema_version") != 1:
            raise JobFatalError("payload snapshot unsupported")
        pipeline_row = dict(pipeline) if pipeline else {}
        _account_render_attempt(ctx, patch_id, pipeline_row, recovery_upload_id)
        output = _render_from_snapshot(ctx, patch, book, pipeline_row, snapshot)
        ctx.progress(4, 6, phase="registering")
        video = upsert_patch_video(ctx.conn, book_id=book.id, patch_id=patch_id,
                                   file_path=output, resolution=book.video_resolution)
        ctx.conn.execute(
            """UPDATE patch_pipeline SET stage='upload', video_status='done', video_path=?,
               video_id=?, last_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE patch_id=?""",
            (output, video["id"], patch_id),
        )
        ctx.conn.commit()
    else:
        output = (recovery_pipeline["video_path"] if recovery_pipeline else None) or str(
            Path(settings.data_root) / "books" / str(book.id) / "patch_videos" / f"{patch_id}.mp4"
        )
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        if recovery_pipeline:
            _account_render_attempt(ctx, patch_id, recovery_pipeline, recovery_upload_id)
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
            expected = _expected_video(patch.audio_path, render_config or {})
            validator = partial(validate_video, expected=expected)
            with ctx.keep_alive():
                try:
                    result = publish_validated_video(
                        output,
                        lambda temp: video_gen.generate_standalone_video(
                            patch.audio_path, image, temp, **render_config),
                        validator=validator,
                    )
                except VideoValidationError as exc:
                    _persist_validation_report(ctx, patch_id, exc.result, expected)
                    raise
            _persist_validation_report(ctx, patch_id, result, expected)
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
            render_config = {**common, "intro_audio": intro, "outro_audio": outro}
            expected = _expected_video(patch.audio_path, render_config)
            validator = partial(validate_video, expected=expected)
            with ctx.keep_alive():
                try:
                    result = publish_validated_video(output, render, validator=validator)
                except VideoValidationError as exc:
                    _persist_validation_report(ctx, patch_id, exc.result, expected)
                    raise
            _persist_validation_report(ctx, patch_id, result, expected)

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