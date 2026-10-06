"""Tests for chapter-end background music (app/music_bed.py).

The pure parts - reading options, turning chapters into placeable pieces,
building the filtergraph - always run. The end-to-end render is skipped when
ffmpeg is not on the machine.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from app import music_bed
from app.music_bed import BedOptions, Piece


def _has_ffmpeg() -> bool:
    from app.config import settings

    return shutil.which(settings.get_ffmpeg_path()) is not None or Path(settings.get_ffmpeg_path()).exists()


needs_ffmpeg = pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg không có trên máy này")


def test_parse_options_reads_the_render_config_shape():
    options = music_bed.parse_options({
        "music_chapter_end_seconds": 20, "music_random_start": True, "music_fade_enabled": False,
    })
    assert options == BedOptions(chapter_end_seconds=20, random_start=True, fade=False)


def test_parse_options_defaults_to_15s_with_fade():
    assert music_bed.parse_options({}) == BedOptions(chapter_end_seconds=15, random_start=False, fade=True)


def test_parse_options_falls_back_on_junk_and_clamps():
    assert music_bed.parse_options({"music_chapter_end_seconds": "nope"}).chapter_end_seconds == 15
    assert music_bed.parse_options({"music_chapter_end_seconds": True}).chapter_end_seconds == 15
    assert music_bed.parse_options({"music_chapter_end_seconds": 0}).chapter_end_seconds == 1
    assert music_bed.parse_options({"music_chapter_end_seconds": 99999}).chapter_end_seconds == 300


def test_legacy_gap_snapshot_still_gets_chapter_end_music():
    # A render queued before the gap mode was removed carries only the old keys.
    config = {"music_gap_only": True, "music_gap_min_ms": 1500}
    assert music_bed.is_enabled(config) is True
    assert music_bed.parse_options(config) == BedOptions()


def test_is_enabled_only_without_a_config_is_off():
    # No config is the standalone Video Creator, which keeps looping its music.
    assert music_bed.is_enabled(None) is False
    assert music_bed.is_enabled({}) is True


def test_plan_pieces_covers_the_last_seconds_of_each_chapter():
    pieces = music_bed.plan_pieces([(0.0, 100.0), (100.0, 250.0)], BedOptions(chapter_end_seconds=15))
    assert pieces == [Piece(85.0, 15.0), Piece(235.0, 15.0)]


def test_a_chapter_shorter_than_the_window_gets_music_throughout():
    assert music_bed.plan_pieces([(10.0, 18.0)], BedOptions(chapter_end_seconds=15)) == [Piece(10.0, 8.0)]


def test_a_blip_of_a_chapter_gets_no_music():
    assert music_bed.plan_pieces([(10.0, 10.1)], BedOptions()) == []


def test_pieces_play_from_the_start_of_the_track_by_default():
    pieces = music_bed.plan_pieces([(0.0, 100.0)], BedOptions(), music_duration=120.0)
    assert pieces[0].offset == 0.0


def test_random_start_picks_a_point_that_still_fits_the_piece():
    chapters = [(index * 100.0, index * 100.0 + 100.0) for index in range(20)]
    pieces = music_bed.plan_pieces(chapters, BedOptions(random_start=True), music_duration=60.0, seed="a.wav")
    assert all(0.0 <= piece.offset <= 45.0 for piece in pieces)
    assert len({piece.offset for piece in pieces}) > 1


def test_random_start_is_stable_for_the_same_narration():
    chapters = [(0.0, 100.0), (100.0, 200.0)]
    options = BedOptions(random_start=True)
    first = music_bed.plan_pieces(chapters, options, music_duration=60.0, seed="a.wav")
    again = music_bed.plan_pieces(chapters, options, music_duration=60.0, seed="a.wav")
    other = music_bed.plan_pieces(chapters, options, music_duration=60.0, seed="b.wav")
    assert first == again
    assert first != other


def test_random_start_with_a_track_shorter_than_the_piece_starts_at_zero():
    pieces = music_bed.plan_pieces([(0.0, 100.0)], BedOptions(random_start=True), music_duration=10.0, seed="x")
    assert pieces[0].offset == 0.0


def test_plan_pieces_caps_the_piece_count():
    chapters = [(index * 30.0, index * 30.0 + 30.0) for index in range(music_bed.MAX_PIECES + 20)]
    assert len(music_bed.plan_pieces(chapters, BedOptions())) == music_bed.MAX_PIECES


def test_filter_graph_places_offsets_fades_and_delays():
    graph = music_bed.build_filter_graph([Piece(2.0, 3.0), Piece(10.0, 15.0, 4.5)], BedOptions())
    chains = graph.split(";")
    assert chains[0].startswith("[0:a]atrim=0.000:3.000")
    assert "afade=t=in:st=0:d=1.500" in chains[0]
    assert "afade=t=out:st=1.500:d=1.500" in chains[0]
    assert "adelay=2000:all=1[b0]" in chains[0]
    assert chains[1].startswith("[1:a]atrim=4.500:19.500")
    assert "afade=t=in:st=0:d=2.000" in chains[1]
    assert "afade=t=out:st=13.000:d=2.000" in chains[1]
    assert "adelay=10000:all=1[b1]" in chains[1]
    assert chains[-1] == "[b0][b1]amix=inputs=2:normalize=0:dropout_transition=0[bed]"


def test_a_single_piece_needs_no_amix():
    graph = music_bed.build_filter_graph([Piece(1.0, 2.0)], BedOptions())
    assert "amix" not in graph
    assert graph.endswith("[bed]")


def test_fade_off_leaves_the_piece_untouched():
    graph = music_bed.build_filter_graph([Piece(1.0, 15.0)], BedOptions(fade=False))
    assert "afade" not in graph


def test_bed_command_opens_the_music_once_per_piece():
    cmd = music_bed.build_bed_command("music.mp3", "bed.wav", [Piece(1.0, 2.0), Piece(5.0, 2.0)], "graph.txt")
    assert cmd.count("music.mp3") == 2
    assert cmd.count("-stream_loop") == 2
    assert cmd[-1] == "bed.wav"
    assert "-filter_complex_script" in cmd and "graph.txt" in cmd


def test_no_config_builds_nothing(tmp_path):
    assert music_bed.build_chapter_bed("a.wav", "m.mp3", tmp_path / "bed.wav", None) is None


def _render(args: list[str], dest: Path) -> Path:
    from app.config import settings

    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.get_ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error", *args, str(dest)],
        check=True,
    )
    return dest


def _write_timeline(audio: Path, starts: list[float]) -> None:
    import soundfile as sf

    info = sf.info(str(audio))
    chapters = []
    for index, start in enumerate(starts):
        frame = round(start * info.samplerate)
        chapters.append({"chapter_index": index, "title": f"Chương {index + 1}",
                         "start_frame": frame, "start_seconds": frame / info.samplerate})
    audio.with_suffix(".timeline.json").write_text(json.dumps({
        "version": 1, "sample_rate": info.samplerate, "total_frames": info.frames, "chapters": chapters,
    }), encoding="utf-8")


def test_chapter_spans_follow_the_timeline_sidecar(tmp_path):
    import numpy as np
    import soundfile as sf

    audio = tmp_path / "patch.wav"
    sf.write(str(audio), np.zeros(30 * 100, dtype="float32"), 100)
    _write_timeline(audio, [0.0, 12.0, 20.0])
    assert music_bed.chapter_spans(audio, 30.0) == [(0.0, 12.0), (12.0, 20.0), (20.0, 30.0)]


def test_chapter_spans_without_a_timeline_is_one_chapter(tmp_path):
    import numpy as np
    import soundfile as sf

    audio = tmp_path / "patch.wav"
    sf.write(str(audio), np.zeros(1000, dtype="float32"), 100)
    assert music_bed.chapter_spans(audio, 10.0) == [(0.0, 10.0)]


@needs_ffmpeg
def test_bed_sits_under_the_end_of_each_chapter(tmp_path):
    narration = _render(["-f", "lavfi", "-i", "sine=frequency=440:r=44100:duration=40"], tmp_path / "patch.wav")
    _write_timeline(narration, [0.0, 20.0])
    music = _render(["-f", "lavfi", "-i", "sine=frequency=880:r=44100:duration=60"], tmp_path / "music.wav")
    bed = music_bed.build_chapter_bed(
        narration, music, tmp_path / "bed.wav", {"music_chapter_end_seconds": 5, "music_fade_enabled": False},
    )
    assert bed is not None
    # The last piece ends with the narration, so the bed is as long as it.
    assert 39.5 < music_bed.probe_duration(bed) < 40.5
    import soundfile as sf

    data, rate = sf.read(bed)
    level = lambda a, b: float(abs(data[int(a * rate):int(b * rate)]).max())
    assert level(0, 14.5) == 0.0      # chapter 1 body: silent
    assert level(15.5, 19.5) > 0.1   # chapter 1 closing 5s
    assert level(20.5, 34.5) == 0.0  # chapter 2 body
    assert level(35.5, 39.5) > 0.1   # chapter 2 closing 5s
