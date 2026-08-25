"""Re-render a standalone video from persisted application-owned inputs."""
from __future__ import annotations

import json
from pathlib import Path

from app import video_gen
from app.config import settings
from app.image_overlay import render_branding_overlay
from app.jobqueue.models import JobFatalError
from app.production_defaults import get_effective_branding_config
from app.video_integrity import validate_video
from app.video_publish import publish_validated_video
from app.video_recovery import resume_upload_after_render
from app.video_repository import get_video, update_video


def handle(ctx) -> dict:
    video_id = ctx.job.payload.get("video_id")
    if video_id is None:
        raise JobFatalError("payload missing video_id")
    video = get_video(ctx.conn, video_id)
    if not video:
        raise JobFatalError(f"video {video_id} not found")
    audio = video.get("source_audio")
    background = video.get("background_path")
    try:
        config = json.loads(video.get("render_config_json") or "")
    except json.JSONDecodeError as exc:
        raise JobFatalError(f"source_unavailable: invalid render config: {exc}") from exc
    for label, path in (("audio", audio), ("background", background)):
        if not path or not Path(path).is_file():
            raise JobFatalError(f"source_unavailable: {label} missing: {path}")
    if not isinstance(config, dict) or not config:
        raise JobFatalError("source_unavailable: render config missing")
    output = video["file_path"]

    # Resolve branding overlay for the video target.
    branding_overlay_path = None
    try:
        from app import repository
        patch_id = video.get("patch_id")
        book_id = video.get("book_id")
        if patch_id:
            patch = repository.get_patch(ctx.conn, int(patch_id))
            if patch:
                book = repository.get_book(ctx.conn, patch.book_id)
                if book:
                    branding = get_effective_branding_config(ctx.conn, book)
                    w, h = (book.video_resolution or "1920x1080").split("x")
                    overlay_img = render_branding_overlay((int(w), int(h)), branding, target="video")
                    if overlay_img is not None:
                        _bo_dir = Path(settings.data_root) / "books" / str(book.id) / ".branding"
                        _bo_dir.mkdir(parents=True, exist_ok=True)
                        branding_overlay_path = str(_bo_dir / f"branding_standalone_{video_id}.png")
                        overlay_img.save(branding_overlay_path, "PNG")
        elif book_id:
            book = repository.get_book(ctx.conn, int(book_id))
            if book:
                branding = get_effective_branding_config(ctx.conn, book)
                w, h = (book.video_resolution or "1920x1080").split("x")
                overlay_img = render_branding_overlay((int(w), int(h)), branding, target="video")
                if overlay_img is not None:
                    _bo_dir = Path(settings.data_root) / "books" / str(book.id) / ".branding"
                    _bo_dir.mkdir(parents=True, exist_ok=True)
                    branding_overlay_path = str(_bo_dir / f"branding_standalone_{video_id}.png")
                    overlay_img.save(branding_overlay_path, "PNG")
    except Exception:  # noqa: BLE001 — branding is best-effort for standalone re-render
        pass

    ctx.progress(0, 1, phase="encoding")
    publish_validated_video(
        output,
        lambda temp: video_gen.generate_standalone_video(audio, background, temp, **config,
                                                         branding_overlay_path=branding_overlay_path),
        validator=validate_video,
    )
    update_video(ctx.conn, video_id, file_path=output, upload_status="queued",
                 error_message=None)
    recovery_upload_id = ctx.job.payload.get("recovery_upload_id")
    if recovery_upload_id is not None:
        resume_upload_after_render(ctx.conn, recovery_upload_id)
    ctx.progress(1, 1, phase="done")
    return {"output_path": output}
