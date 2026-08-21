"""Gap music at the mux: the bed replaces the track and is never looped."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import music_bed, video_gen


@pytest.fixture
def files(tmp_path):
    for name in ("i.png", "a.wav", "m.mp3"):
        (tmp_path / name).write_bytes(b"x")
    return tmp_path


def _capture(monkeypatch) -> dict:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class R:
            stdout = ""
            stderr = ""
            returncode = 0

        return R()

    monkeypatch.setattr(video_gen.subprocess, "run", fake_run)
    return captured


def test_music_is_stream_looped_when_gap_mode_is_off(files, monkeypatch):
    captured = _capture(monkeypatch)
    video_gen.generate_segment(
        str(files / "i.png"), str(files / "a.wav"), str(files / "o.mp4"),
        music_path=str(files / "m.mp3"),
    )
    assert "-stream_loop" in captured["cmd"]


def test_the_bed_replaces_the_track_and_is_not_looped(files, monkeypatch):
    bed = files / "bed.wav"
    bed.write_bytes(b"x")
    monkeypatch.setattr(video_gen.music_bed, "build_gap_bed", lambda *a, **k: str(bed))
    captured = _capture(monkeypatch)

    video_gen.generate_segment(
        str(files / "i.png"), str(files / "a.wav"), str(files / "o.mp4"),
        music_path=str(files / "m.mp3"), music_gaps={"music_gap_only": True},
    )
    cmd = captured["cmd"]
    assert str(bed) in cmd
    assert str(files / "m.mp3") not in cmd
    # A bed is already exactly as long as the gaps it fills; looping it would
    # drag the music back over the narration.
    assert "-stream_loop" not in cmd


def test_a_narration_with_no_gap_renders_without_music(files, monkeypatch):
    monkeypatch.setattr(video_gen.music_bed, "build_gap_bed", lambda *a, **k: None)
    captured = _capture(monkeypatch)

    video_gen.generate_segment(
        str(files / "i.png"), str(files / "a.wav"), str(files / "o.mp4"),
        music_path=str(files / "m.mp3"), music_gaps={"music_gap_only": True},
    )
    cmd = captured["cmd"]
    assert str(files / "m.mp3") not in cmd
    assert "amix" not in " ".join(cmd)


def test_the_bed_is_cleaned_up_after_the_mux(files, monkeypatch):
    bed = files / "o.musicbed.wav"

    def fake_build(audio_path, music_path, out_path, config):
        Path(out_path).write_bytes(b"x")
        return str(out_path)

    monkeypatch.setattr(video_gen.music_bed, "build_gap_bed", fake_build)
    _capture(monkeypatch)
    video_gen.generate_segment(
        str(files / "i.png"), str(files / "a.wav"), str(files / "o.mp4"),
        music_path=str(files / "m.mp3"), music_gaps={"music_gap_only": True},
    )
    assert not any(p.name.endswith(".musicbed.wav") for p in files.iterdir())
    assert not bed.exists()


def test_gap_settings_reach_the_bed_builder(files, monkeypatch):
    seen: dict = {}

    def fake_build(audio_path, music_path, out_path, config):
        seen["config"] = config
        return None

    monkeypatch.setattr(video_gen.music_bed, "build_gap_bed", fake_build)
    _capture(monkeypatch)
    config = {"music_gap_only": True, "music_gap_min_ms": 2500, "music_gap_fade_ms": 250}
    video_gen.generate_segment(
        str(files / "i.png"), str(files / "a.wav"), str(files / "o.mp4"),
        music_path=str(files / "m.mp3"), music_gaps=config,
    )
    assert music_bed.parse_options(seen["config"]).min_gap_ms == 2500


def test_gap_mode_without_music_does_nothing(files, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("no music, nothing to place")

    monkeypatch.setattr(video_gen.music_bed, "build_gap_bed", explode)
    captured = _capture(monkeypatch)
    video_gen.generate_segment(
        str(files / "i.png"), str(files / "a.wav"), str(files / "o.mp4"),
        music_gaps={"music_gap_only": True},
    )
    assert "amix" not in " ".join(captured["cmd"])
