"""store.py: enqueue/dedupe, claim nguyên tử dưới nhiều thread, backoff, reaper."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from app import db
from app.jobqueue import store
from app.jobqueue.models import CANCELLED, CANCELLING, DONE, FAILED, PENDING, RUNNING


def _conn(tmp_path=None):
    conn = db.connect(str(tmp_path / "app.db") if tmp_path else ":memory:")
    db.init_schema(conn)
    return conn


def _iso(delta_seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def test_enqueue_returns_a_job_id():
    conn = _conn()
    job_id = store.enqueue(conn, "video", payload={"book_job_id": 3}, book_id=9)
    job = store.get(conn, job_id)
    assert job.job_type == "video"
    assert job.payload == {"book_job_id": 3}
    assert job.book_id == 9
    assert job.status == PENDING


def test_enqueue_with_a_live_dedupe_key_returns_none():
    conn = _conn()
    first = store.enqueue(conn, "video", dedupe_key="video:book_job=3")
    assert store.enqueue(conn, "video", dedupe_key="video:book_job=3") is None
    assert store.find_live_by_dedupe(conn, "video:book_job=3").id == first


def test_enqueue_reuses_a_dedupe_key_after_the_job_finished():
    conn = _conn()
    first = store.enqueue(conn, "video", dedupe_key="k")
    store.finish(conn, first, {"ok": True})
    second = store.enqueue(conn, "video", dedupe_key="k")
    assert second is not None and second != first


def test_enqueue_dedupes_live_job_by_type_and_payload_patch_id():
    conn = _conn()
    now = _iso()
    conn.execute(
        """INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,created_at,updated_at)
           VALUES (1,'Sách','a.epub','a.epub',10,'ready',?,?)""", (now, now))
    patch_id = conn.execute(
        """INSERT INTO patch (book_id,patch_index,chapter_start,chapter_end,status,
                              attempt_count,created_at,updated_at)
           VALUES (1,0,0,0,'pending',0,?,?)""", (now, now)).lastrowid
    conn.commit()

    first = store.enqueue(conn, "patch_video", payload={"patch_id": patch_id})

    assert first is not None
    assert store.get(conn, first).patch_id == patch_id
    assert store.enqueue(conn, "patch_video", payload={"patch_id": patch_id}) is None
    assert store.enqueue(conn, "light_tts", payload={"patch_id": patch_id}) is not None


def test_enqueue_rejects_mismatched_patch_ids():
    conn = _conn()
    with pytest.raises(ValueError, match="patch_id does not match"):
        store.enqueue(conn, "patch_video", payload={"patch_id": 1}, patch_id=2)


def test_claim_moves_the_job_to_running_and_bumps_attempt_count():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    job = store.claim(conn, "video", "video#0")
    assert job.id == job_id
    assert job.status == RUNNING
    assert job.attempt_count == 1
    assert job.worker_id == "video#0"
    assert job.started_at is not None


def test_claim_only_returns_jobs_of_the_requested_type():
    conn = _conn()
    store.enqueue(conn, "video")
    assert store.claim(conn, "light_tts", "w") is None


def test_claim_respects_priority_then_id():
    conn = _conn()
    low = store.enqueue(conn, "video", priority=100)
    high = store.enqueue(conn, "video", priority=10)
    assert store.claim(conn, "video", "w").id == high
    assert store.claim(conn, "video", "w").id == low


def test_claim_skips_a_job_whose_retry_time_has_not_arrived():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    store.claim(conn, "video", "w")
    store.fail(conn, job_id, "boom")          # -> pending, next_retry_at ở tương lai
    assert store.claim(conn, "video", "w") is None


def test_claim_is_atomic_across_threads(tmp_path):
    """20 thread cùng claim 5 job — không job nào được giao hai lần."""
    conn = _conn(tmp_path)
    for _ in range(5):
        store.enqueue(conn, "video")
    conn.close()

    claimed: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(20)

    def worker(n: int):
        c = db.connect(str(tmp_path / "app.db"))
        start.wait()
        job = store.claim(c, "video", f"video#{n}")
        if job is not None:
            with lock:
                claimed.append(job.id)
        c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(claimed) == 5
    assert len(set(claimed)) == 5


def test_backoff_grows_and_is_capped():
    assert store.backoff_seconds(1) == 60
    assert store.backoff_seconds(2) == 120
    assert store.backoff_seconds(3) == 240
    assert store.backoff_seconds(99) == 600


def test_fail_reschedules_while_attempts_remain():
    conn = _conn()
    job_id = store.enqueue(conn, "video", max_attempts=3)
    store.claim(conn, "video", "w")
    assert store.fail(conn, job_id, "boom") == PENDING
    job = store.get(conn, job_id)
    assert job.error_message == "boom"
    assert job.next_retry_at > _iso()


def test_fail_gives_up_once_attempts_are_exhausted():
    conn = _conn()
    job_id = store.enqueue(conn, "video", max_attempts=2)
    store.claim(conn, "video", "w")
    store.fail(conn, job_id, "one")
    conn.execute("UPDATE job SET next_retry_at=NULL WHERE id=?", (job_id,))
    conn.commit()
    store.claim(conn, "video", "w")
    assert store.fail(conn, job_id, "two") == FAILED
    assert store.get(conn, job_id).finished_at is not None


def test_an_explicit_max_attempts_overrides_the_stored_one():
    """Runner áp số của HandlerSpec: job enqueue với max_attempts=5 nhưng handler đăng ký
    max_attempts=1 thì hỏng một lần là bỏ."""
    conn = _conn()
    job_id = store.enqueue(conn, "video", max_attempts=5)
    store.claim(conn, "video", "w")
    assert store.fail(conn, job_id, "boom", max_attempts=1) == FAILED


def test_fatal_failure_skips_retry_entirely():
    conn = _conn()
    job_id = store.enqueue(conn, "video", max_attempts=5)
    store.claim(conn, "video", "w")
    assert store.fail(conn, job_id, "file missing", fatal=True) == FAILED


def test_write_progress_updates_counters_and_heartbeat():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    store.claim(conn, "video", "w")
    store.write_progress(conn, job_id, current=3, total=10, phase="encoding")
    job = store.get(conn, job_id)
    assert (job.progress_current, job.progress_total, job.phase) == (3, 10, "encoding")
    assert job.heartbeat_at is not None


def test_finish_stores_the_result():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    store.claim(conn, "video", "w")
    store.finish(conn, job_id, {"output_path": "/x.mp4"})
    job = store.get(conn, job_id)
    assert job.status == DONE
    assert job.result == {"output_path": "/x.mp4"}
    assert job.finished_at is not None


def test_cancel_a_pending_job_is_immediate():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    assert store.request_cancel(conn, job_id) == CANCELLED
    assert store.get(conn, job_id).status == CANCELLED


def test_cancel_a_running_job_asks_politely():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    store.claim(conn, "video", "w")
    assert store.request_cancel(conn, job_id) == CANCELLING
    store.mark_cancelled(conn, job_id)
    assert store.get(conn, job_id).status == CANCELLED


def test_cancel_a_finished_job_does_nothing():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    store.finish(conn, job_id, None)
    assert store.request_cancel(conn, job_id) is None


def test_retry_resets_a_failed_job():
    conn = _conn()
    job_id = store.enqueue(conn, "video", max_attempts=1)
    store.claim(conn, "video", "w")
    store.fail(conn, job_id, "boom")
    assert store.retry(conn, job_id) is True
    job = store.get(conn, job_id)
    assert job.status == PENDING
    assert job.attempt_count == 0
    assert job.error_message is None
    assert job.next_retry_at is None


def test_retry_refuses_a_running_job():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    store.claim(conn, "video", "w")
    assert store.retry(conn, job_id) is False


def test_retry_all_failed_only_retries_latest_type_patch_pair():
    conn = _conn()
    now = _iso()
    conn.execute(
        """INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,created_at,updated_at)
           VALUES (1,'Sách','a.epub','a.epub',10,'ready',?,?)""", (now, now))
    patch_id = conn.execute(
        """INSERT INTO patch (book_id,patch_index,chapter_start,chapter_end,status,
                              attempt_count,created_at,updated_at)
           VALUES (1,0,0,0,'pending',0,?,?)""", (now, now)).lastrowid
    conn.commit()
    old = store.enqueue(conn, "patch_video", payload={"patch_id": patch_id})
    conn.execute("UPDATE job SET status='failed' WHERE id=?", (old,))
    conn.commit()
    latest = store.enqueue(conn, "patch_video", payload={"patch_id": patch_id})
    conn.execute("UPDATE job SET status='failed' WHERE id=?", (latest,))
    conn.commit()

    assert store.retry_all_failed(conn) == 1
    assert store.get(conn, old).status == FAILED
    assert store.get(conn, latest).status == PENDING


def test_a_reaped_worker_cannot_finish_a_job_someone_else_now_owns():
    """Kịch bản zombie: A bị reap giữa chừng, B claim lại, rồi A mới xong. Lần ghi
    muộn của A phải là no-op, không được đè lên lượt chạy của B."""
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    a = store.claim(conn, "video", "video#A")
    conn.execute("UPDATE job SET heartbeat_at=? WHERE id=?", (_iso(-3600), job_id))
    conn.commit()
    store.reap_stale(conn, older_than_seconds=120)
    b = store.claim(conn, "video", "video#B")
    assert b.worker_id == "video#B"

    assert store.finish(conn, job_id, {"from": "A"}, worker_id=a.worker_id) is False
    job = store.get(conn, job_id)
    assert job.status == RUNNING
    assert job.worker_id == "video#B"
    assert job.result is None

    assert store.finish(conn, job_id, {"from": "B"}, worker_id=b.worker_id) is True
    assert store.get(conn, job_id).result == {"from": "B"}


def test_a_reaped_worker_cannot_fail_a_job_someone_else_now_owns():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    a = store.claim(conn, "video", "video#A")
    conn.execute("UPDATE job SET heartbeat_at=? WHERE id=?", (_iso(-3600), job_id))
    conn.commit()
    store.reap_stale(conn, older_than_seconds=120)
    store.claim(conn, "video", "video#B")

    assert store.fail(conn, job_id, "A nói hỏng", worker_id=a.worker_id) is None
    job = store.get(conn, job_id)
    assert job.status == RUNNING
    assert job.error_message is None


def test_a_reaped_worker_cannot_move_progress_or_heartbeat():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    a = store.claim(conn, "video", "video#A")
    conn.execute("UPDATE job SET heartbeat_at=? WHERE id=?", (_iso(-3600), job_id))
    conn.commit()
    store.reap_stale(conn, older_than_seconds=120)
    store.claim(conn, "video", "video#B")

    assert store.write_progress(
        conn, job_id, current=99, total=99, phase="ma", worker_id=a.worker_id) is False
    assert store.heartbeat(conn, job_id, worker_id=a.worker_id) is False
    job = store.get(conn, job_id)
    assert job.progress_current == 0
    assert job.phase is None


def test_a_reaped_worker_cannot_mark_cancelled():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    a = store.claim(conn, "video", "video#A")
    conn.execute("UPDATE job SET heartbeat_at=? WHERE id=?", (_iso(-3600), job_id))
    conn.commit()
    store.reap_stale(conn, older_than_seconds=120)
    store.claim(conn, "video", "video#B")
    assert store.mark_cancelled(conn, job_id, worker_id=a.worker_id) is False
    assert store.get(conn, job_id).status == RUNNING


def test_the_owning_worker_writes_normally():
    """Rào chỉ chặn kẻ lạ — chủ sở hữu thật vẫn ghi được như thường."""
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    job = store.claim(conn, "video", "video#A")
    assert store.write_progress(
        conn, job_id, current=2, total=5, phase="encoding", worker_id=job.worker_id) is True
    assert store.heartbeat(conn, job_id, worker_id=job.worker_id) is True
    assert store.finish(conn, job_id, {"ok": True}, worker_id=job.worker_id) is True
    assert store.get(conn, job_id).status == DONE


def test_writes_without_a_worker_id_are_unfenced():
    """Route admin gọi không kèm worker_id và vẫn phải ghi được."""
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    store.claim(conn, "video", "video#A")
    assert store.finish(conn, job_id, {"by": "admin"}) is True
    assert store.get(conn, job_id).status == DONE


def test_fail_on_a_missing_job_returns_none():
    conn = _conn()
    assert store.fail(conn, 4242, "không tồn tại") is None


def test_reap_returns_a_stale_running_job_to_pending():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    store.claim(conn, "video", "w")
    conn.execute("UPDATE job SET heartbeat_at=? WHERE id=?", (_iso(-3600), job_id))
    conn.commit()
    assert store.reap_stale(conn, older_than_seconds=120) == [job_id]
    job = store.get(conn, job_id)
    assert job.status == PENDING
    assert job.worker_id is None


def test_reap_leaves_a_freshly_heartbeating_job_alone():
    conn = _conn()
    store.enqueue(conn, "video")
    store.claim(conn, "video", "w")
    assert store.reap_stale(conn, older_than_seconds=120) == []


def test_reap_uses_started_at_when_heartbeat_is_still_null():
    conn = _conn()
    job_id = store.enqueue(conn, "video")
    store.claim(conn, "video", "w")
    conn.execute("UPDATE job SET heartbeat_at=NULL, started_at=? WHERE id=?", (_iso(-3600), job_id))
    conn.commit()
    assert store.reap_stale(conn, older_than_seconds=120) == [job_id]


def test_list_jobs_filters_and_orders_newest_first():
    conn = _conn()
    a = store.enqueue(conn, "video", book_id=1)
    b = store.enqueue(conn, "light_tts", book_id=1)
    store.enqueue(conn, "video", book_id=2)
    assert [j.id for j in store.list_jobs(conn, book_id=1)] == [b, a]
    assert [j.id for j in store.list_jobs(conn, job_type="light_tts")] == [b]
    assert [j.id for j in store.list_jobs(conn, status=PENDING, limit=1)] == [
        store.list_jobs(conn)[0].id
    ]


def test_counts_group_by_type_and_status():
    conn = _conn()
    store.enqueue(conn, "video")
    store.enqueue(conn, "video")
    store.claim(conn, "video", "w")
    store.enqueue(conn, "light_tts")
    counts = store.counts(conn)
    assert counts["video"] == {"pending": 1, "running": 1}
    assert counts["light_tts"] == {"pending": 1}


def test_clear_inactive_preserves_active_jobs():
    conn = _conn()
    for status in (PENDING, DONE, FAILED, CANCELLED, RUNNING, CANCELLING):
        job_id = store.enqueue(conn, "video")
        conn.execute("UPDATE job SET status=? WHERE id=?", (status, job_id))
    conn.commit()

    assert store.clear_inactive(conn) == 4
    assert {job.status for job in store.list_jobs(conn)} == {RUNNING, CANCELLING}


def test_pending_count_is_per_type():
    conn = _conn()
    store.enqueue(conn, "video")
    store.enqueue(conn, "light_tts")
    assert store.pending_count(conn, "video") == 1
    assert store.pending_count(conn, "youtube_upload") == 0
