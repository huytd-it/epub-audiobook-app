"""generate_standalone_video must forward music params to generate_segment."""
from unittest.mock import patch

from app import video_gen


def test_music_branch_labels_video_output(tmp_path, monkeypatch):
    """ffmpeg 5+ rejects -map 0:v when 0:v is consumed by a complex filtergraph.

    The music branch must label the video chain ([vout]) and map the label.
    """
    img = tmp_path / "i.png"
    img.write_bytes(b"x")
    aud = tmp_path / "a.wav"
    aud.write_bytes(b"x")
    mus = tmp_path / "m.mp3"
    mus.write_bytes(b"x")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class R:
            stdout = ""
            returncode = 0

        return R()

    monkeypatch.setattr(video_gen.subprocess, "run", fake_run)
    video_gen.generate_segment(
        str(img), str(aud), str(tmp_path / "o.mp4"), music_path=str(mus),
    )
    cmd = captured["cmd"]
    fc = cmd[cmd.index("-filter_complex") + 1]
    assert "[vout]" in fc
    maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
    assert "[vout]" in maps
    assert "0:v" not in maps


def test_standalone_forwards_music_params():
    with patch.object(video_gen, "generate_segment") as seg:
        video_gen.generate_standalone_video(
            "a.mp3", "i.jpg", "o.mp4",
            music_path="m.mp3", music_volume=0.25,
        )
    kwargs = seg.call_args.kwargs
    assert kwargs["music_path"] == "m.mp3"
    assert kwargs["music_volume"] == 0.25


def test_standalone_defaults_no_music():
    with patch.object(video_gen, "generate_segment") as seg:
        video_gen.generate_standalone_video("a.mp3", "i.jpg", "o.mp4")
    kwargs = seg.call_args.kwargs
    assert kwargs["music_path"] is None
    assert kwargs["music_volume"] == 0.15


def test_standalone_forwards_crf_as_encode_quality():
    with patch.object(video_gen, "generate_segment") as seg:
        video_gen.generate_standalone_video("a.mp3", "i.jpg", "o.mp4", crf=18)
    assert seg.call_args.kwargs["crf"] == 18


def test_segment_uses_crf_when_quality_is_not_explicit(tmp_path, monkeypatch):
    image = tmp_path / "image.jpg"
    audio = tmp_path / "audio.wav"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class Result:
            stdout = ""
            returncode = 0

        return Result()

    monkeypatch.setattr(video_gen.subprocess, "run", fake_run)
    video_gen.generate_segment(str(image), str(audio), str(tmp_path / "out.mp4"), crf=18)
    cmd = captured["cmd"]
    assert cmd[cmd.index("-crf") + 1] == "18"
    assert cmd[cmd.index("-b:a") + 1] == "320k"


def test_segment_uses_nvenc_cq_and_configured_audio_bitrate(tmp_path, monkeypatch):
    image = tmp_path / "image.jpg"
    audio = tmp_path / "audio.wav"
    image.write_bytes(b"image")
    audio.write_bytes(b"audio")
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd

        class Result:
            stdout = ""
            returncode = 0

        return Result()

    monkeypatch.setattr(video_gen.subprocess, "run", fake_run)
    video_gen.generate_segment(
        str(image), str(audio), str(tmp_path / "out.mp4"),
        codec="h264_nvenc", quality=20, audio_bitrate="320k",
    )
    cmd = captured["cmd"]
    assert "h264_nvenc" in cmd
    assert cmd[cmd.index("-cq") + 1] == "20"
    assert cmd[cmd.index("-b:a") + 1] == "320k"
    assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"


def test_standalone_adds_greeting_audio(tmp_path):
    with patch.object(video_gen, "generate_segment") as seg, \
         patch.object(video_gen, "concat_segments") as concat:
        video_gen.generate_standalone_video(
            "a.mp3", "i.jpg", "o.mp4",
            intro_audio="intro.mp3", outro_audio="outro.mp3",
        )
    assert [call.args[1] for call in seg.call_args_list] == ["intro.mp3", "a.mp3", "outro.mp3"]
    assert len(concat.call_args.args[0]) == 3
