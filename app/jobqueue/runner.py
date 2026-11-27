from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import traceback
from datetime import datetime, timedelta, timezone

from . import store
from .context import JobContext
from .joblog import JobLogger
from .models import CANCELLING, HandlerSpec, JobFatalError, JobRescheduled

# Số lần liên tiếp job bị hoãn vì server egress, lưu trong payload để tính backoff.
EGRESS_DEFER_KEY = "_egress_defer_count"
_EGRESS_BASE_DEFER_SECONDS = 120
_EGRESS_MAX_DEFER_SECONDS = 600


def egress_defer_seconds(count: int) -> int:
    """Backoff hoãn job khi server egress chết: 2, 4, 8, 10, 10... phút."""
    return min(_EGRESS_BASE_DEFER_SECONDS * 2 ** max(count - 1, 0), _EGRESS_MAX_DEFER_SECONDS)


def _egress_unavailable(exc: BaseException | None):
    """EgressUnavailable nằm ở exc hoặc trong chuỗi __cause__/__context__ (None nếu không)."""
    from app.egress import unavailable_in
    return unavailable_in(exc)


def parse_concurrency(spec: str, *, default: int) -> dict[str, int]:
    out = {}
    for part in (spec or "").split(","):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        try:
            value = int(value)
        except ValueError:
            continue
        if value >= 0 and name.strip():
            out[name.strip()] = value
    return out


class _Tracker:
    def __init__(self, job):
        self.job = job
        self.current = 0
        self.total = 0
        self.ctx = None


class JobQueue:
    def __init__(self, conn_factory, *, concurrency, default_concurrency=10,
                 poll_interval=2.0, reap_after_seconds=120, is_paused=None):
        self._conn_factory = conn_factory
        self._concurrency = dict(concurrency)
        self._default = default_concurrency
        self._poll = poll_interval
        self._reap = reap_after_seconds
        self._is_paused = is_paused
        self._paused = False
        self._specs = {}
        self._tasks = []
        self._inflight = set()
        self._executor = None
        self._stop = None
        self._lock = threading.Lock()
        self._running = {}
        self._cancel = set()
        self.last_heartbeat_at = datetime.now(timezone.utc).isoformat()

    def capacity(self, job_type):
        return self._concurrency.get(job_type, self._default)

    def register(self, job_type, fn, *, concurrency=None, max_attempts=3, cancellable=True):
        if concurrency is not None:
            self._concurrency[job_type] = concurrency
        self._specs[job_type] = HandlerSpec(job_type, fn, self.capacity(job_type), max_attempts, cancellable)

    async def start(self):
        self._stop = asyncio.Event()
        total = sum(s.concurrency for s in self._specs.values()) or 1
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=total + 4)
        self._tasks = [asyncio.create_task(self._supervise(t)) for t in self._specs]
        self._tasks.append(asyncio.create_task(self._reaper()))

    async def stop(self, timeout):
        self._stop.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._inflight:
            await asyncio.wait(set(self._inflight), timeout=timeout)
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None

    def request_cancel(self, job_id):
        with self._lock:
            self._cancel.add(job_id)

    async def _supervise(self, job_type):
        """Giữ đúng một dispatcher sống cho mỗi loại job suốt vòng đời tiến trình.

        Một dispatcher thoát/raise ngoài dự kiến sẽ bỏ rơi pool của nó vĩnh viễn:
        /queue vẫn báo capacity = N, job vẫn 'pending', mà không ai nhặt nữa. Supervisor
        khởi động lại nó (backoff ngắn) để pool luôn được lấp đầy thay vì âm thầm mất
        throughput khi worker còn dư."""
        if self.capacity(job_type) <= 0:
            return
        while not self._stop.is_set():
            try:
                await self._dispatch(job_type)
                return  # stop đã bật, hoặc capacity bị tắt — thoát sạch
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.exception("event=queue.dispatch.restarted job_type=%s", job_type)
                await asyncio.sleep(min(self._poll, 1.0))

    async def _dispatch(self, job_type):
        capacity = self.capacity(job_type)
        if capacity <= 0:
            return
        sem = asyncio.Semaphore(capacity)
        conn = self._conn_factory()
        sequence = 0
        try:
            while not self._stop.is_set():
                self.last_heartbeat_at = datetime.now(timezone.utc).isoformat()
                # Đọc trạng thái pause và claim đều có thể vấp DB đang bị khóa: phải
                # làm chậm vòng lặp chứ không được giết nó. Dispatcher chết = pool rảnh
                # dù job còn chờ.
                try:
                    paused = bool(self._is_paused and self._is_paused(conn))
                except Exception:
                    logging.exception(
                        "event=queue.dispatch.pause_check_failed job_type=%s", job_type)
                    paused = False
                self._paused = paused
                if paused:
                    await asyncio.sleep(self._poll)
                    continue
                await sem.acquire()
                sequence += 1
                try:
                    job = store.claim(conn, job_type, f"{job_type}#{sequence}")
                except Exception:
                    logging.exception(
                        "event=queue.dispatch.claim_failed job_type=%s", job_type)
                    # Nhả slot đã xin (nếu không sẽ rò semaphore) và mở lại connection
                    # vì lần lỗi này có thể là DB hỏng/khóa, không phải chỉ một câu SQL.
                    sem.release()
                    conn = self._reconnect(conn)
                    await asyncio.sleep(self._poll)
                    continue
                if job is None:
                    sem.release()
                    await asyncio.sleep(self._poll)
                    continue
                task = asyncio.create_task(self._run(self._specs[job_type], job, sem))
                self._inflight.add(task)
                task.add_done_callback(self._inflight.discard)
        except asyncio.CancelledError:
            pass
        finally:
            conn.close()

    def _reconnect(self, conn):
        try:
            conn.close()
        except Exception:
            pass
        return self._conn_factory()

    async def _reaper(self):
        try:
            while not self._stop.is_set():
                await asyncio.sleep(max(self._poll, 5))
                conn = self._conn_factory()
                try:
                    store.reap_stale(conn, older_than_seconds=self._reap)
                except Exception:
                    # Một lần reap lỗi (DB khóa) không được giết reaper: chết rồi thì
                    # job 'running' treo sẽ không bao giờ được trả về 'pending' nữa.
                    logging.exception("event=queue.reaper.failed")
                finally:
                    conn.close()
        except asyncio.CancelledError:
            pass

    async def _run(self, spec, job, sem):
        tracker = _Tracker(job)
        with self._lock:
            self._running[job.id] = tracker
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._execute, spec, job, tracker)
        finally:
            with self._lock:
                self._running.pop(job.id, None)
                self._cancel.discard(job.id)
            sem.release()

    def _execute(self, spec, job, tracker):
        conn = self._conn_factory()
        logger = JobLogger(job.id, job.job_type)
        ctx = JobContext(
            job, conn, logger, lambda: self._cancelled(conn, job.id),
            conn_factory=self._conn_factory,
        )
        tracker.ctx = ctx
        original_write = ctx._write
        def tracked_write(now):
            original_write(now)
            tracker.current = ctx._current
            tracker.total = ctx._total
        ctx._write = tracked_write
        try:
            ctx.log(
                f"Bắt đầu attempt {job.attempt_count}/{job.max_attempts}; "
                f"worker={job.worker_id}; payload={job.payload}"
            )
            result = spec.fn(ctx)
            ctx.flush()
            if self._cancelled(conn, job.id):
                ctx.log("Tác vụ đã bị hủy", level=logging.WARNING)
                store.mark_cancelled(conn, job.id, worker_id=job.worker_id)
            else:
                ctx.log(f"Tác vụ hoàn tất; result={result}")
                store.finish(
                    conn, job.id, result if isinstance(result, dict) else None,
                    worker_id=job.worker_id,
                )
        except asyncio.CancelledError:
            ctx.log("Tác vụ bị hủy bởi worker", level=logging.WARNING)
            ctx.flush(); store.mark_cancelled(conn, job.id, worker_id=job.worker_id)
        except JobFatalError as exc:
            ctx.log(traceback.format_exc(), level=logging.ERROR)
            ctx.flush()
            if self._cancelled(conn, job.id):
                store.mark_cancelled(conn, job.id, worker_id=job.worker_id)
            else:
                store.fail(
                    conn, job.id, str(exc), fatal=True, worker_id=job.worker_id,
                )
        except JobRescheduled as exc:
            ctx.log(f"Hoãn tới {exc.next_retry_at}: {exc.message or exc}", level=logging.INFO)
            ctx.flush()
            if self._cancelled(conn, job.id):
                store.mark_cancelled(conn, job.id, worker_id=job.worker_id)
            else:
                store.reschedule(
                    conn, job.id, exc.next_retry_at, exc.message, worker_id=job.worker_id,
                )
        except Exception as exc:
            ctx.log(traceback.format_exc(), level=logging.ERROR)
            # Job đã bị bấm hủy mà còn lỗi: tôn trọng yêu cầu hủy thay vì đưa về
            # pending retry (người dùng hủy xong job vẫn sống lại là bug).
            ctx.flush()
            egress_exc = _egress_unavailable(exc)
            if self._cancelled(conn, job.id):
                store.mark_cancelled(conn, job.id, worker_id=job.worker_id)
            elif egress_exc is not None:
                # Server egress không vào được: không phải lỗi của job. Hoãn (không
                # tính lượt, chờ vô hạn) với backoff 2 → 10 phút; không bao giờ tự
                # đi thẳng (fail-closed).
                self._defer_for_egress(conn, job, ctx, egress_exc)
            else:
                # The enqueue request may set a per-job retry policy. HandlerSpec is
                # only the default; do not overwrite the persisted max_attempts.
                store.fail(
                    conn, job.id, str(exc), max_attempts=job.max_attempts,
                    worker_id=job.worker_id,
                )
        finally:
            ctx.close(); conn.close()

    def _defer_for_egress(self, conn, job, ctx, exc):
        current = store.get(conn, job.id)
        payload = dict(current.payload if current is not None else job.payload)
        count = int(payload.get(EGRESS_DEFER_KEY) or 0) + 1
        payload[EGRESS_DEFER_KEY] = count
        delay = egress_defer_seconds(count)
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
        store.update_payload(conn, job.id, payload, worker_id=job.worker_id)
        message = f"Server egress không vào được – hoãn tới {retry_at}: {exc}"
        ctx.log(message, level=logging.WARNING)
        ctx.flush()
        store.reschedule(conn, job.id, retry_at, message[:4000], worker_id=job.worker_id)

    def _cancelled(self, conn, job_id):
        with self._lock:
            if job_id in self._cancel:
                return True
        row = conn.execute("SELECT status FROM job WHERE id=?", (job_id,)).fetchone()
        return bool(row and row["status"] == CANCELLING)

    def job_detail(self, job_id):
        """Chi tiết tiến độ trong bộ nhớ của job đang chạy (None nếu không có)."""
        with self._lock:
            tracker = self._running.get(job_id)
        ctx = tracker.ctx if tracker else None
        return dict(ctx.detail) if ctx is not None and ctx.detail else None

    def pool_status(self):
        conn = self._conn_factory()
        try:
            # voxcpm_tts is a read-only compatibility alias for persisted pre-rename
            # jobs. Keep it executable without exposing it as a separate queue pool.
            visible_types = (t for t in sorted(self._specs) if t != "voxcpm_tts")
            return [{"job_type": t, "running": sum(r.job.job_type == t for r in self._running.values()),
                     "capacity": self.capacity(t), "pending": store.pending_count(conn, t)} for t in visible_types]
        finally:
            conn.close()

    @property
    def state(self):
        return "paused" if self._paused else ("busy" if self._running else "idle")

    def _audiobook_tts(self):
        return next((
            r for r in self._running.values()
            if r.job.job_type in {"audiobook_tts", "audiobook_tts_api", "voxcpm_tts"}
        ), None)

    @property
    def current_patch_id(self):
        tracker = self._audiobook_tts()
        return tracker.job.payload.get("patch_id") if tracker else None

    @property
    def current_chunk_index(self):
        tracker = self._audiobook_tts()
        return tracker.current if tracker else 0

    @property
    def current_chunk_count(self):
        tracker = self._audiobook_tts()
        return tracker.total if tracker else 0
