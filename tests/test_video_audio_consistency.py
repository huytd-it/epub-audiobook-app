"""Every segment must encode AAC at one fixed sample rate / channel count, or
concat -c copy produces an MP4 whose AAC decoder config changes mid-stream.

`concat -c copy` stream-copies the audio and writes a single AudioSpecificConfig
taken from the *first* segment. A later segment encoded at a different sample
rate keeps its own frame layout, so any decoder configured from that first
header walks off the end of the bitstream at the boundary:

    [aac] channel element 0.12 is not allocated
    [aac] Number of bands (48) exceeds limit (47).

ffmpeg still muxes the file happily and ffprobe still reports one clean aac
stream -- the damage only surfaces on decode, which is why validate_video
reports `decode_failed` and YouTube rejects the upload.

The mismatch is reachable in production because segment audio parameters were
inherited from whatever file each segment happened to use: greeting audio is a
byte-for-byte user upload (any rate), while narration is TTS output at the
engine's rate (24000 for voxcpm, 48000 elsewhere in data/books).

Real ffmpeg is used on purpose: this bug lives entirely in the ffmpeg argv, so a
mocked subprocess would keep passing while production stayed broken.
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


def _audio_format(path: str) -> tuple[str, int, int]:
    """(codec_name, sample_rate, channels) of a file's first audio stream."""
    out = subprocess.run(
        [settings.get_ffprobe_path(), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,sample_rate,channels",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    codec, rate, channels = [l for l in out.stdout.strip().splitlines() if l]
    return codec, int(rate), int(channels)


def _tone(path: Path, *, rate: int, channels: int, seconds: float) -> str:
    _run("-f", "lavfi", "-i", f"sine=f=440:sample_rate={rate}:duration={seconds}",
         "-ac", str(channels), "-ar", str(rate), str(path))
    return str(path)


@pytest.fixture
def still(tmp_path):
    img = tmp_path / "still.png"
    _run("-f", "lavfi", "-i", "color=c=blue:s=160x120", "-frames:v", "1", str(img))
    return str(img)


# ---------------------------------------------------------------------------
# Root cause: generate_segment let the input dictate the output audio format.
# ---------------------------------------------------------------------------

@requires_ffmpeg
@pytest.mark.parametrize("rate,channels", [(24000, 1), (44100, 2), (48000, 1), (22050, 2)])
def test_segment_output_audio_is_normalized(still, tmp_path, rate, channels):
    """Whatever the narration's rate/layout, the segment must come out uniform."""
    narration = _tone(tmp_path / f"n_{rate}_{channels}.wav",
                      rate=rate, channels=channels, seconds=2)
    out = tmp_path / f"seg_{rate}_{channels}.mp4"
    video_gen.generate_segment(still, narration, str(out), resolution=(160, 120), fps=30)

    codec, out_rate, out_channels = _audio_format(str(out))
    assert codec == "aac"
    assert (out_rate, out_channels) == (video_gen.AUDIO_SAMPLE_RATE, video_gen.AUDIO_CHANNELS), (
        f"{rate}Hz/{channels}ch input produced {out_rate}Hz/{out_channels}ch output -- "
        "the audio format is being inherited from the input"
    )


@requires_ffmpeg
def test_segment_with_music_normalizes_audio(still, tmp_path):
    """The amix branch must pin the format too: amix adopts its first input."""
    narration = _tone(tmp_path / "n.wav", rate=24000, channels=1, seconds=2)
    music = _tone(tmp_path / "m.wav", rate=44100, channels=2, seconds=10)
    out = tmp_path / "seg_music.mp4"
    video_gen.generate_segment(still, narration, str(out), resolution=(160, 120),
                               fps=30, music_path=music)

    assert _audio_format(str(out))[1:] == (video_gen.AUDIO_SAMPLE_RATE, video_gen.AUDIO_CHANNELS)


@requires_ffmpeg
def test_segment_with_waveform_preserves_audio(still, tmp_path):
    narration = _tone(tmp_path / "wave.wav", rate=24000, channels=1, seconds=2)
    out = tmp_path / "waveform.mp4"
    video_gen.generate_segment(
        still, narration, str(out), resolution=(160, 120), fps=30,
        waveform_config={
            "waveform_enabled": True, "waveform_style": "cline",
            "waveform_color": "#22d3ee", "waveform_position": "center",
            "waveform_height": 40, "waveform_opacity": 0.9,
        },
    )

    assert _audio_format(str(out))[1:] == (video_gen.AUDIO_SAMPLE_RATE, video_gen.AUDIO_CHANNELS)


@requires_ffmpeg
def test_concat_of_greeting_and_narration_decodes_cleanly(still, tmp_path):
    """The production shape that produced `decode_failed` on a real upload.

    A 44100Hz stereo uploaded greeting in front of 24000Hz mono TTS narration.
    Before the fix the concat muxed fine and probed fine, but decoding it failed
    with "channel element 0.12 is not allocated".
    """
    from app.video_integrity import validate_video

    intro = _tone(tmp_path / "intro.wav", rate=44100, channels=2, seconds=2)
    main = _tone(tmp_path / "main.wav", rate=24000, channels=1, seconds=4)

    intro_seg = tmp_path / "intro.mp4"
    main_seg = tmp_path / "main.mp4"
    video_gen.generate_segment(still, intro, str(intro_seg), resolution=(160, 120), fps=30)
    video_gen.generate_segment(still, main, str(main_seg), resolution=(160, 120), fps=30)

    out = tmp_path / "joined.mp4"
    video_gen.concat_segments([str(intro_seg), str(main_seg)], str(out))

    result = validate_video(str(out))
    assert result.valid, f"{result.error_code}: {result.message}"


@requires_ffmpeg
@pytest.mark.parametrize("rate", [24000, 44100])
@pytest.mark.parametrize("source", ["still", "video_bg"])
def test_segment_av_durations_stay_aligned(tmp_path, rate, source):
    """Resampling must not let the video track outrun the audio.

    Both input shapes are infinite ('-loop 1' / '-stream_loop -1'), so '-shortest'
    decides the length. Once the audio is resampled the resampler's latency delays
    the audio EOF and ~1.5s of surplus video frames slip through, which concat then
    compounds into the A/V drift that makes YouTube reject the upload.
    """
    if source == "still":
        background = tmp_path / "bg.png"
        _run("-f", "lavfi", "-i", "color=c=blue:s=160x120", "-frames:v", "1", str(background))
    else:
        background = tmp_path / "bg.mp4"
        _run("-f", "lavfi", "-i", "color=c=red:s=160x120:r=24", "-t", "3",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(background))

    narration = _tone(tmp_path / "n.wav", rate=rate, channels=1, seconds=5)
    out = tmp_path / "seg.mp4"
    video_gen.generate_segment(str(background), narration, str(out),
                               resolution=(160, 120), fps=30)

    probe = subprocess.run(
        [settings.get_ffprobe_path(), "-v", "error", "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out)],
        check=True, capture_output=True, text=True,
    )
    vdur, adur = [float(l) for l in probe.stdout.strip().splitlines() if l][:2]
    assert abs(vdur - adur) < 0.15, (
        f"video {vdur:.3f}s vs audio {adur:.3f}s -- the resampler's latency let "
        "surplus video frames through '-shortest'"
    )


@requires_ffmpeg
def test_background_sequence_normalizes_audio(tmp_path):
    """The rotating-background path re-encodes audio in its own final mux."""
    backgrounds = []
    for index, color in enumerate(("red", "green")):
        path = tmp_path / f"bg{index}.png"
        _run("-f", "lavfi", "-i", f"color=c={color}:s=160x120", "-frames:v", "1", str(path))
        backgrounds.append(str(path))
    narration = _tone(tmp_path / "n.wav", rate=24000, channels=1, seconds=6)

    out = tmp_path / "seq.mp4"
    video_gen.generate_background_sequence(
        backgrounds, narration, str(out),
        resolution=(160, 120), fps=30, image_duration=3,
    )

    assert _audio_format(str(out)) == ("aac", video_gen.AUDIO_SAMPLE_RATE, video_gen.AUDIO_CHANNELS)


@requires_ffmpeg
def test_segment_and_background_sequence_concat_together(tmp_path, still):
    """generate_full_video mixes both renderers into one concat list."""
    from app.video_integrity import validate_video

    backgrounds = []
    for index, color in enumerate(("red", "green")):
        path = tmp_path / f"bg{index}.png"
        _run("-f", "lavfi", "-i", f"color=c={color}:s=160x120", "-frames:v", "1", str(path))
        backgrounds.append(str(path))

    intro = _tone(tmp_path / "intro.wav", rate=44100, channels=2, seconds=2)
    main = _tone(tmp_path / "main.wav", rate=24000, channels=1, seconds=6)

    intro_seg = tmp_path / "intro.mp4"
    video_gen.generate_segment(still, intro, str(intro_seg), resolution=(160, 120), fps=30)
    main_seg = tmp_path / "main.mp4"
    video_gen.generate_background_sequence(
        backgrounds, main, str(main_seg),
        resolution=(160, 120), fps=30, image_duration=3,
    )

    out = tmp_path / "joined.mp4"
    video_gen.concat_segments([str(intro_seg), str(main_seg)], str(out))

    result = validate_video(str(out))
    assert result.valid, f"{result.error_code}: {result.message}"


# ---------------------------------------------------------------------------
# Defense in depth: never emit a silently broken concat.
# ---------------------------------------------------------------------------

@requires_ffmpeg
@pytest.mark.parametrize("first,second", [
    ((44100, 2), (24000, 2)),   # high -> low: "channel element N is not allocated"
    ((24000, 2), (44100, 2)),   # low -> high: "Number of bands exceeds limit"
    ((48000, 2), (48000, 1)),   # channel-count only
])
def test_concat_rejects_mismatched_audio_format(tmp_path, first, second):
    """An audio-format mismatch must fail loudly instead of producing bad output."""
    paths = []
    for index, (rate, channels) in enumerate((first, second)):
        path = tmp_path / f"seg{index}.mp4"
        _run("-f", "lavfi", "-i", "color=c=red:s=160x120:r=30",
             "-f", "lavfi", "-i", f"sine=f=440:sample_rate={rate}", "-t", "2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-ar", str(rate), "-ac", str(channels), str(path))
        paths.append(str(path))

    with pytest.raises(ValueError, match="audio format"):
        video_gen.concat_segments(paths, str(tmp_path / "o.mp4"))


@requires_ffmpeg
def test_concat_accepts_uniform_audio_format(tmp_path):
    """The guard must not reject a concat ffmpeg itself would handle correctly."""
    paths = []
    for index in range(2):
        path = tmp_path / f"ok{index}.mp4"
        _run("-f", "lavfi", "-i", "color=c=red:s=160x120:r=30",
             "-f", "lavfi", "-i", "sine=f=440:sample_rate=48000", "-t", "2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-ar", "48000", "-ac", "2", str(path))
        paths.append(str(path))

    out = tmp_path / "ok.mp4"
    video_gen.concat_segments(paths, str(out))
    assert Path(out).stat().st_size > 0


# ---------------------------------------------------------------------------
# argv-level guards, so the regression is visible without running ffmpeg.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("music", [None, "m.mp3"])
def test_segment_argv_pins_audio_format(tmp_path, monkeypatch, music):
    src = tmp_path / "bg.png"
    src.write_bytes(b"x")
    aud = tmp_path / "a.wav"
    aud.write_bytes(b"x")
    if music:
        (tmp_path / music).write_bytes(b"x")
        music = str(tmp_path / music)

    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class R:
            stdout = ""
            returncode = 0

        return R()

    monkeypatch.setattr(video_gen.subprocess, "run", fake_run)
    video_gen.generate_segment(str(src), str(aud), str(tmp_path / "o.mp4"),
                               fps=30, music_path=music)
    cmd = captured["cmd"]
    assert "-ar" in cmd, "output sample rate is not pinned"
    assert cmd[cmd.index("-ar") + 1] == str(video_gen.AUDIO_SAMPLE_RATE)
    assert "-ac" in cmd, "output channel count is not pinned"
    assert cmd[cmd.index("-ac") + 1] == str(video_gen.AUDIO_CHANNELS)


def test_background_sequence_argv_pins_audio_format(tmp_path, monkeypatch):
    """generate_background_sequence re-encodes audio in its final mux."""
    backgrounds = []
    for name in ("a.png", "b.png"):
        path = tmp_path / name
        path.write_bytes(b"x")
        backgrounds.append(str(path))
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")

    commands: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd)

        class R:
            stdout = "6.0"
            returncode = 0

        return R()

    monkeypatch.setattr(video_gen.subprocess, "run", fake_run)
    monkeypatch.setattr(video_gen, "concat_segments", lambda *a, **k: None)
    video_gen.generate_background_sequence(
        backgrounds, str(audio), str(tmp_path / "o.mp4"),
        resolution=(160, 120), fps=30, image_duration=3,
    )

    mux = [c for c in commands if "-c:a" in c]
    assert mux, "no audio-encoding command was issued"
    for cmd in mux:
        assert cmd[cmd.index("-ar") + 1] == str(video_gen.AUDIO_SAMPLE_RATE)
        assert cmd[cmd.index("-ac") + 1] == str(video_gen.AUDIO_CHANNELS)
