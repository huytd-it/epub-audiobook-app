"""Upload one video to YouTube through the job queue."""
from __future__ import annotations

import logging

from app import youtube
from app.jobqueue.models import JobFatalError
from app.patch_publishing import sync_pipeline_from_upload
from app.video_repository import update_video
from app.video_integrity import validate_video, validation_report_json
from app.video_recovery import schedule_rerender

logger = logging.getLogger(__name__)

_FATAL_MARKERS = (
    "quotaexceeded", "dailylimitexceeded", "uploadlimitexceeded",
    "forbidden", "filenotfounderror", "no such file",
    "invalid_grant", "unauthorized",
)


def _is_fatal(message: str) -> bool:
    lowered = (message or "").lower()
    return any(marker in lowered for marker in _FATAL_MARKERS)


def handle(ctx) -> dict:
    upload_id = ctx.job.payload.get("upload_id")
    if upload_id is None:
        raise JobFatalError("payload thiếu upload_id")
    upload = ctx.conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    if upload is None:
        raise JobFatalError(f"upload {upload_id} không tồn tại")
    video_id = upload["video_id"]

    ctx.progress(0, 1, phase="validating")
    youtube.mark_validation_started(ctx.conn, upload_id)
    validation = validate_video(upload["video_path"])
    report = validation_report_json(validation)
    if not validation.valid:
        decision = schedule_rerender(ctx.conn, upload_id, validation, report_json=report)
        ctx.log(f"validation failed: {validation.error_code}: {validation.message}", level=logging.ERROR)
        if decision.action == "rerender":
            return {"rerender_scheduled": True, "retry_count": decision.retry_count}
        if decision.action == "retry_validation":
            raise RuntimeError(f"{validation.error_code}: {validation.message}")
        raise JobFatalError(decision.message)
    youtube.mark_validation_valid(ctx.conn, upload_id, report_json=report)

    ctx.progress(0, 1, phase="uploading")
    if video_id:
        update_video(ctx.conn, video_id, upload_status="uploading")

    ctx.log(f"bắt đầu upload {upload_id}")
    try:
        result = youtube.process_upload(ctx.conn, upload_id)
    except JobFatalError:
        raise
    except Exception as exc:
        error = str(exc)
        youtube.mark_upload_failed(ctx.conn, upload_id, error)
        if video_id:
            update_video(ctx.conn, video_id, upload_status="failed", error_message=error)
        if _is_fatal(error):
            raise JobFatalError(error) from exc
        raise

    if result.get("status") != "done":
        error = result.get("error") or result.get("status") or "upload thất bại"
        youtube.mark_upload_failed(ctx.conn, upload_id, error)
        if video_id:
            update_video(ctx.conn, video_id, upload_status="failed", error_message=error)
        ctx.log(f"upload {upload_id} thất bại: {error}", level=logging.ERROR)
        if _is_fatal(error):
            raise JobFatalError(error)
        raise RuntimeError(error)

    youtube_video_id = result.get("youtube_video_id", "")
    youtube.mark_upload_done(ctx.conn, upload_id, youtube_video_id)
    ctx.progress(1, 1, phase="publishing")

    try:
        postprocess = youtube.publish_completed_upload(ctx.conn, upload_id)
        if video_id:
            update_video(
                ctx.conn, video_id, upload_status="uploaded", youtube_video_id=youtube_video_id,
            )
        sync_pipeline_from_upload(ctx.conn, upload_id)
    except Exception as exc:
        error = str(exc)
        youtube.mark_upload_failed(ctx.conn, upload_id, error)
        if video_id:
            update_video(ctx.conn, video_id, upload_status="failed", error_message=error)
        if _is_fatal(error):
            raise JobFatalError(error) from exc
        raise

    status = (postprocess or {}).get("status")
    if status not in (None, "published", "done"):
        message = (postprocess or {}).get("error") or status
        row = ctx.conn.execute(
            "SELECT error_message FROM youtube_uploads WHERE id=?", (upload_id,)
        ).fetchone()
        if row is not None and not row["error_message"]:
            ctx.conn.execute(
                "UPDATE youtube_uploads SET error_message=? WHERE id=?", (message, upload_id))
            ctx.conn.commit()
        ctx.log(f"hậu xử lý chưa trọn vẹn: {message}", level=logging.WARNING)

    ctx.log(f"upload {upload_id} xong -> {youtube_video_id}")
    return {"youtube_video_id": youtube_video_id}
