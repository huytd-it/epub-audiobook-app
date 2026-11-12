"""JobContext: ghi DB có throttle, nhưng đổi phase và lúc kết thúc thì luôn ghi."""
from __future__ import annotations

import logging

import pytest

from app import db
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.joblog import JobLogger


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    yield


def _ctx(clock, *, cancel=lambda: False):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    job_id = store.enqueue(conn, "video")
    job = store.claim(conn, "video", "video#0")
    ctx = JobContext(job, conn, JobLogger(job_id, "video"), cancel, clock=clock)
    return ctx, conn, job_id


def test_first_progress_call_writes_immediately():
    clock = _FakeClock()
    ctx, conn, job_id = _ctx(clock)
    ctx.progress(1, 10, phase="encoding")
    row = store.get(conn, job_id)
    assert (row.progress_current, row.progress_total, row.phase) == (1, 10, "encoding")


def test_rapid_progress_calls_are_throttled_to_one_write_per_second():
    clock = _FakeClock()
    ctx, conn, job_id = _ctx(clock)
    ctx.progress(1, 100, phase="encoding")     # ghi lần 1
    for i in range(2, 100):
        clock.advance(0.001)
        ctx.progress(i, 100, phase="encoding") # bị chặn hết
    assert store.get(conn, job_id).progress_current == 1
    clock.advance(1.0)
    ctx.progress(100, 100, phase="encoding")   # ghi lần 2
    assert store.get(conn, job_id).progress_current == 100


def test_a_phase_change_always_writes_even_inside_the_throttle_window():
    clock = _FakeClock()
    ctx, conn, job_id = _ctx(clock)
    ctx.progress(1, 10, phase="synthesizing")
    clock.advance(0.01)
    ctx.progress(2, 10, phase="encoding")
    row = store.get(conn, job_id)
    assert row.phase == "encoding"
    assert row.progress_current == 2


def test_flush_forces_a_write_of_the_pending_value():
    clock = _FakeClock()
    ctx, conn, job_id = _ctx(clock)
    ctx.progress(1, 10, phase="encoding")
    clock.advance(0.01)
    ctx.progress(9, 10, phase="encoding")      # bị chặn
    assert store.get(conn, job_id).progress_current == 1
    ctx.flush()
    assert store.get(conn, job_id).progress_current == 9


def test_flush_is_a_no_op_when_nothing_changed():
    clock = _FakeClock()
    ctx, conn, job_id = _ctx(clock)
    ctx.progress(5, 10, phase="encoding")
    before = store.get(conn, job_id).updated_at
    ctx.flush()
    assert store.get(conn, job_id).updated_at == before


def test_total_is_remembered_when_omitted():
    clock = _FakeClock()
    ctx, conn, job_id = _ctx(clock)
    ctx.progress(1, 42, phase="synthesizing")
    clock.advance(1.0)
    ctx.progress(2)
    assert store.get(conn, job_id).progress_total == 42


def test_heartbeat_touches_the_row_without_moving_progress():
    clock = _FakeClock()
    ctx, conn, job_id = _ctx(clock)
    ctx.progress(3, 10, phase="uploading")
    before = store.get(conn, job_id)
    clock.advance(5.0)
    ctx.heartbeat()
    after = store.get(conn, job_id)
    assert after.progress_current == 3
    assert after.heartbeat_at >= before.heartbeat_at


def test_on_write_hook_fires_on_every_db_write_and_only_then():
    """Runner dùng hook này thay cho việc bọc lại _write. Nó phải theo đúng nhịp
    throttle: gọi progress() liên tục không được làm hook nổ liên tục."""
    clock = _FakeClock()
    conn = db.connect(":memory:")
    db.init_schema(conn)
    job_id = store.enqueue(conn, "video")
    job = store.claim(conn, "video", "video#0")
    seen = []
    ctx = JobContext(
        job, conn, JobLogger(job_id, "video"), lambda: False, clock=clock,
        on_write=lambda current, total, phase: seen.append((current, total, phase)),
    )

    ctx.progress(1, 10, phase="encoding")      # ghi
    clock.advance(0.01)
    ctx.progress(2, 10, phase="encoding")      # bị chặn
    clock.advance(1.0)
    ctx.progress(3, 10, phase="encoding")      # ghi

    assert seen == [(1, 10, "encoding"), (3, 10, "encoding")]


def test_should_cancel_reflects_the_supplied_check():
    clock = _FakeClock()
    flag = {"stop": False}
    ctx, _, _ = _ctx(clock, cancel=lambda: flag["stop"])
    assert ctx.should_cancel() is False
    flag["stop"] = True
    assert ctx.should_cancel() is True


def test_losing_ownership_flips_should_cancel(tmp_path):
    """Job bị reap rồi worker khác claim: lần ghi tiến độ tiếp theo bị rào chặn, và
    handler được báo dừng qua should_cancel()."""
    clock = _FakeClock()
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    job_id = store.enqueue(conn, "video")
    job = store.claim(conn, "video", "video#A")
    ctx = JobContext(job, conn, JobLogger(job_id, "video"), lambda: False, clock=clock)

    ctx.progress(1, 10, phase="encoding")
    assert ctx.should_cancel() is False
    assert ctx.lost_ownership() is False

    conn.execute("UPDATE job SET worker_id='video#B' WHERE id=?", (job_id,))
    conn.commit()
    clock.advance(1.0)
    ctx.progress(2, 10, phase="encoding")

    assert ctx.lost_ownership() is True
    assert ctx.should_cancel() is True
    assert store.get(conn, job_id).progress_current == 1   # ghi của A không lọt qua


def test_keep_alive_beats_while_a_long_step_runs(tmp_path):
    import time as real_time
    conn = db.connect(str(tmp_path / "a.db"))
    db.init_schema(conn)
    job_id = store.enqueue(conn, "video")
    job = store.claim(conn, "video", "video#A")
    conn.execute("UPDATE job SET heartbeat_at=NULL WHERE id=?", (job_id,))
    conn.commit()
    ctx = JobContext(
        job, conn, JobLogger(job_id, "video"), lambda: False,
        conn_factory=lambda: db.connect(str(tmp_path / "a.db")),
    )

    with ctx.keep_alive(interval=0.05):
        real_time.sleep(0.25)

    assert store.get(conn, job_id).heartbeat_at is not None


def test_keep_alive_is_a_no_op_without_a_conn_factory(tmp_path):
    """Test nào không quan tâm tới nhịp tim thì không phải dựng connection factory."""
    clock = _FakeClock()
    ctx, _, _ = _ctx(clock)
    with ctx.keep_alive(interval=0.01):
        pass


def test_log_and_emit_reach_the_job_log_file():
    from app.jobqueue import joblog
    clock = _FakeClock()
    ctx, _, job_id = _ctx(clock)
    ctx.progress(1, 3, phase="encoding")
    ctx.log("đang ghép audio")
    ctx.emit({"type": "chunk", "index": 0})
    ctx.close()
    text = joblog.tail(job_id)
    assert "đang ghép audio" in text
    assert "phase=encoding" in text
    events, _ = joblog.read_events(job_id)
    assert events == [{"type": "chunk", "index": 0}]


def test_progress_with_none_changes_phase_but_keeps_the_numbers():
    ctx, conn, job_id = _ctx(_FakeClock())
    ctx.progress(4, 10, phase="synthesizing")

    ctx.progress(None, phase="merging_book")

    row = store.get(conn, job_id)
    assert (row.progress_current, row.progress_total, row.phase) == (4, 10, "merging_book")
