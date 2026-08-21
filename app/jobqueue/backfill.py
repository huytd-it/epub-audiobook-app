"""Build the job queue and import pending rows from legacy job tables."""
from __future__ import annotations

import logging
import json
import sqlite3
from pathlib import Path
from typing import Callable

from app.config import settings
from app.jobqueue import store
from app.jobqueue.handlers import background_gen, flow_nodes, gameplay_clip, light_tts, patch_video, standalone_video, video, audiobook_tts, youtube_upload
from app.jobqueue.runner import JobQueue, parse_concurrency

logger = logging.getLogger(__name__)

JOB_TYPES = (
    "audiobook_tts", "video", "patch_video", "standalone_video",
    "youtube_upload", "light_tts", "flow_audio", "flow_video",
    "flow_youtube", "background_gen", "gameplay_clip",
)
QUEUE_CONCURRENCY_STATE_KEY = "queue.concurrency"


def configured_concurrency(conn: sqlite3.Connection) -> dict[str, int]:
    from app import repository

    concurrency = parse_concurrency(
        settings.queue_concurrency, default=settings.queue_default_concurrency
    )
    concurrency.setdefault("patch_video", max(1, int(settings.patch_video_concurrency)))
    concurrency.setdefault("gameplay_clip", max(1, int(settings.gameplay_clip_concurrency)))
    raw = repository.get_app_state(conn, QUEUE_CONCURRENCY_STATE_KEY)
    if raw:
        try:
            saved = json.loads(raw)
            concurrency.update({
                job_type: value for job_type, value in saved.items()
                if job_type in JOB_TYPES and type(value) is int and 0 <= value <= 64
            })
        except (json.JSONDecodeError, AttributeError):
            logger.warning("Ignoring invalid persisted queue concurrency")
    return {job_type: concurrency.get(job_type, settings.queue_default_concurrency) for job_type in JOB_TYPES}


def build_queue(conn_factory: Callable[[], sqlite3.Connection]) -> JobQueue:
    from app import repository

    conn = conn_factory()
    try:
        concurrency = configured_concurrency(conn)
    finally:
        conn.close()
    queue = JobQueue(
        conn_factory,
        concurrency=concurrency,
        default_concurrency=settings.queue_default_concurrency,
        poll_interval=settings.worker_poll_interval,
        reap_after_seconds=settings.queue_reap_after_seconds,
        is_paused=repository.is_queue_paused,
    )
    queue.register("audiobook_tts", audiobook_tts.handle)
    # Compatibility for queue rows created before the generic TTS rename. New jobs
    # always use audiobook_tts; persisted voxcpm_tts rows can still finish safely.
    queue.register("voxcpm_tts", audiobook_tts.handle)
    queue.register("video", video.handle)
    queue.register("patch_video", patch_video.handle)
    queue.register("standalone_video", standalone_video.handle)
    queue.register("youtube_upload", youtube_upload.handle, cancellable=False)
    queue.register("light_tts", light_tts.handle)
    queue.register("flow_audio", flow_nodes.audio)
    queue.register("flow_video", flow_nodes.video)
    queue.register("flow_youtube", flow_nodes.youtube, cancellable=False)
    queue.register("background_gen", background_gen.handle)
    queue.register("gameplay_clip", gameplay_clip.handle)
    return queue


def enqueue_pending_patch_jobs(
    conn: sqlite3.Connection, book_id: int | None = None, tts_engine: str | None = None,
    *, voice: str | None = None, max_chars: int = 0, with_effects: bool = False,
    patch_ids: list[int] | None = None, auto_create_video: bool | None = None,
    auto_upload_youtube: bool | None = None, retry_count: int = 2,
    missing_audio_only: bool = False,
) -> int:
    """Queue a audiobook_tts job for every 'pending' patch, optionally of a single book.

    Deliberately NOT part of backfill_pending_jobs: synthesis is expensive and holds the
    GPU, so it only starts when an operator asks for it (Start queue / Retry failed /
    Requeue stuck / Reset all). Running it at startup would refill a queue that was just
    cleared from /queue."""
    if patch_ids is not None:
        ids = list(dict.fromkeys(int(patch_id) for patch_id in patch_ids))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        params: list[int] = []
        where_book = ""
        if book_id is not None:
            where_book = " AND book_id=?"
            params.append(book_id)
        params.extend(ids)
        rows = conn.execute(
            f"SELECT id, book_id, audio_path FROM patch WHERE status!='processing'{where_book} AND id IN ({placeholders}) ORDER BY book_id, patch_index",
            params,
        ).fetchall()
    elif book_id is None:
        rows = conn.execute(
            "SELECT id, book_id FROM patch WHERE status='pending' ORDER BY book_id, patch_index"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, book_id FROM patch WHERE status='pending' AND book_id=? ORDER BY patch_index",
            (book_id,),
        ).fetchall()

    explicit_config = tts_engine is not None
    engine_id = tts_engine or settings.tts_engine
    from app import repository
    from app.tts_engine import create_tts_engine
    # Validate without loading the heavy model.
    engine = create_tts_engine(engine_id)
    del engine

    # Book audio config cache: bulk "Start queue" reads the book's ACTUAL (effective)
    # audio config instead of blindly using settings.tts_engine. Snapshot files from a
    # previous run still win per patch — that frozen config is what the chunks on disk
    # were generated with, so changing it would invalidate them (no rename on the file).
    from app.production_defaults import get_effective_audio_config
    books: dict[int, object] = {}

    def _audio_config(book_id: int) -> dict:
        book = books.get(book_id)
        if book is None:
            book = repository.get_book(conn, book_id)
            books[book_id] = book
        return get_effective_audio_config(conn, book) if book else {}

    queued = 0
    for row in rows:
        if missing_audio_only and row["audio_path"] and Path(row["audio_path"]).is_file():
            continue
        if not explicit_config:
            audio = _audio_config(row["book_id"])
            request = {
                "tts_engine": audio.get("model_id") or settings.tts_engine,
                "voice": voice if voice else audio.get("voice_id"),
                "max_chars": audio.get("max_chars") or 0,
                "with_effects": bool(audio.get("with_effects", False)),
                "chunk_pause_ms": audio.get("chunk_pause_ms"),
                "chapter_pause_ms": audio.get("chapter_pause_ms"),
            }
        else:
            # An operator overriding the engine/voice is not overriding the merge
            # spacing, so the pauses still come from the book's own audio config.
            audio = _audio_config(row["book_id"])
            request = {
                "tts_engine": engine_id, "voice": voice, "max_chars": max_chars,
                "with_effects": with_effects,
                "chunk_pause_ms": audio.get("chunk_pause_ms"),
                "chapter_pause_ms": audio.get("chapter_pause_ms"),
            }
        # Cờ tự động hoá chỉ vào payload khi operator truyền RÕ — bỏ trống nghĩa là dùng
        # cột persisted trên sách (auto_create_video/auto_upload_youtube).
        if auto_create_video is not None:
            request["auto_create_video"] = bool(auto_create_video)
        if auto_upload_youtube is not None:
            request["auto_upload_youtube"] = bool(auto_upload_youtube)
        if auto_upload_youtube:
            request["auto_create_video"] = True
        if not explicit_config:
            snapshot = (
                Path(settings.data_root) / "books" / str(row["book_id"]) / "patches" /
                f"{row['id']}_chunks" / ".tts_request.json"
            )
            try:
                saved = json.loads(snapshot.read_text(encoding="utf-8"))
                if saved.get("tts_engine"):
                    request.update(saved)
            except (OSError, ValueError, TypeError):
                pass
        if store.enqueue(
            conn,
            "audiobook_tts",
            payload={"patch_id": row["id"], **request},
            book_id=row["book_id"],
            patch_id=row["id"],
            dedupe_key=f"audiobook_tts:patch={row['id']}",
            max_attempts=max(1, min(11, int(retry_count) + 1)),
        ) is not None:
            queued += 1
    return queued


def backfill_pending_jobs(conn: sqlite3.Connection) -> dict[str, int]:
    """Re-attach queue rows to legacy tables that already hold pending work. Runs at
    startup, so it covers only the cheap resumable job types - see
    enqueue_pending_patch_jobs for why audiobook_tts is excluded."""
    counts = {"video": 0, "youtube_upload": 0}

    conn.execute(
        """UPDATE youtube_uploads SET validation_status='pending'
           WHERE status='pending' AND validation_status='validating'"""
    )
    conn.commit()

    for row in conn.execute(
        "SELECT id, book_id FROM book_job WHERE status='pending' AND job_type='video' ORDER BY id"
    ).fetchall():
        if store.enqueue(
            conn,
            "video",
            payload={"book_job_id": row["id"]},
            book_id=row["book_id"],
            dedupe_key=f"video:book_job={row['id']}",
        ) is not None:
            counts["video"] += 1

    for row in conn.execute(
        """SELECT u.id AS id, p.patch_id AS patch_id
           FROM youtube_uploads u
           LEFT JOIN patch_pipeline p ON p.youtube_upload_id = u.id
           WHERE u.status='pending' ORDER BY u.id"""
    ).fetchall():
        if store.enqueue(
            conn,
            "youtube_upload",
            payload={"upload_id": row["id"]},
            patch_id=row["patch_id"],
            dedupe_key=f"youtube_upload:upload={row['id']}",
        ) is not None:
            counts["youtube_upload"] += 1

    for row in conn.execute(
        """SELECT id, render_source_type, render_source_id, integrity_retry_count
           FROM youtube_uploads WHERE validation_status='waiting_for_rerender'
           ORDER BY id"""
    ).fetchall():
        source_type, source_id = row["render_source_type"], row["render_source_id"]
        if source_type == "book":
            job_type, payload = "video", {"book_job_id": source_id, "recovery_upload_id": row["id"]}
        elif source_type == "patch":
            job_type, payload = "patch_video", {"patch_id": source_id, "recovery_upload_id": row["id"]}
        elif source_type == "standalone":
            job_type, payload = "standalone_video", {"video_id": source_id, "recovery_upload_id": row["id"]}
        else:
            continue
        store.enqueue(
            conn, job_type, payload=payload,
            dedupe_key=f"{job_type}:source={source_id}:integrity_retry={row['integrity_retry_count']}",
        )

    return counts
