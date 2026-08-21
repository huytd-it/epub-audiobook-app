"""The per-chunk pause plan: a longer beat before each chapter inside a patch.

Audio, chapter timeline and captions all consume the same plan, so the three
must agree on where every chunk starts - that is what most of these check.
"""
from __future__ import annotations

import json

import numpy as np
import pytest
import soundfile as sf

from app import audio_merge, db, repository
from app.production_defaults import get_effective_audio_config, save_book_audio_section
from app.subtitle_gen import build_cues
from app.tts_engine import normalize_tt_payload


def _plan(*chapter_flags: bool) -> list[dict]:
    return [
        {"text": f"Câu {index}.", "chapter_index": index, "chapter_title": f"Chương {index}",
         "is_chapter_start": flag}
        for index, flag in enumerate(chapter_flags)
    ]


# ---------------------------------------------------------------------------
# The plan itself
# ---------------------------------------------------------------------------


def test_chapter_starts_get_the_longer_pause():
    plan = _plan(True, False, True, False)
    assert audio_merge.build_pause_plan(plan, 300, 1500) == [0, 300, 1500, 300]


def test_nothing_is_ever_prepended_to_the_first_chunk():
    # The first chunk opens a chapter too, but a patch must not start with silence.
    assert audio_merge.build_pause_plan(_plan(True), 300, 1500) == [0]


def test_a_plan_without_chapter_markers_stays_uniform():
    # Text Studio edits destroy the boundaries; spacing then matches the old behaviour.
    plan = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
    assert audio_merge.build_pause_plan(plan, 300, 1500) == [0, 300, 300]


def test_resolve_pauses_accepts_a_plain_int():
    assert audio_merge.resolve_pauses(300, 3) == [0, 300, 300]


def test_resolve_pauses_rejects_a_plan_of_the_wrong_length():
    with pytest.raises(ValueError):
        audio_merge.resolve_pauses([0, 300], 3)


# ---------------------------------------------------------------------------
# Audio layout
# ---------------------------------------------------------------------------


def test_merged_audio_uses_the_chapter_pause_between_chapters(tmp_path):
    rate = 1000
    chunks = [np.ones(rate, dtype=np.float32) * 0.5 for _ in range(3)]
    out = tmp_path / "patch.wav"
    audio_merge.concat_chunks_to_wav(chunks, rate, str(out), pause_ms=[0, 300, 1500])
    # 3 seconds of speech + 0.3s + 1.5s of silence.
    assert sf.info(str(out)).frames == 3 * rate + 300 + 1500


def test_streamed_merge_matches_the_in_memory_merge(tmp_path):
    rate = 1000
    paths = []
    for index in range(3):
        path = tmp_path / f"chunk_{index}.wav"
        sf.write(str(path), np.ones(rate, dtype=np.float32) * 0.5, rate)
        paths.append(str(path))
    out = tmp_path / "streamed.wav"
    audio_merge.concat_wavs(paths, str(out), pause_ms=[0, 300, 1500])
    assert sf.info(str(out)).frames == 3 * rate + 300 + 1500


def test_the_chapter_marker_lands_after_the_pause(tmp_path):
    rate = 1000
    plan = _plan(True, False, True)
    chapters, total = audio_merge.build_chapter_marks(plan, [rate, rate, rate], rate, [0, 300, 1500])
    assert [c["start_frame"] for c in chapters] == [0, 2 * rate + 300 + 1500]
    assert total == 3 * rate + 1800


def test_timeline_total_matches_the_file_on_disk(tmp_path):
    rate = 1000
    plan = _plan(True, False, True)
    chunks = [np.ones(rate, dtype=np.float32) * 0.5 for _ in plan]
    pauses = audio_merge.build_pause_plan(plan, 300, 1500)
    out = tmp_path / "patch.wav"
    audio_merge.concat_chunks_to_wav(chunks, rate, str(out), pause_ms=pauses)
    _, total = audio_merge.build_chapter_marks(plan, [len(c) for c in chunks], rate, pauses)
    assert total == sf.info(str(out)).frames


# ---------------------------------------------------------------------------
# Captions
# ---------------------------------------------------------------------------


def test_cues_follow_the_same_pause_plan():
    rate = 1000
    plan = _plan(True, False, True)
    cues = build_cues(plan, [rate, rate, rate], rate, [0, 300, 1500])
    starts = [round(cue.start_seconds, 3) for cue in cues]
    # chunk 0 at 0s, chunk 1 after 0.3s of silence, chunk 2 after 1.5s more.
    assert starts == [0.0, 1.3, 3.8]


def test_cue_starts_match_the_chapter_timeline():
    rate = 16000
    plan = _plan(True, False, True, False)
    frames = [rate, 2 * rate, rate, rate]
    pauses = audio_merge.build_pause_plan(plan, 300, 1500)
    chapters, _ = audio_merge.build_chapter_marks(plan, frames, rate, pauses)
    cues = build_cues(plan, frames, rate, pauses)
    chapter_two_start = chapters[1]["start_seconds"]
    assert any(abs(cue.start_seconds - chapter_two_start) < 1e-6 for cue in cues)


# ---------------------------------------------------------------------------
# Config plumbing
# ---------------------------------------------------------------------------


def test_payload_defaults_the_pauses_for_jobs_queued_before_the_feature():
    payload = normalize_tt_payload({"patch_id": 1})
    assert payload["chunk_pause_ms"] == audio_merge.DEFAULT_CHUNK_PAUSE_MS
    assert payload["chapter_pause_ms"] == audio_merge.DEFAULT_CHAPTER_PAUSE_MS


def test_payload_keeps_explicit_pauses_and_clamps_junk():
    assert normalize_tt_payload({"chapter_pause_ms": 2500})["chapter_pause_ms"] == 2500
    assert normalize_tt_payload({"chapter_pause_ms": "abc"})["chapter_pause_ms"] == 1500
    assert normalize_tt_payload({"chapter_pause_ms": -5})["chapter_pause_ms"] == 0


def _book_with_custom_audio(conn):
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                             automation_config, created_at, updated_at)
           VALUES (1, 'Sách', 'a.epub', 'a.epub', 10, 'ready', ?, ?, ?)""",
        (json.dumps({"inherit": {"audio": False}}), "2026-01-01", "2026-01-01"),
    )
    conn.commit()
    return repository.get_book(conn, 1)


def test_a_custom_book_stores_its_pauses_in_automation_config():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    _book_with_custom_audio(conn)
    save_book_audio_section(conn, 1, chunk_pause_ms=500, chapter_pause_ms=2500)
    conn.commit()
    config = get_effective_audio_config(conn, repository.get_book(conn, 1))
    assert config["chunk_pause_ms"] == 500
    assert config["chapter_pause_ms"] == 2500


def test_a_custom_book_saved_before_the_feature_falls_back_to_the_defaults():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    book = _book_with_custom_audio(conn)
    config = get_effective_audio_config(conn, book)
    assert config["chunk_pause_ms"] == audio_merge.DEFAULT_CHUNK_PAUSE_MS
    assert config["chapter_pause_ms"] == audio_merge.DEFAULT_CHAPTER_PAUSE_MS
