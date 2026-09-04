from __future__ import annotations

import logging
import logging.handlers
import threading
import re
from contextlib import asynccontextmanager

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import db, repository
from app.config import settings
from app.routes import (books, database_io, downloads, drive, effects, gameplay, local_bridge, logs, media_browser, music,
    patches, photos, production_settings, queue, text_studio, tts_models, ui_api, validation, video, video_api, voices, youtube)
import asyncio

from app.jobqueue import joblog, store
from app.jobqueue.backfill import backfill_pending_jobs, build_queue

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.handlers.RotatingFileHandler(
            settings.log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
        ),
        logging.StreamHandler(),
    ],
)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    from app.gameplay_repository import recover_reserved_clips, seed_catalog
    seed_catalog(conn)
    recovered_gameplay = recover_reserved_clips(conn)
    if recovered_gameplay:
        logging.info("event=gameplay.reservations_recovered count=%s", recovered_gameplay)
    Path(settings.data_root, "preview_tmp").mkdir(parents=True, exist_ok=True)
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
    elif not settings.clean_start_on_startup:
        backfilled = repository.backfill_video_book_jobs(conn)
        if backfilled:
            logging.info(
                "event=backfill.video_jobs_inserted count=%s",
                backfilled,
            )

    db_lock = threading.Lock()
    app.state.conn = conn
    app.state.db_lock = db_lock

    if settings.clean_start_on_startup:
        # Khởi động sạch: hàng đợi của lần chạy trước bị xoá hết và không có bước
        # backfill/automation nào chạy — job chỉ xuất hiện khi người dùng bấm.
        cleared = store.clear_all(conn)
        logging.info("event=queue.clean_start jobs_cleared=%s", cleared)
    else:
        backfilled = backfill_pending_jobs(conn)
        if any(backfilled.values()):
            logging.info(
                "event=queue.backfill video=%s youtube_upload=%s",
                backfilled["video"], backfilled["youtube_upload"],
            )

        # Tự động hoá patch: enqueue các job patch_video/youtube_upload còn thiếu cho mọi
        # patch đã có audio (waiting_config đã tự khỏi khi config hợp lệ trở lại).
        from app.patch_publishing import reconcile_patch_automation
        try:
            reconciled = reconcile_patch_automation(conn)
            if sum(v for k, v in reconciled.items() if k != "errors") or reconciled["errors"]:
                logging.info("event=queue.automation_reconcile %s", reconciled)
        except Exception:
            logging.exception("event=queue.automation_reconcile.failed")

    removed = joblog.purge_old_logs(conn)
    if removed:
        logging.info("event=queue.log_purge removed=%s", removed)

    job_queue = None
    if settings.enable_worker:
        job_queue = build_queue(lambda: db.connect(settings.db_path))
        await job_queue.start()
        logging.info(
            "event=queue.config %s",
            " ".join(f"{p['job_type']}={p['capacity']}" for p in job_queue.pool_status()),
        )
    app.state.job_queue = job_queue
    app.state.worker = job_queue
    app.state.upload_worker = None

    try:
        yield
    finally:
        if job_queue is not None:
            await job_queue.stop(timeout=settings.worker_shutdown_timeout_seconds)
        conn.close()


app = FastAPI(title="EPUB Audiobook App", lifespan=lifespan)
app.include_router(books.router)
app.include_router(patches.router)
app.include_router(downloads.router)
app.include_router(queue.router)
app.include_router(logs.router)
app.include_router(video.router)
app.include_router(video_api.router)
app.include_router(music.router)
app.include_router(photos.router)
app.include_router(voices.router)
app.include_router(youtube.router)
app.include_router(text_studio.router)
app.include_router(drive.router)
app.include_router(database_io.router)
app.include_router(effects.router)
app.include_router(local_bridge.router)
app.include_router(validation.router)
app.include_router(ui_api.router)
app.include_router(production_settings.router)
app.include_router(tts_models.router)
app.include_router(gameplay.router)
app.include_router(media_browser.router)


SPA_DIR = Path("app/spa_dist")
PUBLIC_DIR = Path("frontend/public")
if SPA_DIR.exists():
    app.mount("/assets", StaticFiles(directory=SPA_DIR / "assets"), name="spa-assets")


@app.get("/gameplay/{filename}", include_in_schema=False)
def gameplay_static(filename: str):
    candidate = (PUBLIC_DIR / "gameplay" / filename).resolve()
    if (PUBLIC_DIR / "gameplay").resolve() in candidate.parents and candidate.is_file():
        return FileResponse(candidate)


def _spa_index():
    index = SPA_DIR / "index.html"
    if not index.exists():
        return HTMLResponse("Frontend chưa được build. Chạy npm install && npm run build.", status_code=503)
    return HTMLResponse(index.read_text(encoding="utf-8"))


_SPA_PATHS = (
    re.compile(r"^/books(?:/upload|/\d+|/\d+/chapters/preview-ui|/\d+/patches/build|/\d+/patches/\d+/chunks|/\d+/text-studio)?$"),
    re.compile(r"^/(?:queue|media|music|photos|voices|effects|youtube|drive|database-io|logs|production-defaults|gameplay|media-browser)$"),
)


@app.middleware("http")
async def spa_pages(request: Request, call_next):
    if request.method == "GET" and any(pattern.fullmatch(request.url.path) for pattern in _SPA_PATHS):
        return _spa_index()
    response = await call_next(request)
    if request.method == "GET" and response.status_code == 404 and "text/html" in request.headers.get("accept", ""):
        return _spa_index()
    return response


@app.get("/", include_in_schema=False)
def root():
    return _spa_index()


@app.get("/app/{path:path}", include_in_schema=False)
def spa(path: str):
    candidate = (SPA_DIR / path).resolve()
    if path and SPA_DIR.resolve() in candidate.parents and candidate.is_file():
        return FileResponse(candidate)
    return _spa_index()


@app.get("/{filename}", include_in_schema=False)
def spa_root_asset(filename: str):
    candidate = (SPA_DIR / filename).resolve()
    if SPA_DIR.resolve() in candidate.parents and candidate.is_file():
        return FileResponse(candidate)
    return _spa_index()
