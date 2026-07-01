from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import db, repository
from app.config import settings
from app.routes import books, downloads, drive, logs, patches, queue, video, youtube
from app.tts_engine import VoxCPMEngine
from app.worker import DisabledWorker, PatchWorker

_log_formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
_file_handler = RotatingFileHandler(
    settings.log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
)
_file_handler.setFormatter(_log_formatter)
_console_handler = logging.StreamHandler()
_console_handler.setFormatter(_log_formatter)

_root = logging.getLogger()
for h in list(_root.handlers):
    _root.removeHandler(h)
_root.setLevel(logging.INFO)
_root.addHandler(_file_handler)
_root.addHandler(_console_handler)
# uvicorn dev-reload chatter — ³1 change detected² fires for every watched file
# change (including generated audio output in data/). Mute it; WARNING+ still surface.
logging.getLogger("watchfiles").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    requeued_patches = repository.requeue_stuck_processing_returning(conn)
    if requeued_patches:
        logging.info(
            "requeued %s patch(es) left 'processing' from a previous crashed run; "
            "next_chunk_index preserved for chunk-level resume",
            len(requeued_patches),
        )
        for r in requeued_patches:
            if r["next_chunk_index"] > 0 and r["chunk_count"] > 0:
                logging.info(
                    "  resume patch_id=%s book_id=%s chunk %s/%s",
                    r["patch_id"], r["book_id"],
                    r["next_chunk_index"], r["chunk_count"],
                )
    requeued_bj = repository.requeue_stuck_book_jobs(conn)
    if requeued_bj:
        logging.info("requeued %s book_job(s) left 'processing' from a previous crashed run", requeued_bj)

    if settings.reset_all_jobs_on_startup:
        summary = repository.reset_all_jobs(conn)
        logging.info(
            "event=reset_all_jobs_on_startup patches_reset=%s book_jobs_reset=%s "
            "books_reset=%s files_deleted=%s",
            summary["patches_reset"],
            summary["book_jobs_reset"],
            summary["books_reset"],
            summary["files_deleted"],
        )
    else:
        backfilled = repository.backfill_video_book_jobs(conn)
        if backfilled:
            logging.info(
                "event=backfill.video_jobs_inserted count=%s",
                backfilled,
            )

    db_lock = threading.Lock()
    app.state.conn = conn
    app.state.db_lock = db_lock

    worker_task: asyncio.Task | None = None
    if settings.enable_worker:
        engine = VoxCPMEngine()
        worker = PatchWorker(
            conn,
            engine,
            settings.data_root,
            settings.worker_poll_interval,
            db_lock,
            shutdown_timeout=settings.worker_shutdown_timeout_seconds,
        )
        app.state.worker = worker
        worker_task = asyncio.create_task(worker.run_forever())
        logging.info(
            "worker started (poll_interval=%s s, shutdown_timeout=%s s)",
            settings.worker_poll_interval,
            settings.worker_shutdown_timeout_seconds,
        )
    else:
        app.state.worker = DisabledWorker()
        logging.info(
            "worker disabled by settings.enable_worker=false — background loop "
            "suppressed + all processing patches/book_jobs requeued. "
            "Set ENABLE_WORKER=true to enable."
        )

    try:
        yield
    finally:
        if worker_task is not None:
            worker.stop()
            try:
                await asyncio.wait_for(worker_task, timeout=settings.worker_shutdown_timeout_seconds)
            except asyncio.TimeoutError:
                logger = logging.getLogger(__name__)
                logger.warning(
                    "worker did not stop within %s s; cancelling",
                    settings.worker_shutdown_timeout_seconds,
                )
                worker.log_shutdown_timeout()
                worker_task.cancel()
                try:
                    await worker_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
        conn.close()


app = FastAPI(title="EPUB Audiobook App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(books.router)
app.include_router(patches.router)
app.include_router(downloads.router)
app.include_router(queue.router)
app.include_router(logs.router)
app.include_router(video.router)
app.include_router(youtube.router)
app.include_router(drive.router)


@app.get("/")
def root():
    return RedirectResponse(url="/books")
