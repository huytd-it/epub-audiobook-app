"""Encoder failure handling: ENOENT classification, output dir, missing segments."""
import subprocess
from pathlib import Path

import pytest

from app import video_gen


# ffmpeg returns its own negative AVERROR as the exit status; Windows reports it
# back as an unsigned DWORD. -2 (ENOENT) is the one that took down a real render.
ENOENT_EXIT = 0xFFFFFFFE
CRASH_EXIT = 0xC0000142  # DLL_INIT_FAILED — a genuine Windows crash


def test_enoent_is_not_retried(monkeypatch):
    """ffmpeg's ENOENT is deterministic: retrying it burns render attempts."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.CalledProcessError(ENOENT_EXIT, cmd, stderr="No such file or directory")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(video_gen.time, "sleep", lambda _s: None)

    with pytest.raises(subprocess.CalledProcessError):
        video_gen._run_encoder(["ffmpeg"])
    assert len(calls) == 1


def test_windows_crash_is_still_retried(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        raise subprocess.CalledProcessError(CRASH_EXIT, cmd, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(video_gen.time, "sleep", lambda _s: None)

    with pytest.raises(subprocess.CalledProcessError):
        video_gen._run_encoder(["ffmpeg"])
    assert len(calls) == video_gen._ENCODER_RETRIES + 1


def test_exhausted_retries_log_ffmpeg_stderr(monkeypatch, caplog):
    """The crash branch used to swallow stderr, leaving the failure undebuggable."""
    def fake_run(cmd, **kwargs):
        raise subprocess.CalledProcessError(CRASH_EXIT, cmd, stderr="boom: the real reason")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(video_gen.time, "sleep", lambda _s: None)

    with caplog.at_level("WARNING"), pytest.raises(subprocess.CalledProcessError):
        video_gen._run_encoder(["ffmpeg"])
    assert "boom: the real reason" in caplog.text


def test_concat_recreates_missing_output_dir(monkeypatch, tmp_path):
    """A cleanup pass can remove the output dir during a multi-hour render."""
    segments = []
    for name in ("a.mp4", "b.mp4"):
        p = tmp_path / name
        p.write_bytes(b"x")
        segments.append(str(p))

    out_dir = tmp_path / "gone"
    out = out_dir / "final.mp4"

    monkeypatch.setattr(video_gen, "_assert_uniform_frame_rate", lambda _p: None)
    monkeypatch.setattr(video_gen, "_assert_uniform_audio_format", lambda _p: None)
    monkeypatch.setattr(type(video_gen.settings), "get_ffmpeg_path", lambda _self: "ffmpeg")

    ran = []

    def fake_run_encoder(cmd, **kwargs):
        ran.append(cmd)
        assert out_dir.is_dir(), "output dir must exist before ffmpeg runs"
        Path(cmd[-1]).write_bytes(b"out")

    monkeypatch.setattr(video_gen, "_run_encoder", fake_run_encoder)

    video_gen.concat_segments(segments, str(out))
    assert ran and out.is_file()


def test_concat_rejects_missing_segment(tmp_path):
    """The concat demuxer skips an unopenable entry and still exits 0, so a
    silently truncated video would otherwise pass as a success."""
    present = tmp_path / "a.mp4"
    present.write_bytes(b"x")
    missing = tmp_path / "nope.mp4"

    with pytest.raises(FileNotFoundError, match="nope.mp4"):
        video_gen.concat_segments([str(present), str(missing)], str(tmp_path / "out.mp4"))
