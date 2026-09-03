from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import db, repository, youtube
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.handlers import video, youtube_upload
from app.jobqueue.joblog import JobLogger
from app.jobqueue.models import JobFatalError
from app.video_integrity import ValidationFacts, ValidationResult


VALID = ValidationResult(True, None, "", (), ValidationFacts(), 0)
INVALID = ValidationResult(False, "decode_failed", "corrupt frame", (), ValidationFacts(), 0)


def _setup(tmp_path):
    conn = db.connect(str(tmp_path / "a.db")); db.init_schema(conn); now = datetime.now(timezone.utc).isoformat()
    audio = tmp_path / "book.wav"; audio.write_bytes(b"audio")
    conn.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,final_audio_path,created_at,updated_at) VALUES (1,'Book','b','b',1,'done',?,?,?)", (str(audio), now, now)); conn.commit()
    book_job = repository.enqueue_book_job(conn, 1, "video")
    output = tmp_path / "books" / "1" / f"video_{book_job.id}.mp4"; output.parent.mkdir(parents=True); output.write_bytes(b"initial")
    upload_id = youtube.enqueue_upload(conn, str(output), "Title", render_source_type="book", render_source_id=book_job.id)
    return conn, book_job, upload_id, output


def _ctx(conn, job_type, worker):
    job = store.claim(conn, job_type, worker)
    return JobContext(job, conn, JobLogger(job.id, job_type), lambda: False)


def test_two_invalid_upload_checks_rerender_then_upload_same_row(tmp_path, monkeypatch):
    conn, book_job, upload_id, output = _setup(tmp_path); checks = iter([INVALID, INVALID, VALID]); youtube_calls=[]; renders=[]
    monkeypatch.setattr(youtube_upload, "validate_video", lambda p: next(checks))
    monkeypatch.setattr(video, "validate_video", lambda p: VALID)
    monkeypatch.setattr(video.video_gen, "generate_full_video", lambda *a, **k: (renders.append(Path(a[2])), Path(a[2]).write_bytes(b"rendered")))
    monkeypatch.setattr(youtube_upload.youtube, "process_upload", lambda c, uid, **kw: youtube_calls.append(uid) or {"status":"done","youtube_video_id":"yt"})
    monkeypatch.setattr(youtube_upload.youtube, "publish_completed_upload", lambda *a, **kw: {"status":"published"})
    monkeypatch.setattr(youtube_upload, "sync_pipeline_from_upload", lambda *a, **kw: None)
    store.enqueue(conn,"youtube_upload",payload={"upload_id":upload_id},dedupe_key=f"initial:{upload_id}")
    for generation in (1, 2):
        upload_ctx = _ctx(conn,"youtube_upload",f"u{generation}"); result=youtube_upload.handle(upload_ctx); store.finish(conn,upload_ctx.job.id,result)
        assert result == {"rerender_scheduled":True,"retry_count":generation}
        render_ctx = _ctx(conn,"video",f"v{generation}"); video.handle(render_ctx); store.finish(conn,render_ctx.job.id,{})
    final_ctx = _ctx(conn,"youtube_upload","u3"); youtube_upload.handle(final_ctx)
    row = conn.execute("SELECT status,validation_status,integrity_retry_count,youtube_video_id FROM youtube_uploads WHERE id=?",(upload_id,)).fetchone()
    assert tuple(row) == ("done","valid",2,"yt")
    assert youtube_calls == [upload_id]
    assert len(renders) == 2 and all(path != output for path in renders)
    assert conn.execute("SELECT COUNT(*) FROM youtube_uploads").fetchone()[0] == 1


def test_failure_after_retry_two_is_terminal_without_youtube(tmp_path, monkeypatch):
    conn, book_job, upload_id, output = _setup(tmp_path); youtube_calls=[]
    conn.execute("UPDATE youtube_uploads SET integrity_retry_count=2 WHERE id=?",(upload_id,)); conn.commit()
    monkeypatch.setattr(youtube_upload,"validate_video",lambda p: INVALID)
    monkeypatch.setattr(youtube_upload.youtube,"process_upload",lambda *a, **kw: youtube_calls.append(1))
    store.enqueue(conn,"youtube_upload",payload={"upload_id":upload_id})
    with pytest.raises(JobFatalError,match="2/2"):
        youtube_upload.handle(_ctx(conn,"youtube_upload","u"))
    row=conn.execute("SELECT status,integrity_retry_count FROM youtube_uploads WHERE id=?",(upload_id,)).fetchone()
    assert tuple(row)==("failed",2)
    assert youtube_calls==[]
