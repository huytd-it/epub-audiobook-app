"""Every segment must be CFR at the requested fps, or concat -c copy silently
stretches the video track and YouTube rejects the upload.

The MP4 muxer stamps each packet with the *first* concat input's frame duration.
A 30fps segment written behind a 24fps one therefore plays 30/24 = 1.25x too
long, leaving the video track far longer than the audio. Real ffmpeg is used
here on purpose: this bug lives entirely in the ffmpeg argv, so a mocked
subprocess would keep passing while production stayed broken.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import video_gen
from app.config import settings


def _ffmpeg() -> str:
    return settings.get_ffmpeg_path()


def _have_ffmpeg() -> bool:
    return bool(shutil.which(_ffmpeg()) or Path(_ffmpeg()).is_file())


requires_ffmpeg = pytest.mark.skipif(
    not _have_ffmpeg(), reason="ffmpeg not available"
)


def _run(*args: str) -> None:
    subprocess.run([_ffmpeg(), "-y", *args], check=True, capture_output=True, text=True)


def _probe(path: str, entries: str) -> list[str]:
    out = subprocess.run(
        [settings.get_ffprobe_path(), "-v", "error", "-show_entries", entries,
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return [line for line in out.stdout.strip().splitlines() if line]


def _video_fps(path: str) -> float:
    num, den = _probe(path, "stream=r_frame_rate")[0].split("/")
    return int(num) / int(den)


def _durations(path: str) -> tuple[float, float]:
    """(video_duration, audio_duration) in seconds."""
    v = float(_probe(path, "stream=duration:stream_tags=none")[0])
    a = float(_probe(path, "stream=duration")[1])
    return v, a


@pytest.fixture
def assets(tmp_path):
    """A 24fps video background, a still image, and two narration tracks."""
    bg24 = tmp_path / "bg24.mp4"
    _run("-f", "lavfi", "-i", "color=c=red:s=160x120:r=24", "-t", "3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(bg24))
    img = tmp_path / "still.png"
    _run("-f", "lavfi", "-i", "color=c=blue:s=160x120", "-frames:v", "1", str(img))
    intro = tmp_path / "intro.wav"
    _run("-f", "lavfi", "-i", "sine=f=440", "-t", "2", str(intro))
    main = tmp_path / "main.wav"
    _run("-f", "lavfi", "-i", "sine=f=660", "-t", "10", str(main))
    return {"bg24": str(bg24), "img": str(img), "intro": str(intro), "main": str(main)}


# ---------------------------------------------------------------------------
# Root cause: generate_segment let the input dictate the output framerate.
# ---------------------------------------------------------------------------

@requires_ffmpeg
@pytest.mark.parametrize("source", ["bg24", "img"])
@pytest.mark.parametrize("fps", [24, 30])
def test_segment_output_is_cfr_at_requested_fps(assets, tmp_path, source, fps):
    """A 24fps clip and a still image must both yield exactly `fps` output."""
    out = tmp_path / f"seg_{source}_{fps}.mp4"
    video_gen.generate_segment(
        assets[source], assets["intro"], str(out),
        resolution=(160, 120), fps=fps,
    )
    assert _video_fps(str(out)) == pytest.approx(fps), (
        f"{source} produced {_video_fps(str(out))}fps, expected {fps}fps -- "
        "output framerate is being inherited from the input"
    )


@requires_ffmpeg
def test_concat_of_video_bg_and_still_keeps_av_in_sync(assets, tmp_path):
    """The production shape: intro from a 24fps clip + main from a still image.

    Before the fix the intro came out at 24fps and the main at 25fps (the
    `-loop 1` default), so concat -c copy stretched the 10s main to 10.4s.
    """
    fps = 30
    intro_seg = tmp_path / "intro.mp4"
    main_seg = tmp_path / "main.mp4"
    video_gen.generate_segment(
        assets["bg24"], assets["intro"], str(intro_seg),
        resolution=(160, 120), fps=fps,
    )
    video_gen.generate_segment(
        assets["img"], assets["main"], str(main_seg),
        resolution=(160, 120), fps=fps,
    )
    assert _video_fps(str(intro_seg)) == _video_fps(str(main_seg))

    out = tmp_path / "joined.mp4"
    video_gen.concat_segments([str(intro_seg), str(main_seg)], str(out))

    vdur, adur = _durations(str(out))
    assert abs(vdur - adur) < 0.15, (
        f"video {vdur:.3f}s vs audio {adur:.3f}s -- concat stretched the "
        "video track; YouTube cannot process this"
    )


# ---------------------------------------------------------------------------
# Defense in depth: never emit a silently broken concat.
# ---------------------------------------------------------------------------

@requires_ffmpeg
def test_concat_rejects_mismatched_framerates(tmp_path):
    """A framerate mismatch must fail loudly instead of producing bad output."""
    a24 = tmp_path / "a24.mp4"
    b30 = tmp_path / "b30.mp4"
    for path, rate in ((a24, 24), (b30, 30)):
        _run("-f", "lavfi", "-i", f"color=c=red:s=160x120:r={rate}",
             "-f", "lavfi", "-i", "sine=f=440", "-t", "2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))

    with pytest.raises(ValueError, match="framerate"):
        video_gen.concat_segments([str(a24), str(b30)], str(tmp_path / "o.mp4"))


@requires_ffmpeg
def test_gameplay_concat_repairs_legacy_mixed_framerates(tmp_path):
    """A frozen snapshot may span clips reserved before and after an fps change."""
    paths = []
    for index, rate in enumerate((60, 30)):
        path = tmp_path / f"gameplay_{rate}.mp4"
        _run("-f", "lavfi", "-i", f"color=c=red:s=160x120:r={rate}", "-t", "2",
             "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path))
        paths.append(str(path))

    out = tmp_path / "gameplay_joined.mp4"
    video_gen.concat_video_segments(
        paths, str(out), resolution=(160, 120), fps=30, quality=20
    )

    assert _video_fps(str(out)) == pytest.approx(30)
    assert _probe(str(out), "stream=width,height")[:2] == ["160", "120"]
    assert float(_probe(str(out), "format=duration")[0]) == pytest.approx(4, abs=0.15)


# ---------------------------------------------------------------------------
# argv-level guard, so the regression is visible without running ffmpeg.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,payload", [("bg.mp4", b"x"), ("bg.png", b"x")])
def test_segment_argv_pins_framerate(tmp_path, monkeypatch, name, payload):
    src = tmp_path / name
    src.write_bytes(payload)
    aud = tmp_path / "a.wav"
    aud.write_bytes(b"x")

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class R:
            stdout = ""
            returncode = 0

        return R()

    monkeypatch.setattr(video_gen.subprocess, "run", fake_run)
    video_gen.generate_segment(
        str(src), str(aud), str(tmp_path / "o.mp4"), fps=30,
    )
    cmd = captured["cmd"]
    assert "-r" in cmd, "output framerate is not pinned"
    assert cmd[cmd.index("-r") + 1] == "30"
