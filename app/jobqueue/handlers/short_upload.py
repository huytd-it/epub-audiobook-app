"""Upload 1 short lên 1 platform (fb|tiktok|youtube) qua job queue."""
from __future__ import annotations

import logging

from app import shorts_repository
from app.jobqueue.models import JobFatalError

logger = logging.getLogger(__name__)

_FATAL_MARKERS = ("quotaexceeded", "forbidden", "filenotfounderror",
                  "no such file", "invalid_grant", "unauthorized")


def _is_fatal(message: str) -> bool:
    lowered = (message or "").lower()
    return any(m in lowered for m in _FATAL_MARKERS)


def handle(ctx) -> dict:
    short_id = ctx.job.payload.get("short_id")
    platform = (ctx.job.payload.get("platform") or "").strip()
    if short_id is None or platform not in {"fb", "tiktok", "youtube"}:
        raise JobFatalError("payload cần short_id + platform fb|tiktok|youtube")
    short = shorts_repository.get_short(ctx.conn, int(short_id))
    if short is None:
        raise JobFatalError(f"short {short_id} không tồn tại")
    if not short.video_path:
        raise JobFatalError("short chưa render xong (thiếu video_path)")

    # Tài khoản đăng: payload của job thắng, rồi tới lựa chọn lưu trên short_uploads;
    # None = tài khoản mặc định của mạng đó.
    account_id = ctx.job.payload.get("account_id")
    if account_id is None:
        upload_row = shorts_repository.get_upload(ctx.conn, short.id, platform)
        account_id = upload_row.account_id if upload_row else None

    ctx.progress(0, 1, phase=f"uploading:{platform}")
    shorts_repository.set_upload_status(ctx.conn, short.id, platform, "processing")
    try:
        if platform == "fb":
            from app import facebook
            platform_video_id = facebook.publish_short_video(
                ctx.conn, short.video_path, short.caption, short.story_link,
                account_id=account_id)
        elif platform == "tiktok":
            from app import tiktok
            platform_video_id = tiktok.publish_short_video(
                ctx.conn, short.video_path, short.caption, short.story_link,
                account_id=account_id)
        else:
            from app import youtube
            # Dọc <=3min tự thành Shorts; privacy mặc định draft/private.
            upload_id = youtube.enqueue_upload(
                ctx.conn, short.video_path,
                title=(short.caption or f"Short #{short.id}")[:100],
                description=f"{short.caption}\n{short.story_link}".strip()[:5000],
                tags=["shorts", "sach noi"],
                privacy_status="private",
                render_source_type="external", render_source_id=short.id,
                account_id=account_id,
            )
            result = youtube.process_upload(ctx.conn, upload_id)
            if result.get("status") != "done":
                raise RuntimeError(result.get("error") or "youtube upload thất bại")
            platform_video_id = result.get("youtube_video_id") or ""
        shorts_repository.set_upload_status(
            ctx.conn, short.id, platform, "done",
            platform_video_id=platform_video_id)
    except JobFatalError:
        raise
    except Exception as exc:
        err = str(exc)[:1900]
        shorts_repository.set_upload_status(
            ctx.conn, short.id, platform, "failed", error_message=err)
        if _is_fatal(err):
            raise JobFatalError(err) from exc
        raise RuntimeError(err) from exc
    ctx.progress(1, 1, phase="done")
    # Short coi là published khi cả 3 kênh done (route publish kiểm tra).
    remaining = [u for u in shorts_repository.list_uploads(ctx.conn, short.id)
                 if u.status != "done"]
    if not remaining:
        shorts_repository.update_short(ctx.conn, short.id, status="published")
    return {"platform": platform, "platform_video_id": platform_video_id}
