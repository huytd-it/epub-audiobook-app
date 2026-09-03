"""Nhật ký và tiến độ của một lần upload YouTube.

Cái được kiểm ở đây là thứ người dùng đọc khi upload hỏng lúc 2 giờ sáng: file
log của job có nói được file nào, đi tới đâu, nhanh chậm ra sao, và lỗi thật sự
là gì — hay chỉ có đúng một dòng "bắt đầu upload 7".
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import db, youtube
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.joblog import JobLogger, tail
from app.jobqueue.handlers import youtube_upload as handler
from app.video_integrity import ValidationFacts, ValidationResult


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(youtube, "BASE_RETRY_DELAY", 0.0)
    yield


class _Status:
    """Cái googleapiclient trả về giữa chừng một lần upload resumable."""

    def __init__(self, fraction, resumable_progress=None):
        self._fraction = fraction
        if resumable_progress is not None:
            self.resumable_progress = resumable_progress

    def progress(self):
        return self._fraction


class FakeChunkedService:
    """Mỗi phần tử của `script` là một lượt next_chunk(): một _Status, một
    Exception để ném, hoặc None nghĩa là kết thúc và trả response."""

    def __init__(self, script, video_id="vid123"):
        self.script = list(script)
        self.video_id = video_id
        self.calls = 0

    def videos(self):
        return self

    def insert(self, *args, **kwargs):
        return self

    def next_chunk(self):
        self.calls += 1
        if not self.script:
            return None, {"id": self.video_id}
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step, None


def _prepare(conn, tmp_path, monkeypatch, *, size=4 * 1024 * 1024):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x" * size)
    monkeypatch.setattr(youtube, "MediaFileUpload", lambda *a, **kw: object(), raising=False)
    return youtube.enqueue_upload(conn, str(video), "Tiêu đề", tags=["a", "b"])


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "logging.db"))
    db.init_schema(conn)
    return conn


# --------------------------------------------------------------- describe_error


class _Resp:
    def __init__(self, status):
        self.status = status


class _HttpishError(Exception):
    def __init__(self, status, content):
        super().__init__("<HttpError 403 when requesting https://... returned ...>")
        self.resp = _Resp(status)
        self.content = content


def test_describe_error_keeps_status_reason_and_message_on_one_line():
    exc = _HttpishError(403, b'{"error":{"message":"The user has exceeded the number of'
                             b' videos they may upload.","errors":[{"reason":"uploadLimitExceeded"}]}}')

    described = youtube.describe_error(exc)

    assert described.startswith("HTTP 403 uploadLimitExceeded: ")
    assert "exceeded the number of videos" in described
    # Phân loại lỗi chí tử của handler dò trên chính chuỗi này.
    assert handler._is_fatal(described)


def test_describe_error_names_the_class_for_plain_exceptions():
    assert youtube.describe_error(ValueError("mạng lỗi")) == "ValueError: mạng lỗi"


def _winsock_10013() -> PermissionError:
    """Cái Windows ném ra khi firewall/AV chặn socket đi ra."""
    exc = PermissionError(
        "[WinError 10013] An attempt was made to access a socket in a way"
        " forbidden by its access permissions"
    )
    exc.winerror = 10013
    return exc


def test_winsock_permission_error_is_not_classified_fatal():
    # Thông điệp của WinError 10013 chứa nguyên chữ "forbidden"; khớp nhầm marker
    # đó thì một lần bị chặn nhất thời đóng dấu chí tử và job không chạy lại nữa.
    described = youtube.describe_error(_winsock_10013())

    assert "forbidden" in described.lower()
    assert handler._is_transient(described)
    assert not handler._is_fatal(described)


def test_http_403_forbidden_stays_fatal():
    exc = _HttpishError(403, b'{"error":{"errors":[{"reason":"forbidden"}],'
                             b'"message":"The request is not properly authorized."}}')

    assert handler._is_fatal(youtube.describe_error(exc))


def test_winsock_permission_error_is_a_retryable_transfer():
    # PermissionError của filesystem vẫn là lỗi vĩnh viễn; chỉ socket mới retry.
    assert youtube._is_retryable_transfer(_winsock_10013())
    assert not youtube._is_retryable_transfer(PermissionError("file dang bi khoa"))


# --------------------------------------------------------------- process_upload


def test_process_upload_reports_bytes_and_speed_for_each_chunk(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    upload_id = _prepare(conn, tmp_path, monkeypatch, size=1000)
    monkeypatch.setattr(youtube, "get_youtube_service",
                        lambda c: FakeChunkedService([_Status(0.5, 500), _Status(1.0, 1000)]))
    events = []

    youtube.process_upload(conn, upload_id, progress_cb=events.append)

    kinds = [event["type"] for event in events]
    assert kinds == ["start", "progress", "progress", "done"]
    start = events[0]
    assert start["bytes_total"] == 1000 and start["file_name"] == "v.mp4"
    assert start["title"] == "Tiêu đề" and start["tags"] == ["a", "b"]
    assert [event["bytes_done"] for event in events[1:3]] == [500, 1000]
    assert [event["percent"] for event in events[1:3]] == [50.0, 100.0]
    assert all(event["speed_bps"] >= 0 for event in events[1:3])
    assert events[-1]["youtube_video_id"] == "vid123"


def test_progress_falls_back_to_the_fraction_when_the_api_omits_bytes(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    upload_id = _prepare(conn, tmp_path, monkeypatch, size=1000)
    monkeypatch.setattr(youtube, "get_youtube_service",
                        lambda c: FakeChunkedService([_Status(0.25)]))
    events = []

    youtube.process_upload(conn, upload_id, progress_cb=events.append)

    assert [event["bytes_done"] for event in events if event["type"] == "progress"] == [250]


def test_a_dropped_connection_resumes_instead_of_failing_the_upload(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    upload_id = _prepare(conn, tmp_path, monkeypatch, size=1000)
    service = FakeChunkedService([_Status(0.5, 500), ConnectionResetError("bị ngắt"), _Status(1.0, 1000)])
    monkeypatch.setattr(youtube, "get_youtube_service", lambda c: service)
    events = []

    result = youtube.process_upload(conn, upload_id, progress_cb=events.append)

    assert result["status"] == "done"
    retry = [event for event in events if event["type"] == "retry"]
    assert len(retry) == 1
    assert retry[0]["attempt"] == 1 and retry[0]["bytes_done"] == 500
    assert "ConnectionResetError" in retry[0]["error"]
    # Truyền tiếp từ chỗ dở: không có lần insert() thứ hai nào cả.
    assert events[-1]["type"] == "done" and events[-1]["retries"] == 1


def test_scattered_blips_across_a_long_transfer_do_not_exhaust_the_retry_budget(tmp_path, monkeypatch):
    """5 lần rớt mạng rải rác trong một file lớn không được giết cả lần upload —
    chỉ 5 lần hỏng LIÊN TIẾP mới đáng bỏ cuộc."""
    conn = _conn(tmp_path)
    upload_id = _prepare(conn, tmp_path, monkeypatch, size=1000)
    script = []
    for step in range(1, 6):
        script += [ConnectionResetError(f"blip {step}"), _Status(step / 10, step * 100)]
    monkeypatch.setattr(youtube, "get_youtube_service", lambda c: FakeChunkedService(script))
    events = []

    result = youtube.process_upload(conn, upload_id, progress_cb=events.append)

    assert result["status"] == "done"
    assert len([event for event in events if event["type"] == "retry"]) == 5


def test_a_flapping_connection_gives_up_at_the_total_retry_ceiling(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    upload_id = _prepare(conn, tmp_path, monkeypatch, size=1000)
    script = []
    for step in range(60):
        script += [ConnectionResetError("chập chờn"), _Status(0.5, 500)]
    monkeypatch.setattr(youtube, "get_youtube_service", lambda c: FakeChunkedService(script))
    events = []

    result = youtube.process_upload(conn, upload_id, progress_cb=events.append)

    assert result["status"] == "failed"
    assert len([event for event in events if event["type"] == "retry"]) == youtube.MAX_TRANSFER_RETRIES - 1


def test_a_missing_file_is_not_retried_as_a_network_blip(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    upload_id = _prepare(conn, tmp_path, monkeypatch, size=1000)
    service = FakeChunkedService([FileNotFoundError("file bay mất")])
    monkeypatch.setattr(youtube, "get_youtube_service", lambda c: service)
    events = []

    result = youtube.process_upload(conn, upload_id, progress_cb=events.append)

    assert result["status"] == "failed"
    assert [event["type"] for event in events if event["type"] == "retry"] == []
    assert service.calls == 1


def test_a_failed_upload_persists_the_described_error(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    upload_id = _prepare(conn, tmp_path, monkeypatch, size=1000)
    exc = _HttpishError(403, b'{"error":{"message":"quota gone","errors":[{"reason":"quotaExceeded"}]}}')
    monkeypatch.setattr(youtube, "get_youtube_service",
                        lambda c: FakeChunkedService([exc]))
    events = []

    result = youtube.process_upload(conn, upload_id, progress_cb=events.append)

    assert result["status"] == "failed"
    stored = conn.execute(
        "SELECT status, error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert stored["status"] == "failed"
    assert stored["error_message"].startswith("HTTP 403 quotaExceeded: quota gone")
    error_event = events[-1]
    assert error_event["type"] == "error"
    assert error_event["http_status"] == 403 and error_event["reason"] == "quotaExceeded"


def test_a_broken_progress_callback_never_fails_the_upload(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    upload_id = _prepare(conn, tmp_path, monkeypatch, size=1000)
    monkeypatch.setattr(youtube, "get_youtube_service",
                        lambda c: FakeChunkedService([_Status(0.5, 500)]))

    def explode(event):
        raise RuntimeError("logger hỏng")

    assert youtube.process_upload(conn, upload_id, progress_cb=explode)["status"] == "done"


# ------------------------------------------------------------------- job log


def _upload_row(conn, video_path):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO youtube_uploads (video_path, title, description, tags, privacy_status,
                                         status, not_for_kids, metadata_snapshot, created_at)
           VALUES (?, 'Chương 1-3', 'mô tả', '["audiobook","truyện"]', 'unlisted',
                   'pending', 1, '{"automation":{"youtube":{"playlist_mode":"existing","playlist_id":"PL9"}}}', ?)""",
        (video_path, now))
    conn.commit()
    return cur.lastrowid


def _ctx(conn, upload_id):
    job_id = store.enqueue(conn, "youtube_upload", payload={"upload_id": upload_id})
    job = store.claim(conn, "youtube_upload", "w1")
    return JobContext(job, conn, JobLogger(job_id, "youtube_upload"), lambda: False,
                      flush_interval=0.0), job_id


def _run_handler(conn, tmp_path, monkeypatch, *, script, size=4000):
    video = tmp_path / "tap-01.mp4"
    video.write_bytes(b"x" * size)
    upload_id = _upload_row(conn, str(video))
    monkeypatch.setattr(handler, "validate_video", lambda path: ValidationResult(
        True, None, "", (), ValidationFacts(video_codec="h264", audio_codec="aac",
                                            width=1920, height=1080, fps=24.0,
                                            video_duration=930.0, file_size_bytes=size), 1.5))
    monkeypatch.setattr(youtube, "MediaFileUpload", lambda *a, **kw: object(), raising=False)
    monkeypatch.setattr(youtube, "get_youtube_service", lambda c: FakeChunkedService(script))
    monkeypatch.setattr(handler.youtube, "publish_completed_upload",
                        lambda c, uid: {"status": "published"})
    monkeypatch.setattr(handler, "sync_pipeline_from_upload", lambda c, uid: None)
    ctx, job_id = _ctx(conn, upload_id)
    result = handler.handle(ctx)
    ctx.close()
    return result, tail(job_id), upload_id


def test_job_log_describes_what_is_being_uploaded(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _, log, _ = _run_handler(conn, tmp_path, monkeypatch, script=[_Status(1.0, 4000)])

    assert "tap-01.mp4" in log
    assert "Chương 1-3" in log
    assert "unlisted" in log
    assert "audiobook, truyện" in log
    assert "existing:PL9" in log
    assert "1920x1080" in log and "24fps" in log
    assert "https://youtu.be/vid123" in log


def test_job_log_shows_transfer_percent_speed_and_eta(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _, log, _ = _run_handler(
        conn, tmp_path, monkeypatch,
        script=[_Status(0.25, 1000), _Status(0.5, 2000), _Status(1.0, 4000)])

    assert "25.0%" in log and "100.0%" in log
    assert "/s" in log and "còn ~" in log
    assert "truyền xong" in log


def test_job_log_records_a_resumed_chunk_as_a_warning(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    _, log, _ = _run_handler(
        conn, tmp_path, monkeypatch,
        script=[_Status(0.5, 2000), ConnectionResetError("reset by peer"), _Status(1.0, 4000)])

    assert "[WARN ]" in log
    assert "thử lại sau" in log and "ConnectionResetError" in log


def test_job_log_spells_out_a_youtube_api_failure(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    exc = _HttpishError(403, b'{"error":{"message":"quota gone","errors":[{"reason":"quotaExceeded"}]}}')
    conn_for_log = conn
    video = tmp_path / "tap-02.mp4"
    video.write_bytes(b"x" * 100)
    upload_id = _upload_row(conn_for_log, str(video))
    monkeypatch.setattr(handler, "validate_video", lambda path: ValidationResult(
        True, None, "", (), ValidationFacts(), 0))
    monkeypatch.setattr(youtube, "MediaFileUpload", lambda *a, **kw: object(), raising=False)
    monkeypatch.setattr(youtube, "get_youtube_service", lambda c: FakeChunkedService([exc]))
    ctx, job_id = _ctx(conn, upload_id)

    from app.jobqueue.models import JobFatalError
    with pytest.raises(JobFatalError):
        handler.handle(ctx)
    ctx.close()

    log = tail(job_id)
    assert "[ERROR]" in log
    assert "HTTP 403 quotaExceeded" in log and "quota gone" in log
    assert "không thử lại job này" in log


def test_queue_percent_follows_the_bytes_actually_sent(tmp_path, monkeypatch):
    conn = _conn(tmp_path)
    seen = []
    original = JobContext.progress

    def spy(self, current, total=None, phase=None):
        seen.append((current, total, phase))
        return original(self, current, total, phase)

    monkeypatch.setattr(JobContext, "progress", spy)
    _run_handler(conn, tmp_path, monkeypatch,
                 script=[_Status(0.25, 1000), _Status(1.0, 4000)])

    uploading = [(current, total) for current, total, phase in seen if phase == "uploading"]
    assert (1000, 4000) in uploading and (4000, 4000) in uploading
