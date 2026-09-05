"""Kiểu dữ liệu của queue. Không import store/runner để tránh vòng lặp import."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable

PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
CANCELLING = "cancelling"
CANCELLED = "cancelled"

TERMINAL_STATUSES = frozenset({DONE, FAILED, CANCELLED})


class JobFatalError(Exception):
    """Lỗi không đáng retry: payload sai, file nguồn không tồn tại, quota đã hết.
    Handler raise cái này thì job đi thẳng sang 'failed', bỏ qua backoff."""


class JobRescheduled(Exception):
    """Job không lỗi và không xong — nó đang chờ một tài nguyên bên ngoài (quota GPU
    Kaggle) hồi phục tại một thời điểm biết trước. Khác JobFatalError/retry thường:
    không tiêu attempt_count, không dùng công thức backoff 600s-cap của store.fail."""

    def __init__(self, next_retry_at: str, message: str | None = None):
        super().__init__(message or f"rescheduled until {next_retry_at}")
        self.next_retry_at = next_retry_at
        self.message = message


def _loads(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


@dataclass
class Job:
    id: int
    job_type: str
    status: str
    priority: int
    book_id: int | None
    payload_json: str
    dedupe_key: str | None
    phase: str | None
    progress_current: int
    progress_total: int
    result_json: str | None
    error_message: str | None
    attempt_count: int
    max_attempts: int
    next_retry_at: str | None
    worker_id: str | None
    heartbeat_at: str | None
    created_at: str
    started_at: str | None
    finished_at: str | None
    updated_at: str
    patch_id: int | None = None

    @property
    def payload(self) -> dict[str, Any]:
        return _loads(self.payload_json) or {}

    @property
    def result(self) -> dict[str, Any] | None:
        return _loads(self.result_json)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(**{k: row[k] for k in row.keys()})


@dataclass
class HandlerSpec:
    job_type: str
    fn: Callable[[Any], dict[str, Any] | None]   # Callable[[JobContext], ...]
    concurrency: int
    max_attempts: int = 3
    cancellable: bool = True
