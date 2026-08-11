"""Integration coverage for per-type queue concurrency and failure isolation."""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from app import db
from app.jobqueue import store
from app.jobqueue.runner import JobQueue


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_root", str(tmp_path))


@pytest.fixture
def conn_factory(tmp_path):
    path = str(tmp_path / "queue.db")
    setup = db.connect(path)
    db.init_schema(setup)
    setup.close()
    return lambda: db.connect(path)


async def _drain(conn, timeout=30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        row = conn.execute("SELECT COUNT(*) AS c FROM job WHERE status IN ('pending','running')").fetchone()
        if row["c"] == 0:
            return
        await asyncio.sleep(0.02)
    raise AssertionError("queue did not drain")


@pytest.mark.asyncio
async def test_each_type_respects_its_own_cap(conn_factory):
    conn = conn_factory()
    plan = {"audiobook_tts": 4, "video": 6, "youtube_upload": 3, "light_tts": 30}
    for job_type, count in plan.items():
        for _ in range(count):
            store.enqueue(conn, job_type)

    peaks = {k: 0 for k in plan}
    live = {k: 0 for k in plan}
    lock = threading.Lock()

    def make(job_type):
        def handler(ctx):
            with lock:
                live[job_type] += 1
                peaks[job_type] = max(peaks[job_type], live[job_type])
            time.sleep(0.03)
            with lock:
                live[job_type] -= 1
            return {}
        return handler

    queue = JobQueue(conn_factory, concurrency={"audiobook_tts": 1, "video": 2, "youtube_upload": 1},
                     default_concurrency=10, poll_interval=0.01, reap_after_seconds=120)
    for job_type in plan:
        queue.register(job_type, make(job_type))
    await queue.start()
    await _drain(conn)
    await queue.stop(timeout=10)

    assert peaks["audiobook_tts"] == 1
    assert peaks["video"] <= 2
    assert peaks["youtube_upload"] == 1
    assert peaks["light_tts"] <= 10
    assert peaks["light_tts"] > 1
    assert all(j.status == "done" for j in store.list_jobs(conn, limit=100))


@pytest.mark.asyncio
async def test_a_crashing_type_does_not_stall_the_others(conn_factory):
    conn = conn_factory()
    for _ in range(3):
        store.enqueue(conn, "bad", max_attempts=1)
    for _ in range(5):
        store.enqueue(conn, "good")

    queue = JobQueue(conn_factory, concurrency={}, default_concurrency=4, poll_interval=0.01,
                     reap_after_seconds=120)
    queue.register("bad", lambda ctx: (_ for _ in ()).throw(RuntimeError("always fails")))
    queue.register("good", lambda ctx: {"ok": True})
    await queue.start()
    deadline = asyncio.get_running_loop().time() + 15
    while asyncio.get_running_loop().time() < deadline:
        if len(store.list_jobs(conn, job_type="good", status="done")) == 5:
            break
        await asyncio.sleep(0.02)
    await queue.stop(timeout=10)
    assert len(store.list_jobs(conn, job_type="good", status="done")) == 5
