from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app import db, repository
from app.config import settings
from app.routes import books, downloads, patches
from app.tts_engine import VoxCPMEngine
from app.worker import PatchWorker

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    requeued = repository.requeue_stuck_processing(conn)
    if requeued:
        logging.info("requeued %s patch(es) left 'processing' from a previous crashed run", requeued)

    db_lock = threading.Lock()
    engine = VoxCPMEngine()
    worker = PatchWorker(conn, engine, settings.data_root, settings.worker_poll_interval, db_lock)

    app.state.conn = conn
    app.state.db_lock = db_lock
    app.state.worker = worker

    worker_task = asyncio.create_task(worker.run_forever())
    try:
        yield
    finally:
        worker.stop()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        conn.close()


app = FastAPI(title="EPUB Audiobook App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(books.router)
app.include_router(patches.router)
app.include_router(downloads.router)


@app.get("/")
def root():
    return RedirectResponse(url="/books")
