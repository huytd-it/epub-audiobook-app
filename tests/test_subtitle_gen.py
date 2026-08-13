"""Unit tests for app.subtitle_gen: chunk-exact cue timing and .ass rendering."""
from __future__ import annotations

from app import subtitle_gen
from app.subtitle_gen import Cue, build_cues, render_ass, write_ass


def _plan(*texts: str) -> list[dict]:
    return [{"text": t, "chapter_index": None, "chapter_title": None, "is_chapter_start": i == 0}
            for i, t in enumerate(texts)]


# ---------------------------------------------------------------------------
# build_cues
# ---------------------------------------------------------------------------


def test_build_cues_rejects_mismatched_lengths():
    try:
        build_cues(_plan("a", "b"), [16000], 16000, 0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_single_chunk_short_enough_becomes_one_cue():
    plan = _plan("Một câu ngắn.")
    cues = build_cues(plan, [32000], 16000, 0)
    assert len(cues) == 1
    assert cues[0].text == "Một câu ngắn."
    assert cues[0].start_seconds == 0.0
    assert cues[0].end_seconds == 2.0


def test_chunk_boundaries_are_exact_regardless_of_sub_splitting():
    # Two chunks, each split into several cues - the FIRST cue of chunk 2 must
    # start exactly where chunk 1 ends (plus the pause), because that boundary
    # is real TTS output timing, not an approximation.
    long_text = "Đây là câu một. " * 10 + "Đây là câu hai kết thúc chunk."
    plan = _plan(long_text, "Chunk thứ hai bắt đầu ở đây. Và câu tiếp theo.")
    frame_counts = [16000 * 10, 16000 * 4]
    cues = build_cues(plan, frame_counts, 16000, pause_ms=300)
    chunk1_cues = [c for c in cues if c.end_seconds <= 10.0 + 1e-6]
    chunk2_cues = [c for c in cues if c.start_seconds >= 10.0]
    assert chunk1_cues and chunk2_cues
    assert chunk1_cues[-1].end_seconds == 10.0
    assert chunk2_cues[0].start_seconds == 10.3  # + 300ms pause


def test_cues_within_a_chunk_are_contiguous_and_proportional_to_length():
    text = "Ngắn. " + "Một câu dài hơn nhiều so với câu đầu tiên ở trên kia. " * 3
    plan = _plan(text)
    cues = build_cues(plan, [16000 * 20], 16000, 0, max_chars_per_cue=40)
    assert len(cues) > 1
    for a, b in zip(cues, cues[1:]):
        assert a.end_seconds == b.start_seconds
    assert cues[0].start_seconds == 0.0
    assert cues[-1].end_seconds == 20.0
    # A longer cue gets a proportionally longer share of the chunk's duration.
    durations = [c.end_seconds - c.start_seconds for c in cues]
    lengths = [len(c.text) for c in cues]
    longest = max(range(len(cues)), key=lambda i: lengths[i])
    assert durations[longest] == max(durations)


def test_empty_or_whitespace_chunks_produce_no_cues():
    plan = _plan("   ", "")
    cues = build_cues(plan, [1000, 1000], 16000, 0)
    assert cues == []


def test_skips_only_the_empty_chunk_but_keeps_timing_of_the_rest():
    plan = _plan("Nội dung thật.", "   ", "Nội dung khác.")
    cues = build_cues(plan, [16000, 16000, 16000], 16000, pause_ms=0)
    assert len(cues) == 2
    assert cues[0].end_seconds == 1.0
    # second real chunk starts after BOTH preceding chunks' durations (the
    # empty one still occupied a full second of real audio and must not be
    # skipped over).
    assert cues[1].start_seconds == 2.0


# ---------------------------------------------------------------------------
# render_ass / write_ass
# ---------------------------------------------------------------------------


def test_render_ass_includes_header_and_dialogue_lines():
    cues = [Cue("Xin chào thế giới", 0.0, 2.5), Cue("Câu thứ hai", 2.5, 5.0)]
    doc = render_ass(cues)
    assert "[Script Info]" in doc
    assert "[V4+ Styles]" in doc
    assert "[Events]" in doc
    assert "Dialogue: 0,0:00:00.00,0:00:02.50,Caption,,0,0,0,,Xin chào thế giới" in doc
    assert "Dialogue: 0,0:00:02.50,0:00:05.00,Caption,,0,0,0,,Câu thứ hai" in doc


def test_render_ass_drops_zero_or_negative_duration_cues():
    cues = [Cue("hiện", 1.0, 2.0), Cue("ẩn", 3.0, 3.0)]
    doc = render_ass(cues)
    assert "hiện" in doc
    assert "ẩn" not in doc


def test_render_ass_escapes_braces_and_backslashes():
    doc = render_ass([Cue("giá {x} và \\y", 0.0, 1.0)])
    assert r"giá \{x\} và \\y" in doc


def test_render_ass_applies_requested_color_and_position():
    doc = render_ass([Cue("t", 0.0, 1.0)], color="#FF8800", position="top")
    # '#FF8800' -> BGR '&H000088FF'
    assert "&H000088FF" in doc
    assert ",8," in doc  # top => numpad alignment 8


def test_write_ass_is_atomic_and_readable(tmp_path):
    path = tmp_path / "patch.ass"
    write_ass(path, [Cue("nội dung", 0.0, 1.0)])
    assert path.is_file()
    text = path.read_text(encoding="utf-8")
    assert "nội dung" in text
    assert not list(tmp_path.glob(".*.tmp"))


def test_try_write_ass_never_raises_on_a_bad_path():
    subtitle_gen.try_write_ass("/definitely/not/a/writable/path.ass", [Cue("t", 0.0, 1.0)])
