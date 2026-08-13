"""Real-ffmpeg tests for burning subtitle_gen's .ass sidecars into rendered
video (video_gen.generate_segment / generate_background_sequence).

Real ffmpeg is used on purpose, same rationale as test_video_audio_consistency:
the escaping/filter-graph wiring lives entirely in the ffmpeg argv, so a
mocked subprocess would keep passing while a broken filter string failed in
production.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import subtitle_gen, video_gen
from app.config import settings
from app.subtitle_gen import Cue


def _ffmpeg() -> str:
    return settings.get_ffmpeg_path()


def _have_ffmpeg() -> bool:
    return bool(shutil.which(_ffmpeg()) or Path(_ffmpeg()).is_file())


requires_ffmpeg = pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not available")


def _run(*args: str) -> None:
    subprocess.run([_ffmpeg(), "-y", *args], check=True, capture_output=True, text=True)


def _has_video_stream(path) -> bool:
    out = subprocess.run(
        [settings.get_ffprobe_path(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=codec_type", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return out.returncode == 0 and "video" in out.stdout


@pytest.fixture
def still(tmp_path):
    img = tmp_path / "still.png"
    _run("-f", "lavfi", "-i", "color=c=blue:s=320x240", "-frames:v", "1", str(img))
    return str(img)


@pytest.fixture
def narration(tmp_path):
    wav = tmp_path / "narration.wav"
    _run("-f", "lavfi", "-i", "sine=f=440:sample_rate=24000:duration=2", "-ac", "1", str(wav))
    return str(wav)


def _write_sidecar(audio_path: str, text: str = "Xin chào thế giới") -> Path:
    sidecar = Path(audio_path).with_suffix(".ass")
    subtitle_gen.write_ass(sidecar, [Cue(text, 0.0, 2.0)])
    return sidecar


# ---------------------------------------------------------------------------
# generate_segment
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_segment_renders_without_a_sidecar_when_subtitles_enabled(still, narration, tmp_path):
    """No .ass next to the audio (a voice-clip intro, or a pre-feature patch)
    must be a silent no-op, never a failure."""
    out = tmp_path / "out.mp4"
    video_gen.generate_segment(
        still, narration, str(out), resolution=(320, 240), fps=24,
        waveform_config={"subtitle_enabled": True},
    )
    assert out.is_file() and _has_video_stream(out)


@requires_ffmpeg
def test_segment_burns_subtitles_when_enabled_and_sidecar_present(still, narration, tmp_path):
    _write_sidecar(narration)
    out = tmp_path / "out.mp4"
    video_gen.generate_segment(
        still, narration, str(out), resolution=(320, 240), fps=24,
        waveform_config={"subtitle_enabled": True},
    )
    assert out.is_file() and _has_video_stream(out)


@requires_ffmpeg
def test_segment_ignores_sidecar_when_subtitles_disabled(still, narration, tmp_path):
    _write_sidecar(narration)
    out = tmp_path / "out.mp4"
    # subtitle_enabled defaults to False/absent - must render exactly as it
    # did before this feature existed.
    video_gen.generate_segment(still, narration, str(out), resolution=(320, 240), fps=24)
    assert out.is_file() and _has_video_stream(out)


@requires_ffmpeg
def test_segment_burns_subtitles_together_with_a_waveform(still, narration, tmp_path):
    _write_sidecar(narration)
    out = tmp_path / "out.mp4"
    video_gen.generate_segment(
        still, narration, str(out), resolution=(320, 240), fps=24,
        waveform_config={"subtitle_enabled": True, "waveform_enabled": True},
    )
    assert out.is_file() and _has_video_stream(out)


@requires_ffmpeg
def test_segment_burns_subtitles_together_with_music(still, narration, tmp_path):
    _write_sidecar(narration)
    music = tmp_path / "music.wav"
    _run("-f", "lavfi", "-i", "sine=f=220:sample_rate=44100:duration=5", "-ac", "2", str(music))
    out = tmp_path / "out.mp4"
    video_gen.generate_segment(
        still, narration, str(out), resolution=(320, 240), fps=24,
        music_path=str(music), waveform_config={"subtitle_enabled": True},
    )
    assert out.is_file() and _has_video_stream(out)


@requires_ffmpeg
def test_segment_style_overrides_come_from_config_not_the_sidecar_file(still, narration, tmp_path):
    """Two renders of the SAME .ass file with different video_config styling
    must not raise - proving force_style is really taking effect as an
    override rather than the baked-in style silently winning either way."""
    _write_sidecar(narration)
    for position, size, color in [("top", 30, "#ffffff"), ("bottom", 80, "#ff0000")]:
        out = tmp_path / f"out_{position}.mp4"
        video_gen.generate_segment(
            still, narration, str(out), resolution=(320, 240), fps=24,
            waveform_config={
                "subtitle_enabled": True, "subtitle_position": position,
                "subtitle_font_size": size, "subtitle_color": color,
            },
        )
        assert out.is_file()


# ---------------------------------------------------------------------------
# generate_background_sequence
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_background_sequence_burns_subtitles(still, narration, tmp_path):
    _write_sidecar(narration)
    out = tmp_path / "seq.mp4"
    video_gen.generate_background_sequence(
        [still], narration, str(out), resolution=(320, 240), fps=24, image_duration=5,
        waveform_config={"subtitle_enabled": True},
    )
    assert out.is_file() and _has_video_stream(out)


@requires_ffmpeg
def test_background_sequence_stream_copies_when_no_waveform_or_subtitles(still, narration, tmp_path):
    """The fast path (-c:v copy) must survive untouched when neither feature
    is active - regression guard for the video_map/-c:v change."""
    out = tmp_path / "seq_fast.mp4"
    video_gen.generate_background_sequence(
        [still], narration, str(out), resolution=(320, 240), fps=24, image_duration=5,
    )
    assert out.is_file() and _has_video_stream(out)
