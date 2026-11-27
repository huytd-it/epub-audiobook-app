from __future__ import annotations

import asyncio
import sqlite3
import threading
import time

import pytest

from app import db
from app.jobqueue import store
from app.jobqueue.models import JobFatalError
from app.jobqueue.runner import JobQueue, parse_concurrency


@pytest.fixture(autouse=True)
def isolated_root(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_root", str(tmp_path))


@pytest.fixture
def factory(tmp_path):
    path = str(tmp_path / "queue.db")
    c = db.connect(path); db.init_schema(c); c.close()
    return lambda: db.connect(path)


def queue(factory, **kwargs):
    return JobQueue(factory, concurrency={}, default_concurrency=10, poll_interval=.01, **kwargs)


async def drain(c, timeout=10):
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        if c.execute("select count(*) c from job where status in ('pending','running')").fetchone()["c"] == 0:
            return
        await asyncio.sleep(.02)
    raise AssertionError("queue did not drain")


@pytest.mark.asyncio
async def test_pool_fills_to_capacity_when_jobs_are_waiting(factory):
    """capacity=6 và đủ job ⇒ đúng 6 job chạy song song, không để slot trống."""
    c = factory()
    [store.enqueue(c, "demo") for _ in range(12)]
    live = peak = 0
    lock = threading.Lock()

    def fn(ctx):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return {}

    q = JobQueue(factory, concurrency={"demo": 6}, poll_interval=0.01)
    q.register("demo", fn)
    await q.start()
    await drain(c, 20)
    await q.stop(5)
    assert peak == 6


@pytest.mark.asyncio
async def test_dispatch_recovers_from_a_claim_error(factory, monkeypatch):
    """Một lần claim vấp OperationalError (DB khóa) không được làm chết dispatcher."""
    c = factory()
    jid = store.enqueue(c, "demo")
    real_claim = store.claim
    calls = {"n": 0}

    def flaky_claim(conn, job_type, worker_id, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return real_claim(conn, job_type, worker_id, **kw)

    monkeypatch.setattr(store, "claim", flaky_claim)
    q = queue(factory)
    q.register("demo", lambda ctx: {})
    await q.start()
    await drain(c, 20)
    await q.stop(5)
    assert calls["n"] >= 2
    assert store.get(c, jid).status == "done"


@pytest.mark.asyncio
async def test_supervisor_restarts_a_dead_dispatcher(factory, monkeypatch):
    """Dispatcher thoát/raise ngoài dự kiến phải được supervisor dựng lại, không
    để pool rảnh trong khi job còn chờ."""
    c = factory()
    jid = store.enqueue(c, "demo")
    real_dispatch = JobQueue._dispatch
    calls = {"n": 0}

    async def flaky_dispatch(self, job_type):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        await real_dispatch(self, job_type)

    monkeypatch.setattr(JobQueue, "_dispatch", flaky_dispatch)
    q = queue(factory)
    q.register("demo", lambda ctx: {})
    await q.start()
    await drain(c, 20)
    await q.stop(5)
    assert calls["n"] >= 2
    assert store.get(c, jid).status == "done"


def test_parse_concurrency_reads_values():
    assert parse_concurrency("audiobook_tts=1,video=2,youtube_upload=1", default=10) == {"audiobook_tts": 1, "video": 2, "youtube_upload": 1}

def test_parse_concurrency_ignores_whitespace_and_empty_entries():
    assert parse_concurrency(" video = 2 ,, ", default=10) == {"video": 2}

def test_parse_concurrency_ignores_malformed_entries():
    assert parse_concurrency("video=abc,light_tts=3,broken", default=10) == {"light_tts": 3}

def test_parse_concurrency_empty_is_empty():
    assert parse_concurrency("", default=10) == {}


def test_parse_concurrency_preserves_zero_as_disabled():
    assert parse_concurrency("audiobook_tts=0,video=2", default=10) == {
        "audiobook_tts": 0, "video": 2,
    }

def test_capacity_uses_config_or_default():
    q = JobQueue(lambda: None, concurrency={"video": 2}, default_concurrency=10)
    q.register("video", lambda ctx: {}); q.register("light_tts", lambda ctx: {})
    assert q.capacity("video") == 2 and q.capacity("light_tts") == 10

def test_explicit_capacity_wins():
    q = JobQueue(lambda: None, concurrency={"video": 2}, default_concurrency=10)
    q.register("video", lambda ctx: {}, concurrency=5)
    assert q.capacity("video") == 5

@pytest.mark.asyncio
async def test_job_runs_and_is_done(factory):
    c = factory(); job_id = store.enqueue(c, "demo", payload={"x": 2})
    q = queue(factory); q.register("demo", lambda ctx: {"doubled": ctx.job.payload["x"] * 2})
    await q.start(); await drain(c); await q.stop(5)
    assert store.get(c, job_id).result == {"doubled": 4}

@pytest.mark.asyncio
async def test_concurrency_is_capped(factory):
    c = factory(); [store.enqueue(c, "demo") for _ in range(30)]
    live = peak = 0; lock = threading.Lock()
    def fn(ctx):
        nonlocal live, peak
        with lock: live += 1; peak = max(peak, live)
        time.sleep(.03)
        with lock: live -= 1
        return {}
    q = queue(factory); q.register("demo", fn); await q.start(); await drain(c, 20); await q.stop(5)
    assert peak <= 10 and peak > 1

@pytest.mark.asyncio
async def test_each_type_has_own_cap(factory):
    c = factory(); [store.enqueue(c, t) for t in ("slow", "fast") for _ in range(8)]
    live = {"slow": 0, "fast": 0}; peak = {"slow": 0, "fast": 0}; lock = threading.Lock()
    def fn(kind):
        def run(ctx):
            with lock: live[kind] += 1; peak[kind] = max(peak[kind], live[kind])
            time.sleep(.03)
            with lock: live[kind] -= 1
            return {}
        return run
    q = JobQueue(factory, concurrency={"slow": 1, "fast": 4}, poll_interval=.01)
    q.register("slow", fn("slow")); q.register("fast", fn("fast")); await q.start(); await drain(c, 20); await q.stop(5)
    assert peak["slow"] == 1 and peak["fast"] <= 4

@pytest.mark.asyncio
async def test_failure_is_rescheduled(factory):
    c = factory(); jid = store.enqueue(c, "demo", max_attempts=3)
    q = queue(factory); q.register("demo", lambda ctx: (_ for _ in ()).throw(RuntimeError("boom")))
    await q.start()
    for _ in range(100):
        if store.get(c, jid).attempt_count: break
        await asyncio.sleep(.02)
    await q.stop(5); job = store.get(c, jid)
    assert job.status == "pending" and job.error_message == "boom" and job.next_retry_at

@pytest.mark.asyncio
async def test_fatal_failure_does_not_retry(factory):
    c = factory(); jid = store.enqueue(c, "demo", max_attempts=5)
    q = queue(factory); q.register("demo", lambda ctx: (_ for _ in ()).throw(JobFatalError("bad")))
    await q.start(); await drain(c); await q.stop(5)
    assert store.get(c, jid).status == "failed" and store.get(c, jid).attempt_count == 1

@pytest.mark.asyncio
async def test_pause_stops_claiming(factory):
    c = factory(); store.enqueue(c, "demo")
    q = JobQueue(factory, concurrency={}, poll_interval=.01, is_paused=lambda c: True); q.register("demo", lambda ctx: {})
    await q.start(); await asyncio.sleep(.1); await q.stop(5)
    assert store.list_jobs(c)[0].status == "pending" and q.state == "paused"

@pytest.mark.asyncio
async def test_cancel_is_seen_by_handler(factory):
    c = factory(); jid = store.enqueue(c, "demo"); started = threading.Event(); seen = threading.Event()
    def fn(ctx):
        started.set()
        for _ in range(100):
            if ctx.should_cancel(): seen.set(); raise asyncio.CancelledError()
            time.sleep(.01)
        return {}
    q = queue(factory); q.register("demo", fn); await q.start(); await asyncio.get_running_loop().run_in_executor(None, started.wait, 5); q.request_cancel(jid); await asyncio.get_running_loop().run_in_executor(None, seen.wait, 5); await q.stop(5)
    assert seen.is_set() and store.get(c, jid).status == "cancelled"

@pytest.mark.asyncio
async def test_stop_drains_running_job(factory):
    c = factory(); store.enqueue(c, "demo"); finished = threading.Event()
    def fn(ctx): time.sleep(.1); finished.set(); return {}
    q = queue(factory); q.register("demo", fn); await q.start(); await asyncio.sleep(.05); await q.stop(5)
    assert finished.is_set() and store.list_jobs(c)[0].status == "done"

@pytest.mark.asyncio
async def test_pool_status_reports_capacity_and_pending(factory):
    c = factory(); store.enqueue(c, "demo"); store.enqueue(c, "demo")
    q = JobQueue(factory, concurrency={"demo": 3}); q.register("demo", lambda ctx: {})
    status = q.pool_status()[0]
    assert status["capacity"] == 3 and status["running"] == 0 and status["pending"] == 2


@pytest.mark.asyncio
async def test_runner_supplies_connection_factory_for_keep_alive(factory):
    conn = factory(); job_id = store.enqueue(conn, "demo"); seen = {}
    def fn(ctx):
        seen["factory"] = ctx._conn_factory
        return {}
    q = queue(factory); q.register("demo", fn)
    await q.start(); await drain(conn); await q.stop(5)
    assert store.get(conn, job_id).status == "done"
    assert seen["factory"] is factory


@pytest.mark.asyncio
async def test_error_after_cancel_marks_job_cancelled_instead_of_retrying(factory):
    """Job bị bấm hủy giữa chừng rồi handler mới lỗi: phải về 'cancelled', không
    được quay lại 'pending' retry (người dùng hủy xong job vẫn sống lại là bug)."""
    c = factory(); jid = store.enqueue(c, "demo", max_attempts=3)
    started = threading.Event()
    def fn(ctx):
        started.set()
        time.sleep(.2)
        raise RuntimeError("boom sau khi huy")
    q = queue(factory); q.register("demo", fn); await q.start()
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    store.request_cancel(c, jid); q.request_cancel(jid)
    await drain(c, 10); await q.stop(5)
    job = store.get(c, jid)
    assert job.status == "cancelled"
    assert job.attempt_count == 1


# ------------------------------------------------------------ egress không vào được

def _raise_egress(wrapped=False):
    from app.egress import EgressUnavailable
    def fn(ctx):
        if not wrapped:
            raise EgressUnavailable("server 2 chết")
        try:
            raise EgressUnavailable("server 2 chết")
        except EgressUnavailable as inner:
            raise RuntimeError("kaggle api lỗi") from inner
    return fn


async def _wait_deferred(c, jid):
    for _ in range(200):
        job = store.get(c, jid)
        if job.status == "pending" and job.error_message:
            return job
        await asyncio.sleep(.02)
    raise AssertionError("job không được hoãn")


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_egress_unavailable_defers_without_spending_attempts(factory, wrapped):
    from datetime import datetime, timedelta, timezone
    c = factory(); jid = store.enqueue(c, "demo", max_attempts=1)
    q = queue(factory); q.register("demo", _raise_egress(wrapped))
    await q.start(); job = await _wait_deferred(c, jid); await q.stop(5)
    assert job.status == "pending"          # max_attempts=1 mà không sang failed
    assert job.attempt_count == 0           # hoàn lượt
    assert "Server egress không vào được" in job.error_message
    retry_at = datetime.fromisoformat(job.next_retry_at)
    now = datetime.now(timezone.utc)
    assert now + timedelta(seconds=100) < retry_at < now + timedelta(seconds=140)
    assert job.payload["_egress_defer_count"] == 1


def test_egress_defer_backoff_doubles_and_caps_at_ten_minutes():
    from app.jobqueue.runner import egress_defer_seconds
    assert [egress_defer_seconds(n) for n in (1, 2, 3, 4, 9)] == [120, 240, 480, 600, 600]


@pytest.mark.asyncio
async def test_egress_unavailable_after_cancel_is_cancelled(factory):
    c = factory(); jid = store.enqueue(c, "demo")
    started = threading.Event()
    from app.egress import EgressUnavailable
    def fn(ctx):
        started.set(); time.sleep(.2)
        raise EgressUnavailable("down")
    q = queue(factory); q.register("demo", fn); await q.start()
    await asyncio.get_running_loop().run_in_executor(None, started.wait, 5)
    store.request_cancel(c, jid); q.request_cancel(jid)
    await drain(c, 10); await q.stop(5)
    assert store.get(c, jid).status == "cancelled"
