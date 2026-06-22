"""In-process background worker: polls SQLite for pending patches, synthesizes them sequentially
(TTS is GPU-bound, so one-at-a-time matches the hardware), and finalizes a book's merged audio
once every patch is done."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
from pathlib import Path

from app import audio_merge, repository
from app.tts_engine import VoxCPMEngine

logger = logging.getLogger(__name__)


class PatchWorker:
    """The sqlite3 connection is shared with FastAPI route handlers (same process, same db file).
    sqlite3 connections are not safe for concurrent use across threads even with
    check_same_thread=False, and synthesis runs in a worker thread via asyncio.to_thread while
    the event loop keeps serving HTTP requests - so every access to `conn` must go through
    `db_lock`, shared with the routes that touch the same connection."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        engine: VoxCPMEngine,
        data_root: str,
        poll_interval: float = 2.0,
        db_lock: threading.Lock | None = None,
    ):
        self.conn = conn
        self.engine = engine
        self.data_root = Path(data_root)
        self.poll_interval = poll_interval
        self.db_lock = db_lock or threading.Lock()
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    async def run_forever(self) -> None:
        while not self._stop:
            with self.db_lock:
                patch = repository.claim_next_pending_patch(self.conn)
            if patch is None:
                await asyncio.sleep(self.poll_interval)
                continue
            await self._process(patch)

    async def _process(self, patch) -> None:
        try:
            audio_path = await asyncio.to_thread(self._synthesize, patch)
            with self.db_lock:
                repository.mark_patch_done(self.conn, patch.id, audio_path)
            logger.info("patch %s done -> %s", patch.id, audio_path)
            await self._maybe_finalize_book(patch.book_id)
        except Exception as exc:  # noqa: BLE001 - one bad patch must not stop the queue
            logger.exception("patch %s failed", patch.id)
            with self.db_lock:
                repository.mark_patch_failed(self.conn, patch.id, str(exc))

    def _synthesize(self, patch) -> str:
        """Blocking: runs in a thread via asyncio.to_thread so the event loop (and thus the
        web UI) isn't frozen during synthesis."""
        with self.db_lock:
            patch_text = repository.build_patch_text(self.conn, patch)
            book = repository.get_book(self.conn, patch.book_id)

        wavs = self.engine.synthesize_patch(
            patch_text,
            reference_wav_path=book.voice_clip_path if book else None,
            prompt_text=book.voice_transcript if book else None,
        )

        book_dir = self.data_root / "books" / str(patch.book_id) / "patches"
        book_dir.mkdir(parents=True, exist_ok=True)
        audio_path = str(book_dir / f"{patch.id}.wav")
        audio_merge.concat_chunks_to_wav(wavs, self.engine.sample_rate, audio_path)
        return audio_path

    async def _maybe_finalize_book(self, book_id: int) -> None:
        with self.db_lock:
            done = repository.all_patches_done(self.conn, book_id)
        if not done:
            return
        await asyncio.to_thread(self._merge_final_audio, book_id)

    def _merge_final_audio(self, book_id: int) -> None:
        with self.db_lock:
            patches = repository.list_patches(self.conn, book_id)
        patch_wav_paths = [p.audio_path for p in patches if p.audio_path]
        if len(patch_wav_paths) != len(patches):
            return  # shouldn't happen if all_patches_done was true, but be defensive

        book_dir = self.data_root / "books" / str(book_id)
        book_dir.mkdir(parents=True, exist_ok=True)
        final_path = str(book_dir / "final.wav")
        audio_merge.merge_patches_to_final(patch_wav_paths, final_path)
        with self.db_lock:
            repository.set_book_final_audio(self.conn, book_id, final_path)
        logger.info("book %s finalized -> %s", book_id, final_path)
