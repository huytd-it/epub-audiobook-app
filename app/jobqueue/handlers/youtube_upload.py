"""Upload one video to YouTube through the job queue.

Nhật ký của job này cố ý dày: một lần upload là bước duy nhất trong cả pipeline
mà máy mình không kiểm soát được — mạng, quota và API của YouTube đều ở phía kia.
Khi nó hỏng, thứ duy nhất còn lại để chẩn đoán là file log của job, nên ở đây ghi
đủ: file nào, nặng bao nhiêu, metadata gì được gửi lên, đi được bao nhiêu phần
trăm với tốc độ nào, và lỗi thật sự là gì (mã HTTP + reason của YouTube) chứ
không phải một dòng str(exception) cụt lủn.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

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

# Mã Winsock (10000-11999) là sự cố mạng của chính máy này — firewall/AV chặn
# socket, đứt kết nối, timeout — chứ không phải YouTube từ chối. Phải dò trước
# _FATAL_MARKERS vì thông điệp của WinError 10013 chứa nguyên chữ "forbidden"
# ("in a way forbidden by its access permissions"): khớp nhầm marker đó thì một
# lần bị chặn nhất thời sẽ đóng dấu "chí tử" và job không bao giờ chạy lại.
_WINSOCK_ERROR_RE = re.compile(r"winerror (1[01]\d{3})\b")

# Ghi một dòng tiến độ khi đã nhích đủ nhiều phần trăm HOẶC đã im lặng đủ lâu.
# File 500 MB với chunk 10 MB là 50 lần next_chunk(); ghi hết 50 dòng thì log
# thành một cột số vô nghĩa, còn chỉ ghi theo phần trăm thì một chunk chậm 5 phút
# lại trông như treo máy. Hai điều kiện cùng lúc giữ được cả hai đầu.
_LOG_PERCENT_STEP = 10.0
_LOG_SECONDS_STEP = 30.0


def _is_transient(message: str) -> bool:
    """True cho lỗi hạ tầng mạng đáng thử lại, dù chuỗi có chứa marker chí tử."""
    return bool(_WINSOCK_ERROR_RE.search((message or "").lower()))


def _is_fatal(message: str) -> bool:
    lowered = (message or "").lower()
    if _is_transient(lowered):
        return False
    return any(marker in lowered for marker in _FATAL_MARKERS)


def human_bytes(size: float | None) -> str:
    if not size or size <= 0:
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def human_duration(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "?"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _describe_target(upload) -> list[str]:
    """Các dòng mô tả cái sắp được tải lên, ghi trước khi chạm mạng."""
    row = dict(upload)
    path = Path(row.get("video_path") or "")
    try:
        size = path.stat().st_size
    except OSError:
        size = None
    tags = row.get("tags") or ""
    try:
        # Cột tags là JSON khi đi qua enqueue_upload, nhưng vài đường ghi cũ để lại
        # chuỗi "a,b" — đọc được cả hai chứ đừng để log chết vì một dấu phẩy.
        parsed = json.loads(tags) if tags.strip().startswith("[") else [
            t.strip() for t in tags.split(",") if t.strip()]
    except ValueError:
        parsed = [t.strip() for t in tags.split(",") if t.strip()]
    if not isinstance(parsed, list):
        parsed = []
    metadata = row.get("metadata_snapshot") or ""
    playlist = ""
    if metadata:
        try:
            config = json.loads(metadata).get("automation", {}).get("youtube", {})
            mode = config.get("playlist_mode") or config.get("mode") or ""
            if mode:
                playlist = f"{mode}:{config.get('playlist_id') or '(sẽ tạo)'}"
        except (ValueError, AttributeError):
            playlist = "(metadata_snapshot hỏng)"
    lines = [
        f"file: {path.name or '(trống)'} • {human_bytes(size) if size is not None else 'không đọc được kích thước'}",
        f"đường dẫn: {row.get('video_path')}",
        f"tiêu đề: {(row.get('title') or '')[:100]}",
        f"quyền riêng tư: {row.get('privacy_status') or 'private'}"
        f" • made_for_kids: {not bool(row.get('not_for_kids', 1))}"
        f" • mô tả: {len(row.get('description') or '')} ký tự"
        f" • {len(parsed)} tag",
    ]
    if parsed:
        lines.append(f"tags: {', '.join(parsed[:30])}")
    if playlist:
        lines.append(f"playlist: {playlist}")
    if row.get("render_source_type"):
        lines.append(
            f"nguồn: {row.get('render_source_type')}#{row.get('render_source_id')}"
            f" • video_id: {row.get('video_id')}")
    return lines


class _TransferReporter:
    """Nhận event từ youtube.process_upload rồi đổ vào ctx: tiến độ theo byte cho
    thanh % của trang Queue, dòng chữ có tiết chế cho nhật ký, và event thô cho
    cầu SSE."""

    def __init__(self, ctx, upload_id: int):
        self.ctx = ctx
        self.upload_id = upload_id
        self.bytes_total = 0
        self._last_logged_percent = -_LOG_PERCENT_STEP
        self._last_logged_elapsed = 0.0

    def __call__(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "start":
            self.bytes_total = int(event.get("bytes_total") or 0)
            self.ctx.progress(0, max(1, self.bytes_total), phase="uploading")
            self.ctx.log(
                f"truyền lên YouTube: {human_bytes(self.bytes_total)}"
                f" theo từng khối {human_bytes(event.get('chunk_size'))}")
        elif kind == "progress":
            # Event tiến độ tự lo phần emit của nó: mỗi khối một dòng @@EVENT thì
            # một file 2 GB để lại 200 dòng JSON trong đúng cái nhật ký mà người ta
            # mở ra để đọc. Đi cùng nhịp tiết chế với dòng chữ.
            self._on_progress(event)
            return
        elif kind == "retry":
            self.ctx.log(
                f"khối lỗi tạm thời ({event.get('error')}) — thử lại sau"
                f" {event.get('delay')}s, lần {event.get('attempt')}/{event.get('max_attempts')};"
                f" đã lên {human_bytes(event.get('bytes_done'))}",
                level=logging.WARNING)
        elif kind == "done":
            elapsed = event.get("elapsed") or 0
            self.ctx.progress(max(1, self.bytes_total), max(1, self.bytes_total), phase="uploading")
            self.ctx.log(
                f"truyền xong {human_bytes(event.get('bytes_total'))} trong"
                f" {human_duration(elapsed)}"
                f" ({human_bytes(event.get('speed_bps'))}/s trung bình,"
                f" {event.get('retries') or 0} lần thử lại)")
        elif kind == "error":
            status = event.get("http_status")
            reason = event.get("reason")
            head = " ".join(str(part) for part in (
                f"HTTP {status}" if status else "", reason) if part)
            self.ctx.log(
                f"truyền thất bại sau {human_duration(event.get('elapsed'))}"
                f"{(' — ' + head) if head else ''}: {event.get('error')}",
                level=logging.ERROR)
        self.ctx.emit({"stage": "youtube_upload", **event})

    def _on_progress(self, event: dict) -> None:
        percent = float(event.get("percent") or 0)
        done = int(event.get("bytes_done") or 0)
        total = int(event.get("bytes_total") or 0) or self.bytes_total
        # progress() tự tiết chế ghi DB nên gọi mỗi khối là an toàn; thanh % trên
        # trang Queue nhờ đó chạy theo byte thật thay vì đứng ở 0/1 suốt cả lần upload.
        self.ctx.progress(done, max(1, total), phase="uploading")
        elapsed = float(event.get("elapsed") or 0)
        due = (percent - self._last_logged_percent >= _LOG_PERCENT_STEP
               or elapsed - self._last_logged_elapsed >= _LOG_SECONDS_STEP)
        if not due:
            return
        self._last_logged_percent = percent
        self._last_logged_elapsed = elapsed
        speed = event.get("speed_bps") or 0
        eta = event.get("eta_seconds")
        self.ctx.log(
            f"{percent:.1f}% • {human_bytes(done)}/{human_bytes(total)}"
            f" • {human_bytes(speed)}/s"
            f" • đã {human_duration(elapsed)}"
            f" • còn ~{human_duration(eta)}")
        self.ctx.emit({"stage": "youtube_upload", **event})


def _log_validation(ctx, validation) -> None:
    facts = getattr(validation, "facts", None)
    parts = []
    if facts is not None:
        if facts.width and facts.height:
            parts.append(f"{facts.width}x{facts.height}")
        if facts.fps:
            parts.append(f"{facts.fps:g}fps")
        if facts.video_duration:
            parts.append(f"dài {human_duration(facts.video_duration)}")
        if facts.video_codec or facts.audio_codec:
            parts.append(f"{facts.video_codec or '?'}/{facts.audio_codec or '?'}")
        if facts.file_size_bytes:
            parts.append(human_bytes(facts.file_size_bytes))
    elapsed = getattr(validation, "elapsed_seconds", 0) or 0
    if elapsed:
        parts.append(f"kiểm tra mất {elapsed:.1f}s")
    ctx.log("video hợp lệ" + (f" • {' • '.join(parts)}" if parts else ""))
    for warning in getattr(validation, "warnings", ()) or ():
        ctx.log(f"cảnh báo từ kiểm tra video: {warning}", level=logging.WARNING)


def handle(ctx) -> dict:
    upload_id = ctx.job.payload.get("upload_id")
    if upload_id is None:
        raise JobFatalError("payload thiếu upload_id")
    upload = ctx.conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    if upload is None:
        raise JobFatalError(f"upload {upload_id} không tồn tại")
    video_id = upload["video_id"]

    ctx.progress(0, 1, phase="validating")
    ctx.log(f"chuẩn bị upload #{upload_id}")
    for line in _describe_target(upload):
        ctx.log(line)

    youtube.mark_validation_started(ctx.conn, upload_id)
    ctx.log("kiểm tra tính toàn vẹn của file video")
    with ctx.keep_alive():
        # Probing a large video can exceed the stale-job threshold too; without
        # a heartbeat here the reaper can hand this job to a second worker
        # while this one is still validating, causing a duplicate upload later.
        validation = validate_video(upload["video_path"])
    report = validation_report_json(validation)
    if not validation.valid:
        decision = schedule_rerender(ctx.conn, upload_id, validation, report_json=report)
        ctx.log(f"validation failed: {validation.error_code}: {validation.message}", level=logging.ERROR)
        ctx.emit({"stage": "youtube_upload", "type": "validation_failed",
                  "upload_id": upload_id, "error_code": validation.error_code,
                  "message": validation.message, "action": decision.action})
        if decision.action == "rerender":
            ctx.log(f"xếp lại lịch render (lần {decision.retry_count})", level=logging.WARNING)
            return {"rerender_scheduled": True, "retry_count": decision.retry_count}
        if decision.action == "retry_validation":
            raise RuntimeError(f"{validation.error_code}: {validation.message}")
        raise JobFatalError(decision.message)
    youtube.mark_validation_valid(ctx.conn, upload_id, report_json=report)
    _log_validation(ctx, validation)

    ctx.progress(0, 1, phase="uploading")
    if video_id:
        update_video(ctx.conn, video_id, upload_status="uploading")

    ctx.log(f"bắt đầu upload {upload_id}")
    reporter = _TransferReporter(ctx, upload_id)
    try:
        # YouTube transfers can exceed the stale-job threshold. Keep ownership
        # alive so the reaper cannot hand this upload to a second worker.
        with ctx.keep_alive():
            result = youtube.process_upload(ctx.conn, upload_id, progress_cb=reporter)
    except JobFatalError:
        raise
    except youtube.UploadInProgress as exc:
        # A different worker already owns the real transfer for this upload_id
        # (this job was reaped and re-claimed while still genuinely running).
        # Don't mark the upload/video failed - that would stomp on the state
        # of the transfer that's actually in flight. Just retry later; by
        # then the row will read 'done' (or the other worker's own failure).
        ctx.log(str(exc), level=logging.WARNING)
        raise RuntimeError(str(exc)) from exc
    except Exception as exc:
        error = youtube.describe_error(exc)
        ctx.log(f"upload {upload_id} ném lỗi: {error}", level=logging.ERROR)
        logger.exception("upload %s ném lỗi ngoài dự kiến", upload_id)
        youtube.mark_upload_failed(ctx.conn, upload_id, error)
        if video_id:
            update_video(ctx.conn, video_id, upload_status="failed", error_message=error)
        if _is_fatal(error):
            ctx.log("lỗi chí tử — không thử lại job này", level=logging.ERROR)
            raise JobFatalError(error) from exc
        raise

    if result.get("status") != "done":
        error = result.get("error") or result.get("status") or "upload thất bại"
        youtube.mark_upload_failed(ctx.conn, upload_id, error)
        if video_id:
            update_video(ctx.conn, video_id, upload_status="failed", error_message=error)
        ctx.log(f"upload {upload_id} thất bại: {error}", level=logging.ERROR)
        if _is_fatal(error):
            ctx.log("lỗi chí tử — không thử lại job này", level=logging.ERROR)
            raise JobFatalError(error)
        raise RuntimeError(error)

    youtube_video_id = result.get("youtube_video_id", "")
    youtube.mark_upload_done(ctx.conn, upload_id, youtube_video_id)
    ctx.log(f"video đã lên kênh: https://youtu.be/{youtube_video_id}")
    ctx.progress(1, 1, phase="publishing")

    try:
        ctx.log("hậu xử lý: đặt thumbnail và thêm vào playlist")
        postprocess = youtube.publish_completed_upload(ctx.conn, upload_id)
        if video_id:
            update_video(
                ctx.conn, video_id, upload_status="uploaded", youtube_video_id=youtube_video_id,
            )
        sync_pipeline_from_upload(ctx.conn, upload_id)
    except Exception as exc:
        error = youtube.describe_error(exc)
        ctx.log(f"hậu xử lý thất bại: {error}", level=logging.ERROR)
        logger.exception("hậu xử lý upload %s thất bại", upload_id)
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
    else:
        final = ctx.conn.execute(
            "SELECT thumbnail_status, playlist_status, playlist_id FROM youtube_uploads WHERE id=?",
            (upload_id,),
        ).fetchone()
        if final is not None:
            ctx.log(
                f"hậu xử lý xong • thumbnail: {final['thumbnail_status']}"
                f" • playlist: {final['playlist_status']}"
                f"{' (' + final['playlist_id'] + ')' if final['playlist_id'] else ''}")

    ctx.log(f"upload {upload_id} xong -> {youtube_video_id}")
    ctx.emit({"stage": "youtube_upload", "type": "finished", "upload_id": upload_id,
              "youtube_video_id": youtube_video_id})
    return {"youtube_video_id": youtube_video_id}
