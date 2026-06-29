"""Routes for queue observability and admin: /health, /queue/stats, pause/resume,
retry-failed, regenerate-video. The book detail page embeds last-error and video
status; the buttons that drive state changes live here."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import repository
from app.deps import locked_conn
from app.config import settings
from app.worker import DisabledWorker

logger = logging.getLogger(__name__)

router = APIRouter()


def _worker_snapshot(worker) -> dict:
    """Fields safe to expose on /health regardless of worker kind."""
    if isinstance(worker, DisabledWorker):
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
    if isinstance(worker, DisabledWorker):
        # Background loop is intentionally off (dev mode / uvicorn --reload). The
        # server is up and serving HTTP; the queue is just not draining.
        return {
            "status": "ok",
            "worker_state": "disabled",
            "current_patch_id": None,
            "current_chunk_index": 0,
            "current_chunk_count": 0,
            "queue_depth": 0,
            "last_heartbeat_at": None,
        }
    last_hb = _parse_iso(worker.last_heartbeat_at)
    now = datetime.now(timezone.utc)
    poll = settings.worker_poll_interval
    threshold = 3.0 * poll
    if last_hb is None or (now - last_hb).total_seconds() > threshold:
        reason = (
            f"no heartbeat within {threshold:.1f}s (last: {worker.last_heartbeat_at})"
        )
        return JSONResponse(
            {
                "status": "degraded",
                "reason": reason,
                "worker_state": worker.state,
                "current_patch_id": worker.current_patch_id,
                "current_chunk_index": getattr(worker, "current_chunk_index", 0),
                "current_chunk_count": getattr(worker, "current_chunk_count", 0),
                "last_heartbeat_at": worker.last_heartbeat_at,
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
    }


@router.get("/queue/stats")
def queue_stats(request: Request):
    with locked_conn(request) as conn:
        return repository.get_queue_stats(conn)


@router.post("/queue/requeue-stuck")
def requeue_stuck(request: Request):
    """Operator escape hatch: flip every 'processing' patch back to 'pending' without
    discarding next_chunk_index. The worker will pick each one up and resume from the
    last persisted chunk instead of redoing the whole patch. Mirrors the recovery that
    runs at startup in main.lifespan."""
    with locked_conn(request) as conn:
        resumed = repository.requeue_stuck_processing_returning(conn)
    logger.info(
        "event=queue.requeue_stuck count=%s",
        len(resumed),
    )
    return {"requeued": len(resumed), "patches": resumed}


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
            repository.delete_book_job(conn, book_id, "video")
        repository.enqueue_book_job(conn, book_id, "video")
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/queue/reset-all")
def reset_all_jobs(request: Request):
    """Reset every patch and book_job to pending, every book to 'ready', and delete
    all produced audio/video files from disk. Returns a JSON summary of what was
    touched. No confirmation prompt — callers should gate behind a UI button or
    an env flag (RESET_ALL_JOBS_ON_STARTUP=true in dev)."""
    with locked_conn(request) as conn:
        summary = repository.reset_all_jobs(conn)
    logger.info(
        "event=queue.reset_all patches_reset=%s book_jobs_reset=%s books_reset=%s files_deleted=%s",
        summary["patches_reset"],
        summary["book_jobs_reset"],
        summary["books_reset"],
        summary["files_deleted"],
    )
    return summary
