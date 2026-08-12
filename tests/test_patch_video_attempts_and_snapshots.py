"""patch_video handler: frozen-snapshot renders, render attempt caps, expectations."""
import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app import db, repository
from app.config import settings
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.handlers import patch_video
from app.jobqueue.joblog import JobLogger
from app.jobqueue.models import JobFatalError
from app.patch_publishing import MAX_PATCH_RENDER_ATTEMPTS
from app.video_integrity import ValidationFacts, ValidationResult, VideoExpectation

RATE = 100
FRAMES = 1200


def _wav(tmp_path, frames=FRAMES) -> Path:
    path = tmp_path / "a.wav"
    buf = BytesIO()
    sf.write(buf, np.zeros(frames), RATE, format="WAV")
    path.write_bytes(buf.getvalue())
    return path


def _snapshot_fixture(tmp_path, monkeypatch, *, audio=None, fingerprint_override=None):
    conn = db.connect(str(tmp_path / "app.db"))
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    audio = audio or _wav(tmp_path)
    thumb = tmp_path / "thumb.png"
    thumb.write_bytes(b"i")
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn.execute(
        "INSERT INTO book (id, title, original_filename, epub_path, patch_size, status, "
        "video_resolution, video_fps, created_at, updated_at) "
        "VALUES (1, 'B', 'b', 'b', 1, 'done', '854x480', 24, ?, ?)", (now, now),
    )
    conn.execute(
        "INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status, "
        "audio_path, created_at, updated_at) VALUES (2, 1, 0, 0, 0, 'done', ?, ?, ?)",
        (str(audio), now, now),
    )
    conn.execute(
        "INSERT INTO patch_pipeline (patch_id, stage, config_snapshot, media_snapshot, "
        "created_at, updated_at) VALUES (2, 'pending', '{}', '{}', ?, ?)", (now, now),
    )
    snapshot = {
        "audio_path": str(audio),
        "audio_fingerprint": fingerprint_override or patch_video.audio_fingerprint(
            repository.get_patch(conn, 2)),
        "thumbnail_path": str(thumb),
        "image": str(thumb),
        "image_type": "none",
        "sequence": False,
        "backgrounds": [],
        "render_config": {
            "resolution": "854x480", "fps": 24, "codec": "h264",
            "crf": 23, "audio_bitrate": "192k",
        },
        "sequence_config": {},
    }
    conn.commit()
    return conn, snapshot


def _job(conn, snapshot=None, *, patch_id=2):
    payload = {"patch_id": patch_id}
    if snapshot is not None:
        payload.update({"snapshot": snapshot, "schema_version": 1})
    job_id = store.enqueue(conn, "patch_video", payload=payload, book_id=1)
    job = store.claim(conn, "patch_video", "w")
    return job_id, job


def _handle(conn, job):
    return patch_video.handle(JobContext(job, conn, JobLogger(job.id, "patch_video"), lambda: False))


def test_media_changed_after_enqueue_is_fatal(tmp_path, monkeypatch):
    conn, snapshot = _snapshot_fixture(tmp_path, monkeypatch)
    snapshot["audio_fingerprint"] = "changed:0:0"
    _, job = _job(conn, snapshot)
    with pytest.raises(JobFatalError, match="source_changed"):
        _handle(conn, job)


def test_snapshot_audio_missing_is_fatal(tmp_path, monkeypatch):
    conn, snapshot = _snapshot_fixture(tmp_path, monkeypatch)
    snapshot["audio_path"] = str(tmp_path / "gone.wav")
    _, job = _job(conn, snapshot)
    with pytest.raises(JobFatalError, match="audio missing"):
        _handle(conn, job)


def test_snapshot_render_increments_attempts_and_passes_expected(tmp_path, monkeypatch):
    conn, snapshot = _snapshot_fixture(tmp_path, monkeypatch)
    seen = {}

    def fake_gen(a, b, out, **kw):
        seen.update(kw)
        Path(out).write_bytes(b"new")

    monkeypatch.setattr(patch_video.video_gen, "generate_segment", fake_gen)

    def fake_validate(path, **kw):
        seen["expected"] = kw["expected"]
        return ValidationResult(True, None, "", (), ValidationFacts(), 0)

    monkeypatch.setattr(patch_video, "validate_video", fake_validate)
    _, job = _job(conn, snapshot)
    _handle(conn, job)

    expected = seen["expected"]
    assert isinstance(expected, VideoExpectation)
    assert expected.duration_seconds == pytest.approx(FRAMES / RATE)
    assert (expected.width, expected.height) == (854, 480)
    assert expected.fps == 24
    row = conn.execute(
        "SELECT render_attempts, stage, video_status FROM patch_pipeline WHERE patch_id=2"
    ).fetchone()
    assert (row["render_attempts"], row["stage"], row["video_status"]) == (1, "upload", "done")
    report = json.loads(conn.execute(
        "SELECT validation_report_json FROM patch_pipeline WHERE patch_id=2"
    ).fetchone()[0])
    assert report["valid"] is True
    assert report["expected"]["duration_seconds"] == pytest.approx(FRAMES / RATE)


def test_render_attempt_cap_blocks_further_jobs(tmp_path, monkeypatch):
    conn, snapshot = _snapshot_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(patch_video.video_gen, "generate_segment",
                        lambda a, b, out, **kw: Path(out).write_bytes(b"new"))
    monkeypatch.setattr(patch_video, "validate_video",
                        lambda p, **kw: ValidationResult(True, None, "", (), ValidationFacts(), 0))
    conn.execute(
        "UPDATE patch_pipeline SET stage='upload', render_attempts=? WHERE patch_id=2",
        (MAX_PATCH_RENDER_ATTEMPTS,),
    )
    conn.commit()
    _, job = _job(conn, snapshot)
    with pytest.raises(JobFatalError, match="render_attempt_limit"):
        _handle(conn, job)
    pipeline = conn.execute(
        "SELECT preflight_error_code FROM patch_pipeline WHERE patch_id=2"
    ).fetchone()
    assert pipeline["preflight_error_code"] == "render_attempt_limit"


def test_snapshot_render_is_atomic_on_validation_failure(tmp_path, monkeypatch):
    conn, snapshot = _snapshot_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(patch_video.video_gen, "generate_segment",
                        lambda a, b, out, **kw: Path(out).write_bytes(b"new"))
    from app.video_publish import VideoValidationError

    def failing(path, **kw):
        raise VideoValidationError(ValidationResult(False, "decode_failed", "boom",
                                                    (), ValidationFacts(), 0))

    monkeypatch.setattr(patch_video, "validate_video", failing)
    _, job = _job(conn, snapshot)
    with pytest.raises(VideoValidationError):
        _handle(conn, job)
    report = json.loads(conn.execute(
        "SELECT validation_report_json FROM patch_pipeline WHERE patch_id=2"
    ).fetchone()[0])
    assert report["valid"] is False
    assert report["error_code"] == "decode_failed"
    assert conn.execute(
        "SELECT COUNT(*) FROM videos WHERE patch_id=2"
    ).fetchone()[0] == 0