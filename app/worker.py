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

from app import audio_merge, repository, video_gen
from app.chunker import split_into_tts_chunks
from app.config import settings
from app.models import BookJob, Patch
from app.tts_engine import VoxCPMEngine

logger = logging.getLogger(__name__)


_WARNING_EVENTS = {"queue.paused", "worker.shutdown_timeout"}
_TITLE_END_PUNCTUATION = frozenset(".!?…:;,)]\"'»")


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
    current_chunk_index: int = 0
    current_chunk_count: int = 0

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
        self.current_chunk_index: int = 0
        self.current_chunk_count: int = 0
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
        # Track book_job tasks running in parallel with the main patch loop.
        # TTS synthesis stays sequential (GPU-bound), but video generation is
        # CPU-bound and can run concurrently with patches + with each other.
        self._bg_tasks: set[asyncio.Task] = set()

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
                    resume_from_chunk=patch.next_chunk_index,
                    total_chunks=patch.chunk_count,
                )
                self._in_flight = patch
                self.current_patch_id = patch.id
                self.current_chunk_index = patch.next_chunk_index
                self.current_chunk_count = patch.chunk_count
                self.state = "busy"
                try:
                    await self._process(patch)
                finally:
                    self._in_flight = None
                    self.current_patch_id = None
                    self.current_chunk_index = 0
                    self.current_chunk_count = 0
                    self.state = "idle"
                # After each patch, opportunistically start a book_job in the
                # background so video generation runs in parallel with TTS.
                self._spawn_book_job()
                continue

            # No pending patch — try to spawn a book_job.
            if self._spawn_book_job():
                continue

            self.state = "idle"
            await asyncio.sleep(self.poll_interval)

        # Loop exited cleanly. Wait for in-flight book_job tasks to finish.
        if self._bg_tasks:
            self._log_event("worker.draining_bg_tasks", count=len(self._bg_tasks))
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._log_event("worker.exit")

    def _spawn_book_job(self) -> bool:
        """Try to claim and dispatch a book_job as a background task. Returns
        True if a task was spawned, False if nothing was claimed."""
        with self.db_lock:
            job = repository.claim_next_pending_book_job(self.conn)
        if job is None:
            return False
        self._log_event(
            "book_job.claimed",
            book_job_id=job.id,
            book_id=job.book_id,
            job_type=job.job_type,
            attempt=job.attempt_count,
            mode="background",
        )
        task = asyncio.create_task(self._run_book_job_wrapper(job))
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return True

    async def _run_book_job_wrapper(self, job: BookJob) -> None:
        """Run a book_job in the background, tracking in_flight for graceful shutdown."""
        self._in_flight = job
        self.current_book_job_id = job.id
        self.state = "busy"
        try:
            await self._process_book_job(job)
        finally:
            self._in_flight = None
            self.current_book_job_id = None
            if not self._bg_tasks and self.current_patch_id is None:
                self.state = "idle"

    # ------------------------------------------------------------------ patches

    async def _process(self, patch: Patch) -> None:
        self._log_event(
            "patch.started",
            patch_id=patch.id,
            book_id=patch.book_id,
            attempt=patch.attempt_count,
            resume_from_chunk=patch.next_chunk_index,
            total_chunks=patch.chunk_count,
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
            chapters = repository.get_chapters_in_range(
                self.conn, patch.book_id, patch.chapter_start, patch.chapter_end
            )
            rules = repository.list_replace_rules(self.conn, patch.book_id)
            book = repository.get_book(self.conn, patch.book_id)

        included = [ch for ch in chapters if not ch.is_excluded]
        texts: list[str] = []
        for ch in included:
            t = ch.text
            if ch.title and t.startswith(ch.title) and ch.title[-1] not in _TITLE_END_PUNCTUATION:
                suffix = t[len(ch.title):].lstrip()
                if suffix:
                    t = ch.title + ".\n\n" + suffix
            texts.append(t)
        raw = "\n\n".join(texts)
        patch_text = repository.apply_replace_rules(raw, rules)

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
        try:
            chunks = split_into_tts_chunks(patch_text, max_chars=settings.tts_max_chars)
            with self.db_lock:
                repository.update_patch_chunk_count(self.conn, patch.id, len(chunks))
            start_index = max(0, min(patch.next_chunk_index, len(chunks)))
            if start_index > 0:
                self._log_event(
                    "chunk.resume",
                    patch_id=patch.id,
                    from_chunk=start_index,
                    total_chunks=len(chunks),
                )
            for i in range(start_index, len(chunks)):
                chunk_text = chunks[i]
                self.current_chunk_index = i
                arr = self.engine.synthesize_chunk(
                    chunk_text,
                    reference_wav_path=ref_wav,
                    prompt_text=ref_text,
                )
                chunk_path = str(chunk_dir / f"chunk_{i:03d}.wav")
                sf.write(chunk_path, arr, self.engine.sample_rate)
                self._log_event(
                    "chunk.written",
                    patch_id=patch.id,
                    chunk_index=i,
                    path=chunk_path,
                )
                with self.db_lock:
                    repository.update_patch_chunk_progress(self.conn, patch.id, i + 1)

            chunk_paths = [str(chunk_dir / f"chunk_{i:03d}.wav") for i in range(len(chunks))]
            audio_merge.merge_chunk_files_to_patch(chunk_paths, audio_path)
            self._log_event("chunk.merged", patch_id=patch.id)
            audio_merge.cleanup_chunk_dir(str(chunk_dir))
            self._log_event("chunk.cleaned", patch_id=patch.id)
            return audio_path
        except Exception:
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
        from app import youtube  # lazy import (google.auth is optional)

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

        def _on_progress(event: str, fields: dict) -> None:
            # Re-prefix with video_job context so log lines are easy to grep.
            self._log_event(
                f"video_job.{event}",
                book_job_id=job.id,
                book_id=job.book_id,
                **fields,
            )

        video_gen.generate_full_video(
            done_patches, book, out_path,
            default_image=settings.default_background_image,
            use_nvenc=settings.use_nvenc,
            on_progress=_on_progress,
        )
        return out_path

    # ------------------------------------------------------------------ helpers

    def _should_exit(self) -> bool:
        if not self._stop:
            return False
        # If we are mid-flight, finish the current job first; otherwise exit.
        if self._in_flight is not None:
            return False
        # Wait for background book_job tasks to drain before exiting.
        bg = getattr(self, "_bg_tasks", None)
        if bg:
            return False
        return True

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
