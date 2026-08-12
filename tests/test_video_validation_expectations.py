"""validate_video(expected=...) — render expectations enforced at the snapshot worker."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.video_integrity import (
    VideoExpectation,
    validation_report_json,
    validate_video,
)


def _probe(*, duration="10", width=1920, height=1080, fps="30000/1001",
           pix_fmt="yuv420p", rotate=0, audio_duration=None):
    video = {
        "codec_type": "video", "codec_name": "h264", "duration": duration,
        "width": width, "height": height, "avg_frame_rate": fps,
        "pix_fmt": pix_fmt, "tags": {"rotate": str(rotate)},
    }
    return SimpleNamespace(
        returncode=0,
        stdout=json.dumps({
            "streams": [
                video,
                {"codec_type": "audio", "codec_name": "aac",
                 "duration": audio_duration or duration},
            ],
            "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": duration},
        }),
        stderr="",
    )


def _run(monkeypatch, probe, expected=None):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return probe if "ffprobe" in str(cmd[0]).lower() else SimpleNamespace(
            returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.video_integrity.subprocess.run", run)
    monkeypatch.setattr("app.video_integrity.Settings.get_ffprobe_path", lambda: "ffprobe")
    monkeypatch.setattr("app.video_integrity.Settings.get_ffmpeg_path", lambda: "ffmpeg")
    return validate_video, calls


@pytest.mark.parametrize(("override", "code", "expected"), [
    (dict(duration="20"), "duration_mismatch", None),
    (dict(width=1280, height=720), "resolution_mismatch", None),
    (dict(width=1921, height=1081), "odd_dimensions",
     VideoExpectation(width=1921, height=1081)),
    (dict(rotate=180), "rotation_mismatch", None),
    (dict(fps="24000/1001"), "fps_mismatch", None),
    (dict(pix_fmt="yuv420p10le"), "pixel_format_mismatch", None),
])
def test_expectation_violations_fail_validation(tmp_path, monkeypatch, override, code, expected):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"media")
    validate, _ = _run(monkeypatch, _probe(**override))
    if expected is None:
        expected = VideoExpectation(duration_seconds=10, width=1920, height=1080,
                                    fps=29.97, pixel_format="yuv420p", rotation=0)
    result = validate(path, expected=expected)
    assert result.valid is False
    assert result.error_code == code
    assert result.message


def test_matching_expectations_pass_without_warnings(tmp_path, monkeypatch):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"media")
    validate, _ = _run(monkeypatch, _probe(duration="10"))
    expected = VideoExpectation(duration_seconds=10, width=1920, height=1080,
                                fps=29.97, pixel_format="yuv420p", rotation=0)
    result = validate(path, expected=expected)
    assert result.valid is True
    assert result.error_code is None
    assert result.warnings == ()


def test_near_duration_mismatch_warns_but_passes(tmp_path, monkeypatch):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"media")
    validate, _ = _run(monkeypatch, _probe(duration="11.5"))
    result = validate(path, expected=VideoExpectation(duration_seconds=10))
    assert result.valid is True
    assert any("differs from expected" in w for w in result.warnings)


def test_video_expectation_from_any_and_roundtrip():
    as_dict = {"duration_seconds": 12.5, "width": 854, "height": 480,
               "fps": 23.97, "pixel_format": "yuv420p", "rotation": 90}
    parsed = VideoExpectation.from_any(as_dict)
    assert parsed == VideoExpectation(**as_dict)
    assert VideoExpectation.from_any(parsed) is parsed
    assert VideoExpectation.from_any(None) is None
    assert VideoExpectation.from_any({"width": 640}).width == 640
    assert video_expectation_defaults_are_none(VideoExpectation.from_any({"width": 640}))


def video_expectation_defaults_are_none(parsed):
    return parsed.duration_seconds is None and parsed.fps is None and parsed.pixel_format == ""


def test_report_json_carries_facts_and_expected(tmp_path, monkeypatch):
    path = tmp_path / "v.mp4"
    path.write_bytes(b"media")
    validate, calls = _run(monkeypatch, _probe(duration="10"))
    expected = VideoExpectation(duration_seconds=10, width=1920, height=1080)
    result = validate(path, expected=expected)
    report = json.loads(validation_report_json(result, expected))
    assert report["valid"] is True
    assert report["facts"]["width"] == 1920
    assert report["facts"]["fps"] == pytest.approx(29.97, abs=0.01)
    assert report["expected"] == {
        "duration_seconds": 10, "width": 1920, "height": 1080,
        "fps": None, "pixel_format": "", "rotation": None,
    }
    assert calls and all(isinstance(c, list) for c in calls)


def test_full_probe_called_for_patch_video_expected(tmp_path, monkeypatch):
    """The snapshot handler feeds ffprobe-derived expectations; probe then decode."""
    path = tmp_path / "v.mp4"
    path.write_bytes(b"media")
    probes = []

    def run(cmd, **kwargs):
        probes.append(str(cmd[0]).lower())
        return _probe(duration="10") if "ffprobe" in str(cmd[0]).lower() else SimpleNamespace(
            returncode=0, stdout="", stderr="")

    monkeypatch.setattr("app.video_integrity.subprocess.run", run)
    monkeypatch.setattr("app.video_integrity.Settings.get_ffprobe_path", lambda: "ffprobe")
    monkeypatch.setattr("app.video_integrity.Settings.get_ffmpeg_path", lambda: "ffmpeg")
    result = validate_video(path, expected=VideoExpectation(duration_seconds=10))
    assert result.valid is True
    assert "ffprobe" in probes and "ffmpeg" in probes