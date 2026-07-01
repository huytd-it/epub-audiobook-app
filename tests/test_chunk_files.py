"""Unit tests for chunk-level audio file helpers and worker integration."""
from __future__ import annotations

import sqlite3
from unittest.mock import MagicMock

import numpy as np
import pytest
import soundfile as sf

from app import audio_merge, db as app_db
from app.config import settings
from app.models import Patch
from app.worker import PatchWorker


class FakeEngine:
    """Lightweight stand-in for VoxCPMEngine that returns synthetic audio."""

    sample_rate = 16000

    def synthesize_chunk(self, text, *, reference_wav_path=None, prompt_text=None):
        return np.sin(2 * np.pi * 440 * np.arange(8000) / self.sample_rate).astype(np.float32) * 0.1

    def synthesize_patch(self, text, *, max_chars=400, reference_wav_path=None, prompt_text=None):
        from app.chunker import split_into_tts_chunks

        return [self.synthesize_chunk(c) for c in split_into_tts_chunks(text, max_chars=max_chars)]


@pytest.fixture
def fake_engine():
    return FakeEngine()


@pytest.fixture
def tmp_audio_dir(tmp_path):
    return str(tmp_path)


# ---------------------------------------------------------------------------
# Unit: merge_chunk_files_to_patch
# ---------------------------------------------------------------------------


def test_merge_chunk_files_to_patch(tmp_audio_dir):
    sr = 16000
    chunk1 = np.sin(2 * np.pi * 440 * np.arange(4000) / sr).astype(np.float32) * 0.1
    chunk2 = np.sin(2 * np.pi * 880 * np.arange(6000) / sr).astype(np.float32) * 0.1

    p1 = tmp_audio_dir + "/chunk_000.wav"
    p2 = tmp_audio_dir + "/chunk_001.wav"
    sf.write(p1, chunk1, sr)
    sf.write(p2, chunk2, sr)

    out_path = tmp_audio_dir + "/merged.wav"
    audio_merge.merge_chunk_files_to_patch([p1, p2], out_path)

    merged, merged_sr = sf.read(out_path, dtype="float32")
    assert merged_sr == sr
    expected = np.concatenate([chunk1, chunk2])
    assert merged.shape[0] == expected.shape[0]
    assert np.allclose(merged, expected, atol=5e-4)


def test_merge_chunk_files_single_chunk(tmp_audio_dir):
    sr = 16000
    chunk = np.ones(1000, dtype=np.float32)
    p = tmp_audio_dir + "/chunk_000.wav"
    sf.write(p, chunk, sr)
    out_path = tmp_audio_dir + "/merged.wav"
    audio_merge.merge_chunk_files_to_patch([p], out_path)
    merged, merged_sr = sf.read(out_path, dtype="float32")
    assert merged_sr == sr
    assert merged.shape[0] == chunk.shape[0]
    assert np.allclose(merged, chunk, atol=5e-4)


def test_merge_chunk_files_empty_raises():
    with pytest.raises(ValueError, match="no chunk paths"):
        audio_merge.merge_chunk_files_to_patch([], "/nonexistent.wav")


# ---------------------------------------------------------------------------
# Unit: cleanup_chunk_dir
# ---------------------------------------------------------------------------


def test_cleanup_chunk_dir_removes_directory(tmp_audio_dir):
    import os

    d = tmp_audio_dir + "/test_chunks"
    os.makedirs(d, exist_ok=True)
    with open(d + "/chunk_000.wav", "w") as f:
        f.write("dummy")
    assert os.path.isdir(d)
    audio_merge.cleanup_chunk_dir(d)
    assert not os.path.isdir(d)


def test_cleanup_chunk_dir_nonexistent_does_not_raise(tmp_audio_dir):
    audio_merge.cleanup_chunk_dir(tmp_audio_dir + "/nonexistent_cleanup")


# ---------------------------------------------------------------------------
# Integration: worker._synthesize with toggle on and off
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    app_db.init_schema(c)
    now = "2026-01-01T00:00:00+00:00"
    cur = c.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES ('test', 't.epub', 't.epub', 10, 'ready', ?, ?)",
        (now, now),
    )
    book_id = cur.lastrowid
    chapter_count = 1
    for i in range(chapter_count):
        c.execute(
            "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, i, f"Ch{i}", "Hello world. This is a test sentence. Another sentence here.", 60),
        )
    c.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
        "VALUES (?, 0, 0, 0, 'pending', ?, ?)",
        (book_id, now, now),
    )
    c.commit()
    yield c
    c.close()


def _make_worker(conn, engine, data_root, monkeypatch):
    import threading

    lock = threading.Lock()
    return PatchWorker(
        conn=conn,
        engine=engine,
        data_root=data_root,
        poll_interval=0.1,
        db_lock=lock,
        shutdown_timeout=1.0,
    )


def _read_patch(tmp_audio_dir, conn):
    import os

    patches_dir = os.path.join(tmp_audio_dir, "books", "1", "patches")
    files = os.listdir(patches_dir) if os.path.isdir(patches_dir) else []
    return files


def test_synthesize_with_chunk_files(tmp_audio_dir, seeded_conn, fake_engine, monkeypatch):
    """Integration: when toggle is ON, chunk files appear and are kept on disk after a
    successful merge (not auto-deleted - see worker.py _synthesize for why: a bad merge
    wouldn't necessarily raise, so deleting the source chunks immediately would make that
    unrecoverable). They're only removed later via an explicit regenerate/reset/delete."""
    worker = _make_worker(seeded_conn, fake_engine, tmp_audio_dir, monkeypatch)

    # Force toggle ON via monkeypatch on the module-level settings
    import app.worker as worker_mod
    import app.config as config_mod

    monkeypatch.setattr(config_mod.settings, "tts_write_chunk_files", True)

    # Re-import/refresh worker's local settings reference (it uses from app.config import settings)
    # Since worker imports settings at module level, we need to reload or patch the worker module's reference
    monkeypatch.setattr(worker_mod, "settings", config_mod.settings)

    patch_row = seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone()
    patch = Patch(
        id=patch_row["id"],
        book_id=patch_row["book_id"],
        patch_index=patch_row["patch_index"],
        chapter_start=patch_row["chapter_start"],
        chapter_end=patch_row["chapter_end"],
        status=patch_row["status"],
        audio_path=patch_row["audio_path"],
        error_message=patch_row["error_message"],
        attempt_count=patch_row["attempt_count"],
        created_at=patch_row["created_at"],
        updated_at=patch_row["updated_at"],
    )

    audio_path = worker._synthesize(patch)

    import os

    assert os.path.isfile(audio_path), f"patch WAV not found at {audio_path}"
    # Chunk dir (and its per-chunk wav files) should still be there after a successful merge.
    chunk_dir = os.path.join(tmp_audio_dir, "books", "1", "patches", "1_chunks")
    assert os.path.isdir(chunk_dir), f"chunk dir should be kept after merge: {chunk_dir}"
    assert os.path.isfile(os.path.join(chunk_dir, "chunk_000.wav"))

    # Verify patch WAV is valid
    data, sr = sf.read(audio_path)
    assert sr == 16000
    assert data.shape[0] > 0


def test_synthesize_without_chunk_files(tmp_audio_dir, seeded_conn, fake_engine, monkeypatch):
    """When toggle is OFF, uses in-memory path — no chunk files created."""
    worker = _make_worker(seeded_conn, fake_engine, tmp_audio_dir, monkeypatch)

    import app.worker as worker_mod
    import app.config as config_mod

    monkeypatch.setattr(config_mod.settings, "tts_write_chunk_files", False)
    monkeypatch.setattr(worker_mod, "settings", config_mod.settings)

    patch_row = seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone()
    patch = Patch(
        id=patch_row["id"],
        book_id=patch_row["book_id"],
        patch_index=patch_row["patch_index"],
        chapter_start=patch_row["chapter_start"],
        chapter_end=patch_row["chapter_end"],
        status=patch_row["status"],
        audio_path=patch_row["audio_path"],
        error_message=patch_row["error_message"],
        attempt_count=patch_row["attempt_count"],
        created_at=patch_row["created_at"],
        updated_at=patch_row["updated_at"],
    )

    audio_path = worker._synthesize(patch)

    import os

    assert os.path.isfile(audio_path)
    # Chunk dir must NOT exist when toggle is off
    chunk_dir = os.path.join(tmp_audio_dir, "books", "1", "patches", "1_chunks")
    assert not os.path.isdir(chunk_dir), "chunk dir should not exist when toggle is off"
