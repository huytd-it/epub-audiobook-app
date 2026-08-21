"""Tests for gap-only background music (app/music_bed.py).

The pure parts - reading silencedetect's log, turning silences into placeable
pieces, building the filtergraph - always run. The end-to-end render is skipped
when ffmpeg is not on the machine.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import music_bed
from app.music_bed import GapOptions


def _has_ffmpeg() -> bool:
    from app.config import settings

    return shutil.which(settings.get_ffmpeg_path()) is not None or Path(settings.get_ffmpeg_path()).exists()


needs_ffmpeg = pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg không có trên máy này")

_LOG = """
[silencedetect @ 0000] silence_start: 2.5
[silencedetect @ 0000] silence_end: 5 | silence_duration: 2.5
[silencedetect @ 0000] silence_start: 12.25
[silencedetect @ 0000] silence_end: 14.75 | silence_duration: 2.5
"""


def test_parse_silence_log_reads_pairs():
    assert music_bed.parse_silence_log(_LOG) == [(2.5, 5.0), (12.25, 14.75)]


def test_unterminated_silence_is_closed_at_the_file_end():
    log = "silence_start: 30.0\n"
    assert music_bed.parse_silence_log(log, total_duration=42.0) == [(30.0, 42.0)]


def test_unterminated_silence_without_a_duration_is_dropped():
    # An unbounded gap would place music past the end of the narration.
    assert music_bed.parse_silence_log("silence_start: 30.0\n") == []


def test_parse_options_reads_the_render_config_shape():
    options = music_bed.parse_options({
        "music_gap_only": True, "music_gap_min_ms": 2000, "music_gap_fade_ms": 250,
    })
    assert (options.enabled, options.min_gap_ms, options.fade_ms) == (True, 2000, 250)


def test_parse_options_falls_back_on_junk():
    options = music_bed.parse_options({"enabled": True, "min_gap_ms": "nope"})
    assert options.min_gap_ms == music_bed.DEFAULT_MIN_GAP_MS
    assert music_bed.parse_options(None).enabled is False


def test_parse_options_clamps_out_of_range_values():
    assert music_bed.parse_options({"min_gap_ms": 5}).min_gap_ms == 200
    assert music_bed.parse_options({"fade_ms": 999999}).fade_ms == 5000


def test_is_enabled_is_off_without_a_config():
    assert music_bed.is_enabled(None) is False
    assert music_bed.is_enabled({"music_gap_only": False}) is False
    assert music_bed.is_enabled({"music_gap_only": True}) is True


def test_plan_pieces_insets_the_edges_of_each_gap():
    options = GapOptions(enabled=True, min_gap_ms=1500, edge_pad_ms=100)
    pieces = music_bed.plan_pieces([(10.0, 12.0)], options)
    assert pieces == [(10.1, 1.8)]


def test_plan_pieces_drops_a_gap_shorter_than_the_threshold():
    options = GapOptions(enabled=True, min_gap_ms=1500, edge_pad_ms=100)
    # 0.6s of silence: detection would not report it, and a stale/hand-fed gap
    # this short must not turn into a click of music either.
    assert music_bed.plan_pieces([(4.0, 4.6)], options) == []


def test_padding_never_drops_a_gap_that_did_pass_the_threshold():
    options = GapOptions(enabled=True, min_gap_ms=1500, edge_pad_ms=400)
    assert music_bed.plan_pieces([(4.0, 5.5)], options) == [(4.4, 0.7)]


def test_a_piece_never_outlasts_the_music_itself():
    options = GapOptions(enabled=True, min_gap_ms=1500, edge_pad_ms=0)
    pieces = music_bed.plan_pieces([(0.0, 30.0)], options, music_duration=8.0)
    assert pieces == [(0.0, 8.0)]


def test_plan_pieces_caps_the_piece_count():
    options = GapOptions(enabled=True, min_gap_ms=1500, edge_pad_ms=0)
    gaps = [(index * 10.0, index * 10.0 + 3.0) for index in range(music_bed.MAX_PIECES + 20)]
    assert len(music_bed.plan_pieces(gaps, options)) == music_bed.MAX_PIECES


def test_filter_graph_places_fades_and_delays():
    options = GapOptions(enabled=True, fade_ms=400)
    graph = music_bed.build_filter_graph([(2.0, 3.0), (10.0, 4.0)], options)
    chains = graph.split(";")
    assert chains[0].startswith("[0:a]atrim=0:3.000")
    assert "afade=t=in:st=0:d=0.400" in chains[0]
    assert "afade=t=out:st=2.600:d=0.400" in chains[0]
    assert "adelay=2000:all=1[b0]" in chains[0]
    assert "adelay=10000:all=1[b1]" in chains[1]
    assert chains[-1] == "[b0][b1]amix=inputs=2:normalize=0:dropout_transition=0[bed]"


def test_a_single_piece_needs_no_amix():
    graph = music_bed.build_filter_graph([(1.0, 2.0)], GapOptions(enabled=True))
    assert "amix" not in graph
    assert graph.endswith("[bed]")


def test_fade_never_exceeds_half_the_piece():
    graph = music_bed.build_filter_graph([(0.0, 0.5)], GapOptions(enabled=True, fade_ms=4000))
    assert "afade=t=in:st=0:d=0.250" in graph
    assert "afade=t=out:st=0.250:d=0.250" in graph


def test_zero_fade_leaves_the_piece_untouched():
    graph = music_bed.build_filter_graph([(1.0, 2.0)], GapOptions(enabled=True, fade_ms=0))
    assert "afade" not in graph


def test_bed_command_opens_the_music_once_per_piece():
    cmd = music_bed.build_bed_command("music.mp3", "bed.wav", [(1.0, 2.0), (5.0, 2.0)], "graph.txt")
    assert cmd.count("music.mp3") == 2
    assert cmd.count("-stream_loop") == 2
    assert cmd[-1] == "bed.wav"
    assert "-filter_complex_script" in cmd and "graph.txt" in cmd


def test_detect_command_uses_the_configured_threshold_and_length():
    cmd = music_bed.build_detect_command("a.wav", GapOptions(min_gap_ms=2500, threshold_db=-35))
    # 2500ms minus the detection tolerance.
    assert "silencedetect=noise=-35dB:d=2.45" in cmd


def test_a_gap_exactly_as_long_as_the_threshold_still_qualifies():
    """The default chapter pause is exactly the default threshold, so the two
    must not race on rounding: detection asks for slightly less, and the piece
    filter is looser than that again."""
    options = GapOptions(enabled=True, min_gap_ms=1500, edge_pad_ms=120)
    assert music_bed.plan_pieces([(10.0, 11.451)], options) == [(10.12, 1.211)]


def test_disabled_config_builds_nothing(tmp_path):
    assert music_bed.build_gap_bed("a.wav", "m.mp3", tmp_path / "bed.wav", {"music_gap_only": False}) is None


def _render(args: list[str], dest: Path) -> Path:
    from app.config import settings

    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.get_ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", *args, str(dest)],
        check=True,
    )
    return dest


def _narration_with_gap(dest: Path) -> Path:
    """2s tone, 3s silence, 2s tone - one gap worth filling."""
    return _render([
        "-f", "lavfi", "-i", "sine=frequency=440:r=44100:duration=2",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:r=44100:duration=2",
        "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
    ], dest)


@needs_ffmpeg
def test_detect_finds_the_silence_between_two_tones(tmp_path):
    narration = _narration_with_gap(tmp_path / "narration.wav")
    gaps = music_bed.detect_silence_gaps(narration, GapOptions(enabled=True, min_gap_ms=1500))
    assert len(gaps) == 1
    start, end = gaps[0]
    assert 1.8 < start < 2.2
    assert 4.8 < end < 5.2


@needs_ffmpeg
def test_bed_covers_the_gap_and_nothing_else(tmp_path):
    narration = _narration_with_gap(tmp_path / "narration.wav")
    music = _render(["-f", "lavfi", "-i", "sine=frequency=880:r=44100:duration=10"], tmp_path / "music.wav")
    bed = music_bed.build_gap_bed(
        narration, music, tmp_path / "bed.wav",
        {"music_gap_only": True, "music_gap_min_ms": 1500, "music_gap_fade_ms": 200},
    )
    assert bed is not None
    # The bed ends with the gap it fills (~5s in), well short of the 7s narration.
    duration = music_bed.probe_duration(bed)
    assert 4.5 < duration < 5.5
    # ...and it is silent where the narration is speaking.
    quiet = music_bed.detect_silence_gaps(bed, GapOptions(enabled=True, min_gap_ms=1000, threshold_db=-50))
    assert any(start < 0.5 for start, _ in quiet), quiet


@needs_ffmpeg
def test_the_default_chapter_pause_is_detected_at_the_default_threshold(tmp_path):
    """The end-to-end case the defaults were chosen for: a patch merged with a
    1500ms chapter pause and a 300ms chunk pause, read back with a 1500ms
    threshold. The chapter break must be filled and the chunk breaks must not."""
    import numpy as np
    import soundfile as sf

    from app import audio_merge

    rate = 44100
    plan = [
        {"text": "a", "is_chapter_start": True},
        {"text": "b", "is_chapter_start": False},
        {"text": "c", "is_chapter_start": True},
    ]
    tone = (np.sin(np.arange(rate) * 2 * np.pi * 440 / rate) * 0.5).astype("float32")
    narration = tmp_path / "patch.wav"
    audio_merge.concat_chunks_to_wav(
        [tone, tone, tone], rate, str(narration),
        pause_ms=audio_merge.build_pause_plan(plan, 300, 1500),
    )
    assert sf.info(str(narration)).frames == 3 * rate + round(rate * 1.8)

    options = GapOptions(enabled=True, min_gap_ms=1500)
    pieces = music_bed.plan_pieces(music_bed.detect_silence_gaps(narration, options), options)
    assert len(pieces) == 1, pieces
    start, _ = pieces[0]
    # The chapter pause opens after two chunks of speech plus one 300ms breath.
    assert 2.2 < start < 2.5


@needs_ffmpeg
def test_narration_without_a_long_enough_gap_gets_no_bed(tmp_path):
    narration = _render(
        ["-f", "lavfi", "-i", "sine=frequency=440:r=44100:duration=4"], tmp_path / "solid.wav"
    )
    music = _render(["-f", "lavfi", "-i", "sine=frequency=880:r=44100:duration=5"], tmp_path / "music.wav")
    assert music_bed.build_gap_bed(
        narration, music, tmp_path / "bed.wav", {"music_gap_only": True, "music_gap_min_ms": 1500}
    ) is None
