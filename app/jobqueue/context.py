"""Thứ duy nhất handler nhìn thấy. Cách ly handler khỏi runner: handler không biết
gì về asyncio, semaphore hay dispatcher — chỉ báo tiến độ, ghi log, và hỏi xem có bị
bảo dừng không."""
from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable

from app.jobqueue import store
from app.jobqueue.joblog import JobLogger
from app.jobqueue.models import Job


class JobContext:
    def __init__(
        self,
        job: Job,
        conn: sqlite3.Connection,
        logger: JobLogger,
        cancel_check: Callable[[], bool],
        *,
        flush_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        on_write: Callable[[int, int, str | None], None] | None = None,
        conn_factory: Callable[[], sqlite3.Connection] | None = None,
    ):
        self.job = job
        self.conn = conn
        self._logger = logger
        self._cancel_check = cancel_check
        self._flush_interval = flush_interval
        self._clock = clock
        # Runner dùng hook này để mirror tiến độ vào tracker trong bộ nhớ, tránh cho
        # /health phải đọc DB mỗi lần được gọi. Không có nó, runner sẽ phải thò tay
        # vào thuộc tính private của lớp này.
        self._on_write = on_write
        # keep_alive() cần connection riêng: thread giữ nhịp tim không được dùng chung
        # self.conn với handler đang chạy (sqlite3 connection không an toàn khi hai
        # thread dùng cùng lúc). None => keep_alive thành no-op, dùng trong test.
        self._conn_factory = conn_factory
        self._lost_ownership = False

        self._current = job.progress_current
        self._total = job.progress_total
        self._phase = job.phase
        self._written = (self._current, self._total, self._phase)
        self._last_write_at: float | None = None

    # ------------------------------------------------------------- tiến độ

    def progress(
        self, current: int | None, total: int | None = None, phase: str | None = None
    ) -> None:
        """Luôn cập nhật trong bộ nhớ; chỉ chạm DB khi đã quá flush_interval kể từ lần
        ghi trước, hoặc khi phase đổi. Gọi cái này mỗi chunk/mỗi frame đều an toàn.
        current=None giữ nguyên con số đang có — dùng khi chỉ muốn đổi phase."""
        if current is not None:
            self._current = current
        if total is not None:
            self._total = total
        phase_changed = phase is not None and phase != self._phase
        if phase is not None:
            self._phase = phase

        now = self._clock()
        due = self._last_write_at is None or (now - self._last_write_at) >= self._flush_interval
        if phase_changed or due:
            self._write(now)

    def heartbeat(self) -> None:
        """Báo còn sống khi không có tiến độ mới để báo — handler nào chạy một bước
        dài (upload một file lớn) phải gọi cái này, nếu không reaper sẽ tưởng nó chết."""
        if not store.heartbeat(self.conn, self.job.id, worker_id=self.job.worker_id):
            self._lost_ownership = True
        self._last_write_at = self._clock()

    @contextmanager
    def keep_alive(self, interval: float = 30.0):
        """Giữ nhịp tim trong suốt một bước dài không tự báo tiến độ được — encode
        ffmpeg, hay một lần upload YouTube. Không có nó, reaper (mặc định 120s) sẽ
        tưởng worker đã chết, trả job về 'pending', và một worker thứ hai chạy lại
        đúng công việc đó.

        Thread giữ nhịp mở connection riêng mỗi nhịp: sqlite3 connection không an toàn
        khi hai thread dùng cùng lúc, và handler đang giữ self.conn."""
        if self._conn_factory is None:
            yield
            return

        stop = threading.Event()

        def _beat() -> None:
            while not stop.wait(interval):
                try:
                    conn = self._conn_factory()
                    try:
                        alive = store.heartbeat(
                            conn, self.job.id, worker_id=self.job.worker_id)
                    finally:
                        conn.close()
                except sqlite3.Error:
                    continue      # một nhịp lỡ không đáng làm hỏng job
                if not alive:
                    self._lost_ownership = True
                    return        # job không còn của mình: ngừng đập, để handler tự dừng

        thread = threading.Thread(target=_beat, name=f"keepalive-{self.job.id}", daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join(timeout=5.0)

    def flush(self, *, force: bool = True) -> None:
        """Đẩy giá trị đang treo xuống DB. Runner gọi khi job kết thúc, nên con số
        cuối cùng luôn khớp thực tế dù lần progress() cuối rơi vào cửa sổ throttle."""
        if not force:
            return
        if (self._current, self._total, self._phase) != self._written:
            self._write(self._clock())

    def _write(self, now: float) -> None:
        if not store.write_progress(
            self.conn, self.job.id,
            current=self._current, total=self._total, phase=self._phase,
            worker_id=self.job.worker_id,
        ):
            # Job đã bị reap và giao cho worker khác. Ghi tiếp là đè lên lượt chạy của
            # họ, nên dừng lại và để should_cancel() bảo handler thoát.
            self._lost_ownership = True
        self._written = (self._current, self._total, self._phase)
        self._last_write_at = now
        if self._on_write is not None:
            self._on_write(self._current, self._total, self._phase)

    # ----------------------------------------------------------------- log

    def log(self, message: str, level: int = logging.INFO) -> None:
        self._logger.log(message, level=level, phase=self._phase)

    def emit(self, event: dict[str, Any]) -> None:
        self._logger.emit(event)

    def close(self) -> None:
        self._logger.close()

    # ----------------------------------------------------------------- hủy

    def should_cancel(self) -> bool:
        """True khi có người bấm hủy, HOẶC khi job đã không còn thuộc về worker này.
        Gộp hai thứ vào một cờ để handler chỉ phải kiểm một chỗ ở ranh giới chunk."""
        return self._lost_ownership or self._cancel_check()

    def lost_ownership(self) -> bool:
        """Runner dùng để phân biệt 'bị hủy' với 'bị reap rồi cướp mất'."""
        return self._lost_ownership
