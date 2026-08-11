"""Idempotent adapters used by custom per-patch flows."""
from pathlib import Path

from app import repository
from app.jobqueue.models import JobFatalError
from app.jobqueue.handlers import patch_video, audiobook_tts, youtube_upload
from app.patch_publishing import run_patch_publish_stage, seed_patch_video


def audio(ctx):
    patch = repository.get_patch(ctx.conn, ctx.job.payload.get("patch_id"))
    if patch is None:
        raise JobFatalError("patch does not exist")
    if patch.audio_path and Path(patch.audio_path).is_file():
        ctx.progress(1, 1, phase="existing")
        ctx.log(f"audio already exists -> {patch.audio_path}")
        return {"audio_path": patch.audio_path, "skipped": True}
    return audiobook_tts.handle(ctx)


def video(ctx):
    patch_id = ctx.job.payload.get("patch_id")
    row = ctx.conn.execute(
        "SELECT * FROM videos WHERE patch_id=? ORDER BY id DESC LIMIT 1", (patch_id,)
    ).fetchone()
    if row is not None and Path(row["file_path"]).is_file():
        ctx.progress(1, 1, phase="existing")
        ctx.log(f"video already exists -> {row['file_path']}")
        return {"output_path": row["file_path"], "video_id": row["id"], "skipped": True}
    return patch_video.handle(ctx)


def youtube(ctx):
    patch_id = ctx.job.payload.get("patch_id")
    video = ctx.conn.execute(
        "SELECT * FROM videos WHERE patch_id=? ORDER BY id DESC LIMIT 1", (patch_id,)
    ).fetchone()
    if video is None or not Path(video["file_path"]).is_file():
        raise JobFatalError("patch video is missing")
    if video["youtube_video_id"]:
        ctx.progress(1, 1, phase="existing")
        ctx.log(f"YouTube video already exists -> {video['youtube_video_id']}")
        return {"youtube_video_id": video["youtube_video_id"], "skipped": True}
    seed_patch_video(ctx.conn, patch_id, video["id"], video["file_path"])
    state = run_patch_publish_stage(ctx.conn, patch_id)
    upload_id = state.get("youtube_upload_id")
    if not upload_id:
        raise JobFatalError("could not prepare YouTube upload")
    privacy = ctx.job.payload.get("privacy", "private")
    ctx.conn.execute(
        "UPDATE youtube_uploads SET privacy_status=? WHERE id=?", (privacy, upload_id)
    )
    ctx.conn.commit()
    ctx.job.payload_json = __import__("json").dumps({"upload_id": upload_id})
    return youtube_upload.handle(ctx)
