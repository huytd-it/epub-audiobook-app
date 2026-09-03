"""Handler youtube_upload: thành công, thất bại, quota là lỗi fatal."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import db
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.joblog import JobLogger
from app.jobqueue.handlers import youtube_upload as handler
from app.jobqueue.models import JobFatalError
from app.video_integrity import ValidationFacts, ValidationResult


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(handler, "validate_video", lambda path: ValidationResult(
        True, None, "", (), ValidationFacts(), 0))
    yield


def _upload_row(conn, *, video_id=None):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO youtube_uploads (video_id, video_path, title, description, tags,
                                         privacy_status, status, created_at)
           VALUES (?, '/tmp/v.mp4', 'T', 'D', 'a,b', 'private', 'pending', ?)""",
        (video_id, now))
    conn.commit()
    return cur.lastrowid


def _ctx(conn, upload_id, video_id=None):
    payload = {"upload_id": upload_id}
    if video_id is not None:
        payload["video_id"] = video_id
    job_id = store.enqueue(conn, "youtube_upload", payload=payload)
    job = store.claim(conn, "youtube_upload", "youtube_upload#1")
    return JobContext(job, conn, JobLogger(job_id, "youtube_upload"), lambda: False), job_id


def test_missing_upload_id_is_fatal(tmp_path):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    job_id = store.enqueue(conn, "youtube_upload", payload={})
    job = store.claim(conn, "youtube_upload", "w")
    ctx = JobContext(job, conn, JobLogger(job_id, "youtube_upload"), lambda: False)
    with pytest.raises(JobFatalError):
        handler.handle(ctx)


def test_successful_upload_returns_the_video_id(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    upload_id = _upload_row(conn)
    monkeypatch.setattr(
        handler.youtube, "process_upload",
        lambda c, uid, **kw: {"status": "done", "youtube_video_id": "abc123"})
    calls = []
    monkeypatch.setattr(handler.youtube, "publish_completed_upload", lambda c, uid, **kw: calls.append(("publish", uid)) or {"status": "published"})
    monkeypatch.setattr(handler, "sync_pipeline_from_upload", lambda c, uid, **kw: calls.append(("sync", uid)))
    ctx, _ = _ctx(conn, upload_id)

    assert handler.handle(ctx) == {"youtube_video_id": "abc123"}
    assert calls == [("publish", upload_id), ("sync", upload_id)]
    row = conn.execute("SELECT status, youtube_video_id FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert dict(row) == {"status": "done", "youtube_video_id": "abc123"}


def test_youtube_transfer_runs_with_keep_alive(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    upload_id = _upload_row(conn)
    ctx, _ = _ctx(conn, upload_id)
    calls = []

    class KeepAlive:
        def __enter__(self):
            calls.append("enter")

        def __exit__(self, *args):
            calls.append("exit")

    monkeypatch.setattr(ctx, "keep_alive", lambda: KeepAlive())
    monkeypatch.setattr(
        handler.youtube, "process_upload",
        lambda *args, **kw: calls.append("upload") or {"status": "done", "youtube_video_id": "yt"},
    )
    monkeypatch.setattr(handler.youtube, "publish_completed_upload", lambda *args, **kw: {"status": "published"})
    monkeypatch.setattr(handler, "sync_pipeline_from_upload", lambda *args, **kw: None)

    handler.handle(ctx)

    assert calls == ["enter", "exit", "enter", "upload", "exit"]


def test_validation_finishes_before_youtube_transfer(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db")); db.init_schema(conn)
    upload_id = _upload_row(conn); calls = []
    monkeypatch.setattr(handler, "validate_video", lambda path: calls.append("validate") or ValidationResult(True, None, "", (), ValidationFacts(), 0))
    monkeypatch.setattr(handler.youtube, "process_upload", lambda c, uid, **kw: calls.append(c.execute("SELECT validation_status FROM youtube_uploads WHERE id=?", (uid,)).fetchone()[0]) or {"status": "done", "youtube_video_id": "yt"})
    monkeypatch.setattr(handler.youtube, "publish_completed_upload", lambda *a, **kw: {"status": "published"})
    monkeypatch.setattr(handler, "sync_pipeline_from_upload", lambda *a, **kw: None)
    handler.handle(_ctx(conn, upload_id)[0])
    assert calls == ["validate", "valid"]


def test_invalid_external_video_never_calls_youtube(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db")); db.init_schema(conn)
    upload_id = _upload_row(conn); calls = []
    monkeypatch.setattr(handler, "validate_video", lambda path: ValidationResult(False, "decode_failed", "broken", (), ValidationFacts(), 0))
    monkeypatch.setattr(handler.youtube, "process_upload", lambda *a, **kw: calls.append("upload"))
    with pytest.raises(JobFatalError, match="external"):
        handler.handle(_ctx(conn, upload_id)[0])
    assert calls == []


def test_upload_in_progress_elsewhere_retries_without_marking_failed(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    video_id = conn.execute(
        "INSERT INTO videos (filename, file_path, upload_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("v.mp4", "/tmp/v.mp4", "queued", now, now),
    ).lastrowid
    upload_id = _upload_row(conn, video_id=video_id)
    conn.execute("UPDATE youtube_uploads SET status='uploading' WHERE id=?", (upload_id,))
    conn.commit()
    monkeypatch.setattr(
        handler.youtube, "process_upload",
        lambda c, uid, **kw: (_ for _ in ()).throw(handler.youtube.UploadInProgress("busy")))
    ctx, _ = _ctx(conn, upload_id, video_id=video_id)

    with pytest.raises(RuntimeError, match="busy"):
        handler.handle(ctx)

    upload = conn.execute("SELECT status, error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    video = conn.execute("SELECT upload_status, error_message FROM videos WHERE id=?", (video_id,)).fetchone()
    assert upload["status"] == "uploading"
    assert upload["error_message"] is None
    assert video["upload_status"] == "uploading"
    assert video["error_message"] is None


def test_a_failed_transfer_marks_the_upload_and_raises(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    upload_id = _upload_row(conn)
    monkeypatch.setattr(
        handler.youtube, "process_upload",
        lambda c, uid, **kw: {"status": "failed", "error": "mạng lỗi"})
    ctx, _ = _ctx(conn, upload_id)
    with pytest.raises(RuntimeError, match="mạng lỗi"):
        handler.handle(ctx)
    row = conn.execute(
        "SELECT status, error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert row["status"] == "failed"


def test_unexpected_transfer_exception_marks_upload_and_video_failed(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    video_id = conn.execute(
        "INSERT INTO videos (filename, file_path, upload_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("v.mp4", "/tmp/v.mp4", "queued", now, now),
    ).lastrowid
    upload_id = _upload_row(conn, video_id=video_id)
    conn.commit()
    monkeypatch.setattr(handler.youtube, "process_upload", lambda c, uid, **kw: (_ for _ in ()).throw(ValueError("unexpected transfer")))
    ctx, _ = _ctx(conn, upload_id, video_id=video_id)

    with pytest.raises(ValueError, match="unexpected transfer"):
        handler.handle(ctx)

    upload = conn.execute("SELECT status, error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    video = conn.execute("SELECT upload_status, error_message FROM videos WHERE id=?", (video_id,)).fetchone()
    assert dict(upload) == {"status": "failed", "error_message": "ValueError: unexpected transfer"}
    assert dict(video) == {"upload_status": "failed", "error_message": "ValueError: unexpected transfer"}


def test_fatal_transfer_exception_marks_upload_and_raises_job_fatal_error(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    upload_id = _upload_row(conn)
    monkeypatch.setattr(handler.youtube, "process_upload", lambda c, uid, **kw: (_ for _ in ()).throw(ValueError("quotaExceeded: daily limit")))
    ctx, _ = _ctx(conn, upload_id)

    with pytest.raises(JobFatalError, match="quotaExceeded: daily limit"):
        handler.handle(ctx)

    row = conn.execute("SELECT status, error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert dict(row) == {"status": "failed", "error_message": "ValueError: quotaExceeded: daily limit"}


def test_failed_transfer_with_video_id_marks_both_rows(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    video_id = conn.execute(
        "INSERT INTO videos (filename, file_path, upload_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("v.mp4", "/tmp/v.mp4", "queued", now, now),
    ).lastrowid
    upload_id = _upload_row(conn, video_id=video_id)
    conn.commit()
    monkeypatch.setattr(handler.youtube, "process_upload", lambda c, uid, **kw: {"status": "failed", "error": "mạng lỗi"})
    ctx, _ = _ctx(conn, upload_id, video_id=video_id)

    with pytest.raises(RuntimeError, match="mạng lỗi"):
        handler.handle(ctx)

    upload = conn.execute("SELECT status, error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    video = conn.execute("SELECT upload_status, error_message FROM videos WHERE id=?", (video_id,)).fetchone()
    assert dict(upload) == {"status": "failed", "error_message": "mạng lỗi"}
    assert dict(video) == {"upload_status": "failed", "error_message": "mạng lỗi"}


def test_quota_errors_are_fatal_and_never_retried(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    upload_id = _upload_row(conn)
    monkeypatch.setattr(
        handler.youtube, "process_upload",
        lambda c, uid, **kw: {"status": "failed", "error": "quotaExceeded: daily limit"})
    ctx, _ = _ctx(conn, upload_id)
    with pytest.raises(JobFatalError):
        handler.handle(ctx)


def test_a_missing_source_file_is_fatal(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    upload_id = _upload_row(conn)
    monkeypatch.setattr(
        handler.youtube, "process_upload",
        lambda c, uid, **kw: {"status": "failed", "error": "FileNotFoundError: /tmp/v.mp4"})
    ctx, _ = _ctx(conn, upload_id)
    with pytest.raises(JobFatalError):
        handler.handle(ctx)


def test_video_row_is_updated_when_the_upload_row_carries_one(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO videos (filename, file_path, upload_status, created_at, updated_at)
           VALUES ('v.mp4', '/tmp/v.mp4', 'queued', ?, ?)""", (now, now))
    conn.commit()
    video_id = cur.lastrowid
    upload_id = _upload_row(conn, video_id=video_id)
    monkeypatch.setattr(
        handler.youtube, "process_upload",
        lambda c, uid, **kw: {"status": "done", "youtube_video_id": "xyz"})
    monkeypatch.setattr(handler.youtube, "publish_completed_upload", lambda c, uid, **kw: {"status": "published"})
    monkeypatch.setattr(handler, "sync_pipeline_from_upload", lambda c, uid, **kw: None)
    ctx, _ = _ctx(conn, upload_id, video_id=video_id)

    handler.handle(ctx)

    row = conn.execute("SELECT upload_status, youtube_video_id FROM videos WHERE id=?",
                       (video_id,)).fetchone()
    assert row["upload_status"] == "uploaded"
    assert row["youtube_video_id"] == "xyz"


def test_non_success_postprocess_records_upload_error_without_failing_video(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    upload_id = _upload_row(conn)
    monkeypatch.setattr(handler.youtube, "process_upload", lambda c, uid, **kw: {"status": "done", "youtube_video_id": "xyz"})
    monkeypatch.setattr(handler.youtube, "publish_completed_upload", lambda c, uid, **kw: {"status": "auth_required", "error": "auth"})
    monkeypatch.setattr(handler, "sync_pipeline_from_upload", lambda c, uid, **kw: None)
    ctx, _ = _ctx(conn, upload_id)

    assert handler.handle(ctx) == {"youtube_video_id": "xyz"}
    row = conn.execute("SELECT status, error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert row["status"] != "failed"
    assert row["error_message"] == "auth"


def test_raised_publish_exception_marks_upload_and_video_failed(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    video_id = conn.execute(
        "INSERT INTO videos (filename, file_path, upload_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("v.mp4", "/tmp/v.mp4", "queued", now, now),
    ).lastrowid
    upload_id = _upload_row(conn, video_id=video_id)
    conn.commit()
    monkeypatch.setattr(handler.youtube, "process_upload", lambda c, uid, **kw: {"status": "done", "youtube_video_id": "xyz"})
    monkeypatch.setattr(handler.youtube, "publish_completed_upload", lambda c, uid, **kw: (_ for _ in ()).throw(ValueError("publish failed")))
    ctx, _ = _ctx(conn, upload_id, video_id=video_id)

    with pytest.raises(ValueError, match="publish failed"):
        handler.handle(ctx)

    upload = conn.execute("SELECT status, error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    video = conn.execute("SELECT upload_status, error_message FROM videos WHERE id=?", (video_id,)).fetchone()
    assert dict(upload) == {"status": "failed", "error_message": "ValueError: publish failed"}
    assert dict(video) == {"upload_status": "failed", "error_message": "ValueError: publish failed"}


def test_fatal_postprocess_exception_marks_upload_and_raises_job_fatal_error(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    upload_id = _upload_row(conn)
    monkeypatch.setattr(handler.youtube, "process_upload", lambda c, uid, **kw: {"status": "done", "youtube_video_id": "xyz"})
    monkeypatch.setattr(handler.youtube, "publish_completed_upload", lambda c, uid, **kw: (_ for _ in ()).throw(ValueError("forbidden: publish denied")))
    ctx, _ = _ctx(conn, upload_id)

    with pytest.raises(JobFatalError, match="forbidden: publish denied"):
        handler.handle(ctx)

    row = conn.execute("SELECT status, error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert dict(row) == {"status": "failed", "error_message": "ValueError: forbidden: publish denied"}


def test_raised_sync_exception_marks_upload_and_video_failed(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    video_id = conn.execute(
        "INSERT INTO videos (filename, file_path, upload_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        ("v.mp4", "/tmp/v.mp4", "queued", now, now),
    ).lastrowid
    upload_id = _upload_row(conn, video_id=video_id)
    conn.commit()
    monkeypatch.setattr(handler.youtube, "process_upload", lambda c, uid, **kw: {"status": "done", "youtube_video_id": "xyz"})
    monkeypatch.setattr(handler.youtube, "publish_completed_upload", lambda c, uid, **kw: {"status": "published"})
    monkeypatch.setattr(handler, "sync_pipeline_from_upload", lambda c, uid, **kw: (_ for _ in ()).throw(RuntimeError("sync failed")))
    ctx, _ = _ctx(conn, upload_id, video_id=video_id)

    with pytest.raises(RuntimeError, match="sync failed"):
        handler.handle(ctx)

    upload = conn.execute("SELECT status, error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    video = conn.execute("SELECT upload_status, error_message FROM videos WHERE id=?", (video_id,)).fetchone()
    assert dict(upload) == {"status": "failed", "error_message": "RuntimeError: sync failed"}
    assert dict(video) == {"upload_status": "failed", "error_message": "RuntimeError: sync failed"}
