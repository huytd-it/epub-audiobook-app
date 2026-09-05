"""store.reschedule: đưa job về pending tại thời điểm chỉ định, không đụng attempt_count;
runner bắt JobRescheduled thay vì coi là lỗi."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db
from app.jobqueue import store
from app.jobqueue.models import PENDING


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _future(seconds=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_reschedule_returns_job_to_pending_without_touching_attempt_count():
    conn = _conn()
    job_id = store.enqueue(conn, "kaggle_tts")
    store.claim(conn, "kaggle_tts", "w")
    target = _future()
    assert store.reschedule(conn, job_id, target, "no quota") is True
    job = store.get(conn, job_id)
    assert job.status == PENDING
    assert job.attempt_count == 1          # không reset, không tăng thêm
    assert job.next_retry_at == target
    assert job.error_message == "no quota"
    assert job.worker_id is None


def test_reschedule_is_fenced_like_finish_and_fail():
    conn = _conn()
    job_id = store.enqueue(conn, "kaggle_tts")
    a = store.claim(conn, "kaggle_tts", "kaggle_tts#A")
    conn.execute(
        "UPDATE job SET heartbeat_at=? WHERE id=?",
        ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(), job_id),
    )
    conn.commit()
    store.reap_stale(conn, older_than_seconds=120)
    store.claim(conn, "kaggle_tts", "kaggle_tts#B")
    assert store.reschedule(conn, job_id, _future(), worker_id=a.worker_id) is False


def test_reschedule_without_worker_id_is_unfenced():
    conn = _conn()
    job_id = store.enqueue(conn, "kaggle_tts")
    store.claim(conn, "kaggle_tts", "w")
    assert store.reschedule(conn, job_id, _future()) is True


def test_claim_skips_a_rescheduled_job_before_its_time():
    conn = _conn()
    job_id = store.enqueue(conn, "kaggle_tts")
    store.claim(conn, "kaggle_tts", "w")
    store.reschedule(conn, job_id, _future(), worker_id="w")
    assert store.claim(conn, "kaggle_tts", "w2") is None


def test_claim_picks_up_a_rescheduled_job_once_its_time_arrives():
    conn = _conn()
    job_id = store.enqueue(conn, "kaggle_tts")
    store.claim(conn, "kaggle_tts", "w")
    store.reschedule(conn, job_id, _future(-10), worker_id="w")
    job = store.claim(conn, "kaggle_tts", "w2")
    assert job is not None and job.id == job_id
