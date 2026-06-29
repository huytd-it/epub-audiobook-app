"""In-process background worker: drains two queues with patch > book_job priority.

* Patches (`patch` table) are chapter-range TTS jobs. Claimed and synthesized one at a
  time (TTS is GPU-bound, sequential matches the hardware).
* Book jobs (`book_job` table) are book-level operations. Currently only `video` —
  ffmpeg mux of the final audio onto a background image. Claimed only when no patch is
  pending, so patches always win priority, but the same worker handles both.

The sqlite3 connection is shared with FastAPI route handlers (same process, same db
file). sqlite3 connections are not safe for concurrent use across threads, and
synthesis runs in a worker thread via asyncio.to_thread while the event loop keeps
serving HTTP requests - so every access to `conn` goes through `db_lock`, shared with
the routes that touch the same connection.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

import soundfile as sf

from app import audio_merge, repository, video_gen, youtube
from app.chunker import split_into_tts_chunks
from app.config import settings
from app.models import BookJob, Patch
from app.tts_engine import VoxCPMEngine

logger = logging.getLogger(__name__)


_WARNING_EVENTS = {"queue.paused", "worker.shutdown_timeout"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DisabledWorker:
    """Sentinel used in place of PatchWorker when settings.enable_worker is False.

    Reports itself as 'disabled' on /health and short-circuits any worker-side
    operations. The DB recovery (requeue, backfill) and HTTP routes still run
    normally — only the background claim/synthesize loop is suppressed, which
    is what the user wants when uvicorn --reload is restarting the process on
    every file save.
    """

    state: str = "disabled"
    last_heartbeat_at: str = ""
    current_patch_id: int | None = None
    current_book_job_id: int | None = None

    def stop(self) -> None:
        pass

    def log_shutdown_timeout(self) -> None:
        pass


class PatchWorker:
    def __init__(
        self,
        conn: sqlite3.Connection,
        engine: VoxCPMEngine,
        data_root: str,
        poll_interval: float = 2.0,
        db_lock: threading.Lock | None = None,
        shutdown_timeout: float = 300.0,
    ):
        self.conn = conn
        self.engine = engine
        self.data_root = Path(data_root)
        self.poll_interval = poll_interval
        self.db_lock = db_lock or threading.Lock()
        self.shutdown_timeout = shutdown_timeout

        self._stop = False
        self._in_flight: Patch | BookJob | None = None

        # Observable state for /health. Updated by the loop and by _process* methods.
        self.state: str = "idle"  # 'idle' | 'busy' | 'paused'
        self.current_patch_id: int | None = None
        self.current_book_job_id: int | None = None
        self.last_heartbeat_at: str = _now_iso()

    # ------------------------------------------------------------------ public

    def stop(self) -> None:
        self._stop = True

    def log_shutdown_timeout(self) -> None:
        """Called by the FastAPI lifespan when wait_for times out, so the worker can
        record the event in its structured log stream."""
        self._log_event(
            "worker.shutdown_timeout",
            timeout_seconds=self.shutdown_timeout,
            level=logging.WARNING,
        )

    async def run_forever(self) -> None:
        while not self._should_exit():
            # Update heartbeat at the top of every iteration, regardless of work found.
            self.last_heartbeat_at = _now_iso()
            self._log_event("worker.heartbeat", level=logging.DEBUG)

            # Pause check
            with self.db_lock:
                paused = repository.is_queue_paused(self.conn)
            if paused:
                self.state = "paused"
                self._log_event("queue.paused", level=logging.WARNING)
                await asyncio.sleep(self.poll_interval)
                continue
            if self.state == "paused":
                self._log_event("queue.resumed")

            # Claim a patch first; if none, claim a book_job.
            with self.db_lock:
                patch = repository.claim_next_pending_patch(self.conn)
            if patch is not None:
                self._log_event(
                    "patch.claimed",
                    patch_id=patch.id,
                    book_id=patch.book_id,
                    attempt=patch.attempt_count,
                )
                self._in_flight = patch
                self.current_patch_id = patch.id
                self.state = "busy"
                try:
                    await self._process(patch)
                finally:
                    self._in_flight = None
                    self.current_patch_id = None
                    self.state = "idle"
                continue

            with self.db_lock:
                job = repository.claim_next_pending_book_job(self.conn)
            if job is not None:
                self._log_event(
                    "book_job.claimed",
                    book_job_id=job.id,
                    book_id=job.book_id,
                    job_type=job.job_type,
                    attempt=job.attempt_count,
                )
                self._in_flight = job
                self.current_book_job_id = job.id
                self.state = "busy"
                try:
                    await self._process_book_job(job)
                finally:
                    self._in_flight = None
                    self.current_book_job_id = None
                    self.state = "idle"
                continue

            self.state = "idle"
            await asyncio.sleep(self.poll_interval)

        # Loop exited cleanly. The lifespan will see the task finish.
        self._log_event("worker.exit")

    # ------------------------------------------------------------------ patches

    async def _process(self, patch: Patch) -> None:
        self._log_event(
            "patch.started",
            patch_id=patch.id,
            book_id=patch.book_id,
            attempt=patch.attempt_count,
        )
        try:
            audio_path = await asyncio.to_thread(self._synthesize, patch)
            with self.db_lock:
                repository.mark_patch_done(self.conn, patch.id, audio_path)
            self._log_event(
                "patch.done",
                patch_id=patch.id,
                book_id=patch.book_id,
                output_path=audio_path,
            )
            await self._maybe_finalize_book(patch.book_id)
        except Exception as exc:  # noqa: BLE001 - one bad patch must not stop the queue
            logger.exception("patch %s failed", patch.id)
            with self.db_lock:
                repository.mark_patch_failed(self.conn, patch.id, str(exc))
            self._log_event(
                "patch.failed",
                patch_id=patch.id,
                book_id=patch.book_id,
                attempt=patch.attempt_count,
                error=str(exc),
                level=logging.ERROR,
            )

    def _synthesize(self, patch: Patch) -> str:
        """Blocking: runs in a thread via asyncio.to_thread so the event loop (and thus the
        web UI) isn't frozen during synthesis."""
        with self.db_lock:
            patch_text = repository.build_patch_text(self.conn, patch)
            book = repository.get_book(self.conn, patch.book_id)

        ref_wav = book.voice_clip_path if book else None
        ref_text = book.voice_transcript if book else None

        book_dir = self.data_root / "books" / str(patch.book_id) / "patches"
        book_dir.mkdir(parents=True, exist_ok=True)
        audio_path = str(book_dir / f"{patch.id}.wav")

        if not settings.tts_write_chunk_files:
            wavs = self.engine.synthesize_patch(
                patch_text,
                reference_wav_path=ref_wav,
                prompt_text=ref_text,
            )
            audio_merge.concat_chunks_to_wav(wavs, self.engine.sample_rate, audio_path)
            return audio_path

        chunk_dir = book_dir / f"{patch.id}_chunks"
        chunk_dir.mkdir(parents=True, exist_ok=True)
        chunk_paths: list[str] = []
        try:
            chunks = split_into_tts_chunks(patch_text, max_chars=settings.tts_max_chars)
            for i, chunk_text in enumerate(chunks):
                arr = self.engine.synthesize_chunk(
                    chunk_text,
                    reference_wav_path=ref_wav,
                    prompt_text=ref_text,
                )
                chunk_path = str(chunk_dir / f"chunk_{i:03d}.wav")
                sf.write(chunk_path, arr, self.engine.sample_rate)
                chunk_paths.append(chunk_path)
                self._log_event(
                    "chunk.written",
                    patch_id=patch.id,
                    chunk_index=i,
                    path=chunk_path,
                )

            audio_merge.merge_chunk_files_to_patch(chunk_paths, audio_path)
            self._log_event("chunk.merged", patch_id=patch.id)
            audio_merge.cleanup_chunk_dir(str(chunk_dir))
            self._log_event("chunk.cleaned", patch_id=patch.id)
            return audio_path
        except Exception:
            audio_merge.cleanup_chunk_dir(str(chunk_dir))
            raise

    async def _maybe_finalize_book(self, book_id: int) -> None:
        with self.db_lock:
            done = repository.all_patches_done(self.conn, book_id)
        if not done:
            return
        await asyncio.to_thread(self._merge_final_audio, book_id)

    def _merge_final_audio(self, book_id: int) -> None:
        with self.db_lock:
            patches = repository.list_patches(self.conn, book_id)
            book = repository.get_book(self.conn, book_id)
        patch_wav_paths = [p.audio_path for p in patches if p.audio_path]
        if len(patch_wav_paths) != len(patches):
            return  # shouldn't happen if all_patches_done was true, but be defensive

        book_dir = self.data_root / "books" / str(book_id)
        book_dir.mkdir(parents=True, exist_ok=True)
        final_path = str(book_dir / "final.wav")
        audio_merge.merge_patches_to_final(patch_wav_paths, final_path)
        with self.db_lock:
            repository.set_book_final_audio(self.conn, book_id, final_path)
        self._log_event("book.finalized", book_id=book_id, final_audio_path=final_path)

        # Auto-enqueue a video book_job if the book has any usable image.
        if book is not None:
            has_bg = bool(book.background_image_path)
            has_patch_img = any(p.image_path for p in patches)
            if has_bg or has_patch_img:
                with self.db_lock:
                    repository.enqueue_book_job(self.conn, book_id, "video")
                self._log_event(
                    "book_job.auto_enqueued",
                    book_id=book_id,
                    job_type="video",
                )

    # ------------------------------------------------------------------ book jobs

    async def _process_book_job(self, job: BookJob) -> None:
        self._log_event(
            "book_job.started",
            book_job_id=job.id,
            book_id=job.book_id,
            job_type=job.job_type,
            attempt=job.attempt_count,
        )
        try:
            if job.job_type == "video":
                output_path = await asyncio.to_thread(self._run_video_job, job)
            else:
                raise ValueError(f"unknown book_job type: {job.job_type!r}")
            with self.db_lock:
                repository.mark_book_job_done(self.conn, job.id, output_path)
            # Mirror the output path onto the book row for the existing download route.
            with self.db_lock:
                repository.set_book_final_video(self.conn, job.book_id, output_path)
            self._log_event(
                "book_job.done",
                book_job_id=job.id,
                book_id=job.book_id,
                job_type=job.job_type,
                output_path=output_path,
            )

            # Auto-upload to YouTube if configured
            if settings.youtube_auto_upload and youtube.is_configured():
                try:
                    with self.db_lock:
                        book = repository.get_book(self.conn, job.book_id)
                    if book:
                        tags = [t.strip() for t in settings.youtube_default_tags.split(",") if t.strip()]
                        with self.db_lock:
                            upload_id = youtube.enqueue_upload(
                                self.conn,
                                video_path=output_path,
                                title=book.title,
                                description=f"{book.title} - EPUB Audiobook",
                                tags=tags,
                                privacy_status=settings.youtube_default_privacy,
                            )
                            result = youtube.upload_video(
                                self.conn,
                                video_path=output_path,
                                title=book.title,
                                description=f"{book.title} - EPUB Audiobook",
                                tags=tags,
                                privacy_status=settings.youtube_default_privacy,
                            )
                        self._log_event(
                            "youtube.upload_done",
                            upload_id=upload_id,
                            youtube_video_id=result.get("youtube_video_id", ""),
                            book_id=job.book_id,
                        )
                except Exception as yt_exc:
                    self._log_event(
                        "youtube.upload_failed",
                        book_id=job.book_id,
                        error=str(yt_exc),
                        level=logging.WARNING,
                    )
        except Exception as exc:  # noqa: BLE001
            logger.exception("book_job %s failed", job.id)
            with self.db_lock:
                repository.mark_book_job_failed(self.conn, job.id, str(exc))
            self._log_event(
                "book_job.failed",
                book_job_id=job.id,
                book_id=job.book_id,
                job_type=job.job_type,
                attempt=job.attempt_count,
                error=str(exc),
                level=logging.ERROR,
            )

    def _run_video_job(self, job: BookJob) -> str:
        """Blocking: runs in a thread via asyncio.to_thread."""
        with self.db_lock:
            book = repository.get_book(self.conn, job.book_id)
            patches = repository.list_patches(self.conn, job.book_id)
        if book is None or not book.final_audio_path:
            raise ValueError(f"book {job.book_id} has no final_audio_path")

        done_patches = [p for p in patches if p.status == "done" and p.audio_path]
        book_dir = self.data_root / "books" / str(job.book_id)
        book_dir.mkdir(parents=True, exist_ok=True)
        out_path = str(book_dir / f"video_{job.id}.mp4")

        video_gen.generate_full_video(
            done_patches, book, out_path,
            default_image=settings.default_background_image,
            use_nvenc=settings.use_nvenc,
        )
        return out_path

    # ------------------------------------------------------------------ helpers

    def _should_exit(self) -> bool:
        if not self._stop:
            return False
        # If we are mid-flight, finish the current job first; otherwise exit.
        return self._in_flight is None

    def _log_event(
        self, event: str, *, level: int | None = None, **fields
    ) -> None:
        parts = [f"event={event}"]
        for k, v in fields.items():
            if isinstance(v, str) and (any(c.isspace() for c in v) or '"' in v):
                escaped = v.replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'{k}="{escaped}"')
            else:
                parts.append(f"{k}={v}")
        msg = " ".join(parts)
        if level is None:
            if event.endswith(".failed"):
                level = logging.ERROR
            elif event in _WARNING_EVENTS:
                level = logging.WARNING
            else:
                level = logging.INFO
        logger.log(level, msg)
