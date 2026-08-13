"""Mọi câu SQL của queue. Không phụ thuộc asyncio, không phụ thuộc FastAPI —
gọi được từ bất kỳ thread nào, với bất kỳ connection nào."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from app.jobqueue.models import (
    CANCELLED, CANCELLING, FAILED, PENDING, TERMINAL_STATUSES, Job,
)

_BASE_BACKOFF_SECONDS = 30
_MAX_BACKOFF_SECONDS = 600

_COLUMNS = (
    "id, job_type, status, priority, book_id, payload_json, dedupe_key, phase, "
    "progress_current, progress_total, result_json, error_message, attempt_count, "
    "max_attempts, next_retry_at, worker_id, heartbeat_at, created_at, started_at, "
    "finished_at, updated_at"
    ", flow_run_id, node_id, patch_id"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def backoff_seconds(attempt: int) -> int:
    """30·2^attempt, chặn trên ở 600s. attempt là attempt_count sau khi đã tăng,
    nên lần hỏng đầu tiên (attempt=1) đợi 60 giây."""
    return min(_BASE_BACKOFF_SECONDS * (2 ** max(1, attempt)), _MAX_BACKOFF_SECONDS)


# ---------------------------------------------------------------- enqueue / đọc

def enqueue(
    conn: sqlite3.Connection,
    job_type: str,
    *,
    payload: dict | None = None,
    book_id: int | None = None,
    dedupe_key: str | None = None,
    priority: int = 100,
    max_attempts: int = 3,
    flow_run_id: int | None = None,
    node_id: str | None = None,
    patch_id: int | None = None,
    depends_on: int | None = None,
) -> int | None:
    """Trả về id job mới, hoặc None nếu đã có job cùng dedupe_key đang pending/running.

    Không tự kiểm tra trước rồi mới insert — chạy thẳng INSERT và bắt IntegrityError,
    vì partial unique index mới là thứ duy nhất đúng khi có nhiều tiến trình cùng gọi."""
    payload = payload or {}
    payload_patch_id = payload.get("patch_id")
    if patch_id is not None and payload_patch_id is not None and patch_id != payload_patch_id:
        raise ValueError("patch_id does not match payload.patch_id")
    patch_id = patch_id if patch_id is not None else payload_patch_id
    # job.patch_id có FK tới patch; patch không tồn tại thì bỏ patch_id (NULL) — handler
    # sẽ phát hiện "patch không tồn tại" và fatal như thiết kế, thay vì enqueue bị chặn.
    if patch_id is not None and conn.execute(
        "SELECT 1 FROM patch WHERE id=?", (patch_id,)
    ).fetchone() is None:
        patch_id = None
    now = _now()
    try:
        cur = conn.execute(
            """INSERT INTO job (job_type, status, priority, book_id, payload_json,
                                dedupe_key, max_attempts, created_at, updated_at,
                                flow_run_id, node_id, patch_id)
               VALUES (?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (job_type, priority, book_id, json.dumps(payload), dedupe_key,
             max_attempts, now, now, flow_run_id, node_id, patch_id),
        )
    except sqlite3.IntegrityError:
        conn.rollback()
        return None
    job_id = cur.lastrowid
    if depends_on is not None:
        conn.execute(
            "INSERT INTO job_dependency (job_id, depends_on_job_id) VALUES (?, ?)",
            (job_id, depends_on),
        )
    conn.commit()
    return job_id


def get(conn: sqlite3.Connection, job_id: int) -> Job | None:
    row = conn.execute(f"SELECT {_COLUMNS} FROM job WHERE id=?", (job_id,)).fetchone()
    return Job.from_row(row) if row else None


def find_live_by_dedupe(conn: sqlite3.Connection, dedupe_key: str) -> Job | None:
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM job WHERE dedupe_key=? AND status IN ('pending','running')",
        (dedupe_key,),
    ).fetchone()
    return Job.from_row(row) if row else None


def update_payload(
    conn: sqlite3.Connection, job_id: int, payload: dict, *,
    worker_id: str | None = None,
) -> bool:
    """Replace a job's payload_json. Fenced by worker_id like every other
    write, so a worker that no longer owns the job cannot overwrite the
    payload of its successor.

    Handlers use this to persist a one-time random draw (e.g. a patch
    background's style/scene/seed) on the first attempt, so a retry of the
    same job reuses the same values instead of re-rolling.
    """
    stamp = _now()
    guard, extra = _fence(worker_id)
    cur = conn.execute(
        f"UPDATE job SET payload_json=?, updated_at=? WHERE id=?{guard}",
        [json.dumps(payload), stamp, job_id] + extra,
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------- claim

def claim(
    conn: sqlite3.Connection, job_type: str, worker_id: str, *, now: str | None = None
) -> Job | None:
    """Một câu UPDATE nguyên tử. Điều kiện `AND status='pending'` ở ngoài là chốt chặn
    thứ hai: nếu một claim khác đã đổi trạng thái giữa lúc subquery chọn id và lúc
    UPDATE ghi, câu này khớp 0 dòng và trả None thay vì cướp job."""
    stamp = now or _now()
    row = conn.execute(
        f"""UPDATE job
               SET status='running', worker_id=?, started_at=COALESCE(started_at, ?),
                   heartbeat_at=?, attempt_count=attempt_count+1, error_message=NULL,
                   next_retry_at=NULL, updated_at=?
             WHERE id=(SELECT id FROM job
                         WHERE status='pending' AND job_type=?
                           AND (next_retry_at IS NULL OR next_retry_at<=?)
                           AND NOT EXISTS (
                               SELECT 1 FROM job_dependency d
                               JOIN job upstream ON upstream.id=d.depends_on_job_id
                               WHERE d.job_id=job.id AND upstream.status!='done'
                           )
                        ORDER BY priority, id LIMIT 1)
               AND status='pending'
         RETURNING {_COLUMNS}""",
        (worker_id, stamp, stamp, stamp, job_type, stamp),
    ).fetchone()
    conn.commit()
    return Job.from_row(row) if row else None


# ------------------------------------------------------------ tiến độ / nhịp tim

def _fence(worker_id: str | None) -> tuple[str, list]:
    """Mảnh WHERE tùy chọn rào theo chủ sở hữu. Truyền worker_id thì câu UPDATE chỉ
    khớp khi job vẫn thuộc về worker đó — worker đã bị reap ghi vào là no-op."""
    return (" AND worker_id=?", [worker_id]) if worker_id is not None else ("", [])


def write_progress(
    conn: sqlite3.Connection, job_id: int, *, current: int, total: int,
    phase: str | None, now: str | None = None, worker_id: str | None = None,
) -> bool:
    stamp = now or _now()
    guard, extra = _fence(worker_id)
    cur = conn.execute(
        f"""UPDATE job SET progress_current=?, progress_total=?, phase=?,
                           heartbeat_at=?, updated_at=? WHERE id=?{guard}""",
        [current, total, phase, stamp, stamp, job_id] + extra,
    )
    conn.commit()
    return cur.rowcount > 0


def heartbeat(
    conn: sqlite3.Connection, job_id: int, *, now: str | None = None,
    worker_id: str | None = None,
) -> bool:
    stamp = now or _now()
    guard, extra = _fence(worker_id)
    cur = conn.execute(
        f"UPDATE job SET heartbeat_at=?, updated_at=? WHERE id=?{guard}",
        [stamp, stamp, job_id] + extra,
    )
    conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------------- kết thúc

def finish(
    conn: sqlite3.Connection, job_id: int, result: dict | None = None, *,
    worker_id: str | None = None,
) -> bool:
    now = _now()
    guard, extra = _fence(worker_id)
    cur = conn.execute(
        f"""UPDATE job SET status='done', result_json=?, error_message=NULL,
                           finished_at=?, heartbeat_at=?, updated_at=? WHERE id=?{guard}""",
        [json.dumps(result) if result is not None else None, now, now, now, job_id] + extra,
    )
    conn.commit()
    return cur.rowcount > 0


def fail(
    conn: sqlite3.Connection, job_id: int, error: str, *, fatal: bool = False,
    max_attempts: int | None = None, worker_id: str | None = None,
) -> str | None:
    """Trả về trạng thái mới: 'pending' nếu còn lượt retry, 'failed' nếu hết
    (hoặc fatal=True). Trả về None khi bị rào chặn — job đã không còn thuộc về
    worker_id truyền vào, nên lần ghi này bị bỏ qua. Cắt error về 4000 ký tự:
    traceback của ffmpeg có thể rất dài và cột này được đọc trên mọi trang danh sách.

    `max_attempts` cho phép runner áp số của HandlerSpec, đè lên giá trị đã lưu trên
    dòng job. Cần thiết vì job có thể được enqueue trước khi handler đăng ký (backfill,
    hoặc một bản cũ của app), và số đúng phải là số của handler đang chạy."""
    now = _now()
    job = get(conn, job_id)
    if job is None:
        return None
    if worker_id is not None and job.worker_id != worker_id:
        return None
    message = (error or "")[:4000]
    limit = job.max_attempts if max_attempts is None else max_attempts
    guard, extra = _fence(worker_id)
    if not fatal and job.attempt_count < limit:
        retry_at = (
            datetime.now(timezone.utc) + timedelta(seconds=backoff_seconds(job.attempt_count))
        ).isoformat()
        cur = conn.execute(
            f"""UPDATE job SET status='pending', error_message=?, next_retry_at=?,
                               worker_id=NULL, updated_at=? WHERE id=?{guard}""",
            [message, retry_at, now, job_id] + extra,
        )
        conn.commit()
        return PENDING if cur.rowcount > 0 else None
    cur = conn.execute(
        f"""UPDATE job SET status='failed', error_message=?, finished_at=?,
                           worker_id=NULL, updated_at=? WHERE id=?{guard}""",
        [message, now, now, job_id] + extra,
    )
    conn.commit()
    return FAILED if cur.rowcount > 0 else None


# ---------------------------------------------------------------- hủy / retry

def request_cancel(conn: sqlite3.Connection, job_id: int) -> str | None:
    """pending → cancelled ngay. running → cancelling (handler tự dừng).
    Job đã kết thúc → None, không đụng vào."""
    job = get(conn, job_id)
    if job is None or job.status in TERMINAL_STATUSES:
        return None
    now = _now()
    if job.status == PENDING:
        conn.execute(
            "UPDATE job SET status='cancelled', finished_at=?, updated_at=? WHERE id=?",
            (now, now, job_id),
        )
        conn.commit()
        return CANCELLED
    conn.execute("UPDATE job SET status='cancelling', updated_at=? WHERE id=?", (now, job_id))
    conn.commit()
    return CANCELLING


def mark_cancelled(
    conn: sqlite3.Connection, job_id: int, *, worker_id: str | None = None
) -> bool:
    now = _now()
    guard, extra = _fence(worker_id)
    cur = conn.execute(
        f"""UPDATE job SET status='cancelled', finished_at=?, worker_id=NULL,
                           updated_at=? WHERE id=?{guard}""",
        [now, now, job_id] + extra,
    )
    conn.commit()
    return cur.rowcount > 0


def retry(conn: sqlite3.Connection, job_id: int) -> bool:
    """Đưa job đã kết thúc về pending nếu chưa có job thay thế đang hoạt động."""
    now = _now()
    cur = conn.execute(
        """UPDATE job SET status='pending', attempt_count=0, error_message=NULL,
                          next_retry_at=NULL, worker_id=NULL, started_at=NULL,
                          finished_at=NULL, updated_at=?
              WHERE id=?
                AND status IN ('done', 'failed', 'cancelled')
                AND NOT EXISTS (
                    SELECT 1 FROM job live
                     WHERE live.id != job.id
                       AND job.patch_id IS NOT NULL
                       AND live.job_type = job.job_type
                       AND live.patch_id = job.patch_id
                       AND live.status IN ('pending', 'running', 'cancelling')
                )
                AND NOT EXISTS (
                    SELECT 1 FROM job live
                     WHERE live.id != job.id
                       AND job.dedupe_key IS NOT NULL
                       AND live.dedupe_key = job.dedupe_key
                       AND live.status IN ('pending', 'running')
                )""",
        (now, job_id),
    )
    conn.commit()
    return cur.rowcount > 0


def retry_all_failed(conn: sqlite3.Connection) -> int:
    """Retry job failed mới nhất cho mỗi type/patch và mọi job không gắn patch."""
    now = _now()
    cur = conn.execute(
        """UPDATE job SET status='pending', attempt_count=0, error_message=NULL,
                          next_retry_at=NULL, worker_id=NULL, started_at=NULL,
                          finished_at=NULL, updated_at=?
              WHERE status='failed'
                AND (patch_id IS NULL OR id IN (
                    SELECT MAX(id) FROM job
                     WHERE status='failed' AND patch_id IS NOT NULL
                     GROUP BY job_type, patch_id
                ))
                AND NOT EXISTS (
                    SELECT 1 FROM job live
                     WHERE live.job_type=job.job_type AND live.patch_id=job.patch_id
                       AND live.status IN ('pending', 'running', 'cancelling')
                )""",
        (now,),
    )
    conn.commit()
    return cur.rowcount


def reap_stale(
    conn: sqlite3.Connection, *, older_than_seconds: int, now: str | None = None
) -> list[int]:
    """Job 'running' mà nhịp tim đã ngừng (tiến trình chết, hoặc shutdown hết giờ chờ)
    được trả về 'pending'. attempt_count giữ nguyên nên nó vẫn tiêu một lượt retry —
    một job làm treo worker không được phép quay vòng vô hạn.

    COALESCE(heartbeat_at, started_at): job vừa claim xong đã chết trước lần
    write_progress đầu tiên thì heartbeat_at vẫn là giá trị đặt lúc claim, nhưng phòng
    khi có bản ghi cũ để NULL."""
    stamp = _parse(now) or datetime.now(timezone.utc)
    cutoff = (stamp - timedelta(seconds=older_than_seconds)).isoformat()
    rows = conn.execute(
        """UPDATE job SET status='pending', worker_id=NULL, updated_at=?
            WHERE status='running'
              AND COALESCE(heartbeat_at, started_at, created_at) < ?
        RETURNING id""",
        (stamp.isoformat(), cutoff),
    ).fetchall()
    conn.commit()
    return [r["id"] for r in rows]


# -------------------------------------------------------------------- đọc list

def list_jobs(
    conn: sqlite3.Connection, *, job_type: str | None = None, status: str | None = None,
    book_id: int | None = None, limit: int = 100, queue_order: bool = False,
) -> list[Job]:
    where, params = [], []
    if job_type:
        where.append("job_type=?")
        params.append(job_type)
    if status:
        where.append("status=?")
        params.append(status)
    if book_id is not None:
        where.append("book_id=?")
        params.append(book_id)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    params.append(max(1, min(limit, 1000)))
    order = "priority, id" if queue_order else "id DESC"
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM job {clause} ORDER BY {order} LIMIT ?", params
    ).fetchall()
    return [Job.from_row(r) for r in rows]


def counts(conn: sqlite3.Connection) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for row in conn.execute(
        "SELECT job_type, status, COUNT(*) AS c FROM job GROUP BY job_type, status"
    ):
        out.setdefault(row["job_type"], {})[row["status"]] = row["c"]
    return out


def clear_inactive(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM job WHERE status NOT IN ('running', 'cancelling')")
    conn.commit()
    return cur.rowcount


def delete_terminal(conn: sqlite3.Connection, job_ids: list[int]) -> int:
    ids = list(dict.fromkeys(job_ids))
    if not ids:
        return 0
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"DELETE FROM job WHERE id IN ({placeholders}) AND status IN ('done', 'failed', 'cancelled')",
        ids,
    )
    conn.commit()
    return cur.rowcount


def reorder_pending(conn: sqlite3.Connection, job_type: str, ordered_ids: list[int]) -> bool:
    if len(ordered_ids) != len(set(ordered_ids)):
        return False
    current = conn.execute(
        "SELECT id FROM job WHERE job_type=? AND status='pending' ORDER BY priority, id",
        (job_type,),
    ).fetchall()
    if {row["id"] for row in current} != set(ordered_ids):
        return False
    count = len(ordered_ids)
    conn.executemany(
        "UPDATE job SET priority=?, updated_at=? WHERE id=? AND job_type=? AND status='pending'",
        [(-count + index, _now(), job_id, job_type) for index, job_id in enumerate(ordered_ids)],
    )
    conn.commit()
    return True


def delete_pending(conn: sqlite3.Connection, job_id: int) -> bool:
    """Delete only work that has not started; dependent jobs are cancelled for auditability."""
    job = get(conn, job_id)
    if job is None or job.status != PENDING:
        return False
    now = _now()
    conn.execute(
        """UPDATE job SET status='cancelled', error_message='upstream job was deleted',
                  finished_at=?, updated_at=? WHERE id IN (
                  SELECT job_id FROM job_dependency WHERE depends_on_job_id=?)
                  AND status='pending'""",
        (now, now, job_id),
    )
    conn.execute("DELETE FROM job WHERE id=? AND status='pending'", (job_id,))
    conn.commit()
    return True


def pending_count(conn: sqlite3.Connection, job_type: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM job WHERE job_type=? AND status='pending'", (job_type,)
    ).fetchone()
    return row["c"]
