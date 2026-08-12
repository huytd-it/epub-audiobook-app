"""Routes for queue observability and admin: /health, /queue/stats, pause/resume,
retry-failed, regenerate-video. The book detail page embeds last-error and video
status; the buttons that drive state changes live here."""
from __future__ import annotations

import logging
import asyncio
import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from app import repository
from app.deps import locked_conn
from app.config import settings
from app.jobqueue import joblog, store
from app.jobqueue.backfill import backfill_pending_jobs, enqueue_pending_patch_jobs
from app.jobqueue.models import TERMINAL_STATUSES

logger = logging.getLogger(__name__)

router = APIRouter()

_JOB_FIELDS = ("id", "job_type", "status", "priority", "book_id", "phase",
               "progress_current", "progress_total", "error_message", "attempt_count",
               "max_attempts", "worker_id", "created_at", "started_at", "finished_at",
               "updated_at")
_JOB_FIELDS += ("flow_run_id", "node_id", "patch_id")


def _job_dict(job) -> dict:
    data = {name: getattr(job, name) for name in _JOB_FIELDS}
    data["payload"] = job.payload
    data["result"] = job.result
    data["percent"] = (min(100, round(job.progress_current * 100 / job.progress_total))
                       if job.progress_total else 0)
    return data


def _pools(worker) -> list[dict]:
    if worker is None or not hasattr(worker, "pool_status"):
        return []
    try:
        return worker.pool_status()
    except Exception:
        return []


def _worker_snapshot(worker) -> dict:
    """Fields safe to expose on /health regardless of worker kind."""
    if worker is None:
        return {
            "current_patch_id": None,
            "current_chunk_index": 0,
            "current_chunk_count": 0,
        }
    return {
        "current_patch_id": worker.current_patch_id,
        "current_chunk_index": getattr(worker, "current_chunk_index", 0),
        "current_chunk_count": getattr(worker, "current_chunk_count", 0),
    }


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


@router.get("/health")
def health(request: Request):
    """Lightweight liveness probe. 200 when the worker has heartbeated recently,
    503 otherwise. Returns the worker's last known state for diagnostic context."""
    worker = request.app.state.worker
    if worker is None:
        return {
            "status": "ok",
            "worker_state": "disabled",
            "current_patch_id": None,
            "current_chunk_index": 0,
            "current_chunk_count": 0,
            "queue_depth": 0,
            "last_heartbeat_at": None,
            "pools": _pools(worker),
        }
    last_hb = _parse_iso(worker.last_heartbeat_at)
    now = datetime.now(timezone.utc)
    poll = settings.worker_poll_interval
    threshold = 3.0 * poll
    if last_hb is None or (now - last_hb).total_seconds() > threshold:
        reason = (
            f"no heartbeat within {threshold:.1f}s (last: {worker.last_heartbeat_at})"
        )
        with locked_conn(request) as conn:
            queue_depth = repository.get_queue_stats(conn)["patch"]["pending"]
        return JSONResponse(
            {
                "status": "degraded",
                "reason": reason,
                "worker_state": worker.state,
                "current_patch_id": worker.current_patch_id,
                "current_chunk_index": getattr(worker, "current_chunk_index", 0),
                "current_chunk_count": getattr(worker, "current_chunk_count", 0),
                "queue_depth": queue_depth,
                "last_heartbeat_at": worker.last_heartbeat_at,
                "pools": _pools(worker),
            },
            status_code=503,
        )

    with locked_conn(request) as conn:
        stats = repository.get_queue_stats(conn)
    return {
        "status": "ok",
        "worker_state": worker.state,
        **_worker_snapshot(worker),
        "queue_depth": stats["patch"]["pending"],
        "last_heartbeat_at": worker.last_heartbeat_at,
        "pools": _pools(worker),
    }


@router.get("/queue/stats")
def queue_stats(request: Request):
    with locked_conn(request) as conn:
        stats = repository.get_queue_stats(conn)
        stats["jobs"] = store.counts(conn)
        return stats




@router.get("/queue/jobs")
def list_jobs(request: Request, type: str = "", status: str = "",
              book_id: int | None = None, limit: int = 100):
    with locked_conn(request) as conn:
        jobs = store.list_jobs(conn, job_type=type or None, status=status or None,
                               book_id=book_id, limit=limit)
    return {"jobs": [_job_dict(job) for job in jobs]}


@router.get("/queue/jobs/{job_id}")
def job_detail(request: Request, job_id: int):
    with locked_conn(request) as conn:
        job = store.get(conn, job_id)
    if job is None:
        raise HTTPException(404, detail=f"job {job_id} không tồn tại")
    return _job_dict(job)


@router.get("/queue/jobs/{job_id}/log", response_class=PlainTextResponse)
def job_log(request: Request, job_id: int, tail: int = 500):
    with locked_conn(request) as conn:
        if store.get(conn, job_id) is None:
            raise HTTPException(404, detail=f"job {job_id} không tồn tại")
    return joblog.tail(job_id, lines=tail)


@router.get("/queue/jobs/{job_id}/stream")
async def job_stream(request: Request, job_id: int):
    with locked_conn(request) as conn:
        if store.get(conn, job_id) is None:
            raise HTTPException(404, detail=f"job {job_id} không tồn tại")

    async def stream():
        cursor = 0
        while True:
            events, cursor = await asyncio.to_thread(
                joblog.read_events, job_id, from_line=cursor
            )
            for event in events:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            with locked_conn(request) as conn:
                job = store.get(conn, job_id)
            if job is None:
                return
            yield f"data: {json.dumps({'type': 'progress', **_job_dict(job)}, ensure_ascii=False, default=str)}\n\n"
            if job.status in TERMINAL_STATUSES or await request.is_disconnected():
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/queue/jobs/{job_id}/cancel")
def cancel_job(request: Request, job_id: int):
    with locked_conn(request) as conn:
        status = store.request_cancel(conn, job_id)
    if status is None:
        raise HTTPException(409, detail="job đã kết thúc hoặc không tồn tại")
    queue = getattr(request.app.state, "job_queue", None)
    if queue is not None and hasattr(queue, "request_cancel"):
        queue.request_cancel(job_id)
    return {"job_id": job_id, "status": status}


@router.post("/queue/jobs/{job_id}/retry")
def retry_job(request: Request, job_id: int):
    with locked_conn(request) as conn:
        if not store.retry(conn, job_id):
            raise HTTPException(409, detail="chỉ retry được job đã kết thúc")
    return {"job_id": job_id, "retried": True}


@router.delete("/queue/jobs/{job_id}")
def delete_job(request: Request, job_id: int):
    with locked_conn(request) as conn:
        if not store.delete_pending(conn, job_id):
            raise HTTPException(409, detail="chỉ xóa được job đang chờ")
    return {"job_id": job_id, "deleted": True}


@router.post("/queue/clear")
def clear_queue(request: Request):
    with locked_conn(request) as conn:
        cleared = store.clear_inactive(conn)
    return {"cleared": cleared}


@router.post("/queue/requeue-stuck")
def requeue_stuck(request: Request):
    """Operator escape hatch: flip every 'processing' patch back to 'pending' without
    discarding next_chunk_index, then queue every pending patch. The worker resumes each
    one from the last persisted chunk instead of redoing the whole patch. Startup flips
    crashed patches the same way but deliberately does not queue them, so this is the
    button that actually restarts synthesis after a crash."""
    with locked_conn(request) as conn:
        resumed = repository.requeue_stuck_processing_returning(conn)
        repository.requeue_stuck_book_jobs(conn)
        backfill_pending_jobs(conn)
        queued = enqueue_pending_patch_jobs(conn)
    logger.info(
        "event=queue.requeue_stuck count=%s queued=%s",
        len(resumed), queued,
    )
    return {"requeued": len(resumed), "queued": queued, "patches": resumed}


@router.post("/queue/pause")
def pause_queue(request: Request):
    with locked_conn(request) as conn:
        repository.set_app_state(conn, "queue.paused", "1")
    return RedirectResponse(url="/books", status_code=303)


@router.post("/queue/resume")
def resume_queue(request: Request):
    with locked_conn(request) as conn:
        repository.set_app_state(conn, "queue.paused", "0")
    return RedirectResponse(url="/books", status_code=303)


@router.post("/books/{book_id}/patches/retry-failed")
def retry_failed_patches(request: Request, book_id: int):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        n = repository.retry_all_failed_patches_for_book(conn, book_id)
        enqueue_pending_patch_jobs(conn, book_id)
    logger.info("retry_all_failed book_id=%s reset=%s", book_id, n)
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/video/regenerate")
def regenerate_video(request: Request, book_id: int):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        existing = repository.get_book_job(conn, book_id, "video")
        if existing is not None and existing.status == "processing":
            raise HTTPException(
                status_code=409,
                detail="a video job for this book is already processing; wait for it to finish",
            )
        if existing is not None:
            stale = store.find_live_by_dedupe(conn, f"video:book_job={existing.id}")
            if stale is not None:
                store.request_cancel(conn, stale.id)
                if store.get(conn, stale.id).status == "cancelling":
                    store.mark_cancelled(conn, stale.id)
            repository.delete_book_job(conn, book_id, "video")
        book_job = repository.enqueue_book_job(conn, book_id, "video")
        store.enqueue(conn, "video", payload={"book_job_id": book_job.id}, book_id=book_id,
                      dedupe_key=f"video:book_job={book_job.id}")
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/queue/reset-all")
def reset_all_jobs(request: Request):
    """Reset every patch and book_job to pending, every book to 'ready', and delete
    all produced audio/video files from disk. Returns a JSON summary of what was
    touched. No confirmation prompt — callers should gate behind a UI button or
    an env flag (RESET_ALL_JOBS_ON_STARTUP=true in dev)."""
    with locked_conn(request) as conn:
        summary = repository.reset_all_jobs(conn)
        cleared = conn.execute("DELETE FROM job").rowcount
        conn.commit()
        summary["jobs_cleared"] = cleared
        summary["jobs_enqueued"] = (
            sum(backfill_pending_jobs(conn).values()) + enqueue_pending_patch_jobs(conn)
        )
    logger.info(
        "event=queue.reset_all patches_reset=%s book_jobs_reset=%s books_reset=%s files_deleted=%s",
        summary["patches_reset"],
        summary["book_jobs_reset"],
        summary["books_reset"],
        summary["files_deleted"],
    )
    return summary
