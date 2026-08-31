"""Unit tests for chunk-level audio file helpers and worker integration."""
from __future__ import annotations

import sqlite3
import json
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest
import soundfile as sf

from app import audio_merge, db as app_db
from app.config import settings
from app.models import Patch
from app.worker import PatchWorker
from app import repository


class FakeEngine:
    """Lightweight stand-in for VoxCPMEngine that returns synthetic audio."""

    sample_rate = 16000

    def __init__(self):
        self.chunk_texts = []

    def synthesize_chunk(self, text, *, reference_wav_path=None, prompt_text=None):
        self.chunk_texts.append(text)
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
    audio_merge.concat_wavs([p1, p2], out_path)

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
    audio_merge.concat_wavs([p], out_path)
    merged, merged_sr = sf.read(out_path, dtype="float32")
    assert merged_sr == sr
    assert merged.shape[0] == chunk.shape[0]
    assert np.allclose(merged, chunk, atol=5e-4)


def test_merge_chunk_files_empty_raises():
    with pytest.raises(ValueError, match="no input paths"):
        audio_merge.concat_wavs([], "/nonexistent.wav")


def test_concat_chunks_empty_with_pause_writes_empty_mono_audio(tmp_audio_dir):
    out_path = tmp_audio_dir + "/empty.wav"
    audio_merge.concat_chunks_to_wav([], 1000, out_path, pause_ms=300)
    data, sr = sf.read(out_path, dtype="float32")
    assert sr == 1000
    assert data.shape == (0,)


def test_concat_chunks_inserts_exact_300ms_silence_between_chunks(tmp_audio_dir):
    sr = 1000
    out_path = tmp_audio_dir + "/merged.wav"
    audio_merge.concat_chunks_to_wav(
        [np.ones(2, dtype=np.float32) * 0.25, np.ones(3, dtype=np.float32) * 0.5],
        sr,
        out_path,
        pause_ms=300,
    )

    merged, merged_sr = sf.read(out_path, dtype="float32")
    assert merged_sr == sr
    assert merged.shape == (305,)
    assert np.allclose(merged[:2], 0.25, atol=5e-4)
    assert np.array_equal(merged[2:302], np.zeros(300, dtype=np.float32))
    assert np.allclose(merged[302:], 0.5, atol=5e-4)


def test_concat_chunks_default_has_no_pause(tmp_audio_dir):
    out_path = tmp_audio_dir + "/merged.wav"
    audio_merge.concat_chunks_to_wav(
        [np.ones(2, dtype=np.float32) * 0.25, np.ones(3, dtype=np.float32) * 0.5],
        1000,
        out_path,
    )

    merged, _ = sf.read(out_path, dtype="float32")
    assert np.allclose(merged, np.array([0.25, 0.25, 0.5, 0.5, 0.5], dtype=np.float32), atol=5e-4)


def test_concat_chunks_supports_multichannel_and_rounds_pause_frames(tmp_audio_dir):
    out_path = tmp_audio_dir + "/merged.wav"
    first = np.ones((2, 3), dtype=np.float32) * 0.25
    second = np.ones((1, 3), dtype=np.float32) * 0.5
    audio_merge.concat_chunks_to_wav([first, second], 44100, out_path, pause_ms=1)

    merged, _ = sf.read(out_path, dtype="float32")
    assert merged.shape == (47, 3)
    assert np.array_equal(merged[2:46], np.zeros((44, 3), dtype=np.float32))


@pytest.mark.parametrize("chunks", [[np.array(1.0)], [np.zeros(2), np.zeros((2, 1))]])
def test_concat_chunks_rejects_invalid_shapes_before_touching_output(tmp_audio_dir, chunks):
    out_path = Path(tmp_audio_dir) / "existing.wav"
    out_path.write_bytes(b"keep")
    with pytest.raises(ValueError, match="same channel shape|one or two dimensions"):
        audio_merge.concat_chunks_to_wav(chunks, 1000, str(out_path))
    assert out_path.read_bytes() == b"keep"


def test_concat_wavs_preserves_stereo_and_inserts_pause(tmp_audio_dir):
    sr = 1000
    first = np.ones((2, 2), dtype=np.float32) * 0.25
    second = np.ones((3, 2), dtype=np.float32) * 0.5
    p1 = tmp_audio_dir + "/first.wav"
    p2 = tmp_audio_dir + "/second.wav"
    out_path = tmp_audio_dir + "/merged.wav"
    sf.write(p1, first, sr)
    sf.write(p2, second, sr)

    audio_merge.concat_wavs([p1, p2], out_path, pause_ms=300)

    merged, merged_sr = sf.read(out_path, dtype="float32")
    assert merged_sr == sr
    assert merged.shape == (305, 2)
    assert np.allclose(merged[:2], first, atol=5e-4)
    assert np.array_equal(merged[2:302], np.zeros((300, 2), dtype=np.float32))
    assert np.allclose(merged[302:], second, atol=5e-4)


def test_concat_wavs_rejects_mismatched_metadata(tmp_audio_dir):
    p1 = tmp_audio_dir + "/first.wav"
    p2 = tmp_audio_dir + "/second.wav"
    sf.write(p1, np.ones((2, 2), dtype=np.float32), 1000)
    sf.write(p2, np.ones((2, 2), dtype=np.float32), 2000)

    with pytest.raises(ValueError, match="samplerate/channels"):
        audio_merge.concat_wavs([p1, p2], tmp_audio_dir + "/merged.wav")


def test_concat_wavs_rejects_channel_mismatch_without_truncating_output(tmp_audio_dir):
    p1 = tmp_audio_dir + "/first.wav"
    p2 = tmp_audio_dir + "/second.wav"
    out_path = tmp_audio_dir + "/merged.wav"
    sf.write(p1, np.ones((2, 2), dtype=np.float32) * 0.25, 1000)
    sf.write(p2, np.ones((2, 1), dtype=np.float32) * 0.5, 1000)
    with open(out_path, "wb") as output:
        output.write(b"existing output")

    with pytest.raises(ValueError, match="samplerate/channels"):
        audio_merge.concat_wavs([p1, p2], out_path)
    assert open(out_path, "rb").read() == b"existing output"


def test_concat_wavs_validates_three_file_boundaries_before_streaming(tmp_audio_dir):
    paths = [tmp_audio_dir + f"/part_{i}.wav" for i in range(3)]
    for path in paths:
        sf.write(path, np.ones(1, dtype=np.float32) * 0.25, 1000)
    sf.write(paths[2], np.ones((1, 2), dtype=np.float32) * 0.25, 1000)
    out_path = tmp_audio_dir + "/merged.wav"
    with open(out_path, "wb") as output:
        output.write(b"keep me")

    with pytest.raises(ValueError, match="samplerate/channels"):
        audio_merge.concat_wavs(paths, out_path, pause_ms=1)
    assert open(out_path, "rb").read() == b"keep me"


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


@pytest.mark.parametrize("write_chunk_files", [True, False])
def test_worker_synthesizes_each_chapter_separately(tmp_audio_dir, seeded_conn, fake_engine, monkeypatch, write_chunk_files):
    now = "2026-01-01T00:00:00+00:00"
    seeded_conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (1, 1, 'Ch1', 'Second chapter.', 15)"
    )
    seeded_conn.execute("UPDATE patch SET chapter_end = 1, max_chars = 400 WHERE id = 1")
    seeded_conn.commit()
    import app.worker as worker_mod
    monkeypatch.setattr(worker_mod.settings, "tts_write_chunk_files", write_chunk_files)
    worker = _make_worker(seeded_conn, fake_engine, tmp_audio_dir, monkeypatch)
    worker._synthesize(Patch(**dict(seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone())))
    # Mỗi chương mở đầu bằng chính tiêu đề của nó: EPUB tách heading khỏi thân
    # chương, nên tiêu đề chỉ được đọc nếu prepare_chapter_tts_text ghép vào.
    assert fake_engine.chunk_texts == [
        "Ch0. Hello world. This is a test sentence. Another sentence here.",
        "Ch1. Second chapter.",
    ]


@pytest.mark.parametrize("write_chunk_files", [True, False])
def test_worker_writes_frame_timeline_and_300ms_chunk_pause(tmp_audio_dir, seeded_conn, fake_engine, monkeypatch, write_chunk_files):
    seeded_conn.execute("INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (1, 1, 'Ch1', 'Second chapter.', 15)")
    seeded_conn.execute("UPDATE patch SET chapter_end = 1, max_chars = 400 WHERE id = 1")
    seeded_conn.commit()
    import app.worker as worker_mod
    monkeypatch.setattr(worker_mod.settings, "tts_write_chunk_files", write_chunk_files)
    worker = _make_worker(seeded_conn, fake_engine, tmp_audio_dir, monkeypatch)
    patch = Patch(**dict(seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone()))
    audio_path = worker._synthesize(patch)
    timeline = json.loads(Path(audio_path).with_suffix(".timeline.json").read_text())
    assert timeline["version"] == 1
    assert timeline["total_frames"] == sf.info(audio_path).frames
    assert [c["start_frame"] for c in timeline["chapters"]] == [0, 8000 + 4800]


def test_worker_preserves_timeline_when_merge_fails(tmp_audio_dir, seeded_conn, fake_engine, monkeypatch):
    import app.worker as worker_mod
    monkeypatch.setattr(worker_mod.settings, "tts_write_chunk_files", False)
    worker = _make_worker(seeded_conn, fake_engine, tmp_audio_dir, monkeypatch)
    patch = Patch(**dict(seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone()))
    audio_path = worker._synthesize(patch)
    sidecar = Path(audio_path).with_suffix(".timeline.json")
    original = sidecar.read_bytes()
    monkeypatch.setattr(audio_merge, "concat_chunks_to_wav", MagicMock(side_effect=RuntimeError("merge")))
    with pytest.raises(RuntimeError):
        worker._synthesize(patch)
    assert sidecar.read_bytes() == original


def test_worker_returns_audio_when_timeline_replace_fails(tmp_audio_dir, seeded_conn, fake_engine, monkeypatch, caplog):
    import app.worker as worker_mod
    monkeypatch.setattr(worker_mod.settings, "tts_write_chunk_files", False)
    worker = _make_worker(seeded_conn, fake_engine, tmp_audio_dir, monkeypatch)
    patch = Patch(**dict(seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone()))
    audio_path = worker._synthesize(patch)
    sidecar = Path(audio_path).with_suffix(".timeline.json")
    sidecar.write_text("old", encoding="utf-8")
    monkeypatch.setattr(audio_merge.os, "replace", MagicMock(side_effect=OSError("replace failed")))
    with caplog.at_level("WARNING"):
        result = worker._synthesize(patch)
    assert result == audio_path
    assert Path(audio_path).exists()
    assert sidecar.read_text(encoding="utf-8") == "old"
    assert "timeline" in caplog.text


def test_worker_returns_audio_when_timeline_failure_and_stale_delete_fail(
    tmp_audio_dir, seeded_conn, fake_engine, monkeypatch, caplog
):
    import app.worker as worker_mod
    monkeypatch.setattr(worker_mod.settings, "tts_write_chunk_files", False)
    worker = _make_worker(seeded_conn, fake_engine, tmp_audio_dir, monkeypatch)
    patch = Patch(**dict(seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone()))
    audio_path = worker._synthesize(patch)
    sidecar = Path(audio_path).with_suffix(".timeline.json")
    sidecar.write_text("old", encoding="utf-8")
    monkeypatch.setattr(audio_merge.os, "replace", MagicMock(side_effect=OSError("replace failed")))
    original_unlink = Path.unlink

    def refuse_sidecar(path, *args, **kwargs):
        if path == sidecar:
            raise OSError("sidecar delete failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_sidecar)
    with caplog.at_level("WARNING"):
        result = worker._synthesize(patch)
    assert result == audio_path
    assert Path(audio_path).exists()
    assert "timeline" in caplog.text


def test_worker_skips_chapter_that_becomes_newline_or_tab_only_after_replacement(
    seeded_conn, fake_engine
):
    repository.create_replace_rule(seeded_conn, 1, "REMOVE", "\n\t", False, 0)
    seeded_conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (1, 1, 'Ch2', 'REMOVE', 6)"
    )
    seeded_conn.execute("UPDATE patch SET chapter_end = 1")
    seeded_conn.commit()
    patch = Patch(**dict(seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone()))
    assert all(item["chapter_index"] != 1 for item in repository.build_patch_chunk_plan(seeded_conn, patch))


def test_worker_skips_punctuation_only_chapter_after_replacement(seeded_conn, fake_engine):
    repository.create_replace_rule(seeded_conn, 1, "REMOVE", "!?…", False, 0)
    seeded_conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (1, 1, 'Ch2', 'REMOVE', 6)"
    )
    seeded_conn.execute("UPDATE patch SET chapter_end = 1")
    seeded_conn.commit()
    patch = Patch(**dict(seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone()))
    assert all(item["chapter_index"] != 1 for item in repository.build_patch_chunk_plan(seeded_conn, patch))


def test_delete_patch_audio_files_attempts_both_paths_on_error(tmp_audio_dir, monkeypatch):
    wav = Path(tmp_audio_dir) / "patch.wav"
    sidecar = wav.with_suffix(".timeline.json")
    wav.write_bytes(b"wav")
    sidecar.write_text("{}", encoding="utf-8")
    original_unlink = Path.unlink
    calls = []

    def unlink(path, *args, **kwargs):
        calls.append(path)
        if path == wav:
            raise OSError("wav failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(OSError, match="wav failure"):
        repository.delete_patch_audio_files(str(wav))
    assert wav in calls
    assert sidecar in calls
    assert not sidecar.exists()


def test_delete_patch_audio_files_rethrows_sidecar_error_after_wav_delete(tmp_audio_dir, monkeypatch):
    wav = Path(tmp_audio_dir) / "patch.wav"
    sidecar = wav.with_suffix(".timeline.json")
    wav.write_bytes(b"wav")
    sidecar.write_text("{}", encoding="utf-8")
    original_unlink = Path.unlink

    def refuse_sidecar(path, *args, **kwargs):
        if path == sidecar:
            raise OSError("sidecar failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", refuse_sidecar)
    with pytest.raises(OSError, match="sidecar failure"):
        repository.delete_patch_audio_files(str(wav))
    assert not wav.exists()
    assert sidecar.exists()


def test_delete_patch_audio_files_removes_wav_and_timeline(tmp_audio_dir):
    wav = Path(tmp_audio_dir) / "patch.wav"
    sidecar = wav.with_suffix(".timeline.json")
    wav.write_bytes(b"wav")
    sidecar.write_text("{}", encoding="utf-8")
    repository.delete_patch_audio_files(str(wav))
    assert not wav.exists()
    assert not sidecar.exists()


def test_worker_resume_counts_existing_chunk_frames(tmp_audio_dir, seeded_conn, fake_engine, monkeypatch):
    import app.worker as worker_mod
    monkeypatch.setattr(worker_mod.settings, "tts_write_chunk_files", True)
    chunk_dir = Path(tmp_audio_dir) / "books" / "1" / "patches" / "1_chunks"
    chunk_dir.mkdir(parents=True)
    sf.write(chunk_dir / "chunk_000.wav", np.zeros(1234, dtype=np.float32), fake_engine.sample_rate)
    seeded_conn.execute("INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (1, 1, 'Ch1', 'Second chapter.', 15)")
    seeded_conn.execute("UPDATE patch SET chapter_end = 1, max_chars = 400, next_chunk_index = 1, chunk_count = 2 WHERE id = 1")
    seeded_conn.commit()
    worker = _make_worker(seeded_conn, fake_engine, tmp_audio_dir, monkeypatch)
    patch = Patch(**dict(seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone()))
    audio_path = worker._synthesize(patch)
    timeline = json.loads(Path(audio_path).with_suffix(".timeline.json").read_text())
    expected_total = 1234 + 8000 + round(fake_engine.sample_rate * 300 / 1000)
    assert sf.info(audio_path).frames == expected_total
    assert timeline["total_frames"] == expected_total


@pytest.mark.parametrize("write_chunk_files", [True, False])
def test_worker_rejects_patch_with_no_speakable_text(tmp_audio_dir, seeded_conn, fake_engine, monkeypatch, write_chunk_files):
    seeded_conn.execute("UPDATE chapter SET text = '', char_count = 0 WHERE book_id = 1")
    seeded_conn.commit()
    import app.worker as worker_mod
    monkeypatch.setattr(worker_mod.settings, "tts_write_chunk_files", write_chunk_files)
    worker = _make_worker(seeded_conn, fake_engine, tmp_audio_dir, monkeypatch)
    patch = Patch(**dict(seeded_conn.execute("SELECT * FROM patch WHERE id = 1").fetchone()))
    with pytest.raises(ValueError, match="patch has no speakable text"):
        worker._synthesize(patch)
