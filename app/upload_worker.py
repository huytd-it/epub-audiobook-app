"""Sequential YouTube upload queue worker."""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone, timedelta

from app import db, youtube
from app.patch_publishing import sync_pipeline_from_upload
from app.video_repository import get_video, update_video

logger = logging.getLogger(__name__)

UPLOAD_DELAY = 2  # seconds between uploads
DAILY_UPLOAD_LIMIT = 50  # max uploads per day (YouTube default quota)


class UploadWorker:
    def __init__(self, conn: sqlite3.Connection, db_lock: threading.Lock | None = None):
        self.conn = conn
        self.db_lock = db_lock or threading.Lock()
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Upload worker started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Upload worker stopped")

    def enqueue(self, video_id: int | None, title: str, description: str, tags: str, privacy: str, video_path: str | None = None) -> int:
        """Enqueue a video for upload. If video_id is None or video not found, uses video_path directly."""
        with self.db_lock:
            file_path = video_path
            if video_id is not None:
                video = get_video(self.conn, video_id)
                if video:
                    file_path = video["file_path"]
                    tags_list = [t.strip() for t in tags.split(",") if t.strip()]
                    upload_id = youtube.enqueue_upload(
                        self.conn, file_path, title, description, tags_list, privacy, video_id=video_id
                    )
                    update_video(self.conn, video_id, upload_status="queued", youtube_upload_id=upload_id)
                    return upload_id
            # ponytail: direct enqueue without video record, for auto-upload before video table integration
            if not file_path:
                raise ValueError("video_path required when video_id is None or video not found")
            tags_list = [t.strip() for t in tags.split(",") if t.strip()]
            return youtube.enqueue_upload(
                self.conn, file_path, title, description, tags_list, privacy, video_id=None
            )

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "processing": self._task is not None and not self._task.done(),
        }

    async def _run_loop(self):
        while self._running:
            try:
                with self.db_lock:
                    pending = youtube.get_pending_uploads(self.conn)

                # Filter by daily upload limit and scheduled publish time
                eligible = self._filter_eligible_uploads(pending)

                for upload in eligible:
                    if not self._running:
                        break
                    await self._process_upload(upload)
                    await asyncio.sleep(UPLOAD_DELAY)
            except Exception as e:
                logger.error("Upload worker error: %s", e)

            await asyncio.sleep(5)  # poll interval

    def _filter_eligible_uploads(self, pending: list[dict]) -> list[dict]:
        """Filter pending uploads by daily limit and scheduled publish time."""
        now = datetime.now(timezone.utc)
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        # Count successful uploads today
        with self.db_lock:
            row = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM youtube_uploads WHERE status='done' AND uploaded_at >= ? AND uploaded_at < ?",
                (today_start.isoformat(), today_end.isoformat()),
            ).fetchone()
            uploaded_today = row["cnt"] if row else 0

        remaining_quota = max(0, DAILY_UPLOAD_LIMIT - uploaded_today)
        eligible = []
        for upload in pending:
            if remaining_quota <= 0:
                logger.info("Daily upload limit reached (%d/%d), deferring remaining uploads",
                           uploaded_today, DAILY_UPLOAD_LIMIT)
                break

            # Check scheduled publish time
            scheduled = upload.get("scheduled_publish_at")
            if scheduled:
                try:
                    sched_dt = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
                    if sched_dt > now:
                        logger.info("Upload %d scheduled for %s, skipping for now", upload["id"], scheduled)
                        continue
                except (ValueError, TypeError):
                    pass  # Invalid schedule, proceed with upload

            eligible.append(upload)
            remaining_quota -= 1

        return eligible

    async def _process_upload(self, upload: dict):
        upload_id = upload["id"]
        video_id = upload.get("video_id")
        try:
            validation = await asyncio.to_thread(youtube.validate_upload_file, self.conn, upload_id)
            if not validation.valid:
                with self.db_lock:
                    if video_id:
                        update_video(self.conn, video_id, upload_status="failed",
                                     error_message=f"{validation.error_code}: {validation.message}")
                return
            with self.db_lock:
                if video_id:
                    update_video(self.conn, video_id, upload_status="uploading")

            execution_conn = self._execution_connection()
            if execution_conn is None:
                with self.db_lock:
                    if video_id:
                        update_video(self.conn, video_id, upload_status="failed", error_message="in-memory database cannot run network uploads")
                    youtube.mark_upload_failed(self.conn, upload_id, "in-memory database cannot run network uploads")
                return
            try:
                result = await asyncio.to_thread(youtube.process_upload, execution_conn, upload_id)
                if result.get("status") == "done":
                    postprocess_result = await asyncio.to_thread(youtube.publish_completed_upload, execution_conn, upload_id)
            finally:
                if execution_conn is not self.conn:
                    execution_conn.close()

            with self.db_lock:
                if video_id and result.get("status") == "done":
                    update_video(
                        self.conn, video_id,
                        upload_status="uploaded",
                        youtube_video_id=result.get("youtube_video_id", ""),
                    )
            # Post-process: set thumbnail and add to playlist
            if result.get("status") == "done":
                with self.db_lock:
                    sync_pipeline_from_upload(self.conn, upload_id)
                if postprocess_result and postprocess_result.get("status") not in (None, "published", "done"):
                    with self.db_lock:
                        conn_row = self.conn.execute("SELECT error_message FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
                        if conn_row and not conn_row[0]:
                            self.conn.execute("UPDATE youtube_uploads SET error_message=? WHERE id=?", (postprocess_result.get("error") or postprocess_result["status"], upload_id)); self.conn.commit()
            else:
                with self.db_lock:
                    youtube.mark_upload_failed(self.conn, upload_id, result.get("error") or result.get("status") or "upload failed")
            logger.info("Upload %s done: %s", upload_id, result.get("youtube_video_id"))
        except Exception as e:
            logger.error("Upload %s failed: %s", upload_id, e)
            with self.db_lock:
                if video_id:
                    update_video(self.conn, video_id, upload_status="failed", error_message=str(e))
                youtube.mark_upload_failed(self.conn, upload_id, str(e))

    def _execution_connection(self) -> sqlite3.Connection | None:
        # Even this one-row PRAGMA has to take db_lock: a sqlite3 connection is not safe
        # for concurrent use, and the routes touch this same object from other threads.
        with self.db_lock:
            database = self.conn.execute("PRAGMA database_list").fetchone()[2]
        if not database or database == ":memory:":
            return None
        return db.connect(database)


# Singleton instance - set via init_worker()
upload_worker: UploadWorker | None = None


def init_worker(conn: sqlite3.Connection, db_lock: threading.Lock) -> UploadWorker:
    """Initialize and return the singleton upload worker."""
    global upload_worker
    upload_worker = UploadWorker(conn, db_lock)
    return upload_worker
