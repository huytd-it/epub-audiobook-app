"""Một file log cho mỗi job, dưới <data_root>/logs/jobs/<job_id>.log.

Hai loại dòng sống chung trong cùng file:

    2026-07-29T10:22:31Z [INFO ] phase=encoding | ffmpeg pass 1/2, 34%
    @@EVENT {"type":"chunk","index":7,"total":42}

Dòng thường để người đọc; dòng @@EVENT là dữ liệu có cấu trúc cho cầu SSE
(app/routes/text_studio.py). Một file, một cơ chế — không cần stream thứ hai.

Dòng WARNING trở lên được nhân bản sang logger chính nên app.log vẫn là cái nhìn
toàn cục; dòng INFO/DEBUG chỉ nằm ở file riêng để app.log không bị ngập."""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, TextIO

from app.config import settings

logger = logging.getLogger(__name__)

EVENT_PREFIX = "@@EVENT "

_LEVEL_NAMES = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO ",
    logging.WARNING: "WARN ",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "FATAL",
}


def jobs_log_dir() -> Path:
    return Path(settings.data_root) / "logs" / "jobs"


def job_log_path(job_id: int) -> Path:
    return jobs_log_dir() / f"{job_id}.log"


class JobLogger:
    """Mở file lười ở lần ghi đầu tiên, nối tiếp nếu file đã có (job retry thì log
    của lần chạy trước vẫn còn — cố ý, để so được hai lần chạy)."""

    def __init__(self, job_id: int, job_type: str):
        self.job_id = job_id
        self.job_type = job_type
        self._fh: TextIO | None = None
        self._lock = threading.Lock()

    def _handle(self) -> TextIO:
        if self._fh is None:
            path = job_log_path(self.job_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = path.open("a", encoding="utf-8")
        return self._fh

    def _write(self, line: str) -> None:
        with self._lock:
            fh = self._handle()
            fh.write(line + "\n")
            fh.flush()   # cầu SSE tail file này realtime; buffer sẽ làm tiến độ đứng hình

    def log(self, message: str, level: int = logging.INFO, phase: str | None = None) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        label = _LEVEL_NAMES.get(level, "INFO ")
        prefix = f"phase={phase} | " if phase else ""
        self._write(f"{stamp} [{label}] {prefix}{message}")
        if level >= logging.WARNING:
            logger.log(
                level,
                "event=job.log job_id=%s job_type=%s phase=%s message=%s",
                self.job_id, self.job_type, phase or "", message,
            )

    def emit(self, event: dict[str, Any]) -> None:
        """Ghi một event có cấu trúc. Dùng separators gọn và ensure_ascii=False để
        tiếng Việt trong message lỗi đọc được bằng mắt ngay trong file log."""
        self._write(EVENT_PREFIX + json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def tail(job_id: int, lines: int = 500) -> str:
    # `lines` đến từ query param ?tail= của /queue/jobs/{id}/log, nên phải chặn dưới:
    # all_lines[-0:] là CẢ file chứ không phải rỗng, và đó là bẫy slice âm quen thuộc.
    if lines <= 0:
        return ""
    path = job_log_path(job_id)
    if not path.is_file():
        return ""
    try:
        all_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    return "\n".join(all_lines[-lines:]) + "\n" if all_lines else ""


def read_events(job_id: int, *, from_line: int = 0) -> tuple[list[dict[str, Any]], int]:
    """Đọc các dòng @@EVENT kể từ dòng thứ `from_line` (đếm theo dòng @@EVENT, không
    phải dòng file). Trả về (events, cursor mới) để cầu SSE poll tiếp mà không đọc lại.

    Đếm theo dòng event chứ không theo byte offset: file có cả dòng chữ thường xen
    kẽ, và một cursor byte sẽ vỡ nếu dòng cuối được ghi dở lúc đang đọc."""
    path = job_log_path(job_id)
    if not path.is_file():
        return [], from_line
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], from_line
    raw = text.splitlines()
    # Dòng cuối chưa có '\n' là dòng đang được ghi dở. Bỏ nó ra khỏi lượt này —
    # nếu đếm nó vào `seen` mà không parse được, cursor sẽ nhảy qua và lần đọc sau
    # bỏ luôn dòng đó dù lúc ấy nó đã hoàn chỉnh. Event mất hẳn, không phải chậm.
    if raw and not text.endswith("\n"):
        raw.pop()
    events: list[dict[str, Any]] = []
    seen = 0
    for line in raw:
        if not line.startswith(EVENT_PREFIX):
            continue
        seen += 1
        if seen <= from_line:
            continue
        try:
            parsed = json.loads(line[len(EVENT_PREFIX):])
        except ValueError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events, seen


def purge_old_logs(conn: sqlite3.Connection, *, retention_days: int | None = None) -> int:
    """Xóa file log của job đã kết thúc quá hạn. Trả về số file đã xóa.
    Job chưa kết thúc (finished_at IS NULL) không bao giờ bị đụng vào."""
    days = settings.queue_log_retention_days if retention_days is None else retention_days
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT id FROM job WHERE finished_at IS NOT NULL AND finished_at < ?", (cutoff,)
    ).fetchall()
    removed = 0
    for row in rows:
        path = job_log_path(row["id"])
        if path.is_file():
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                logger.warning("không xóa được log job %s: %s", row["id"], exc)
    return removed
