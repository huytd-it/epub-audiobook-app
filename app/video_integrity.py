"""Queue-independent validation for videos that may be uploaded to YouTube."""
from __future__ import annotations

import json
import math
import subprocess
import time
from dataclasses import dataclass
from math import ceil
from pathlib import Path

from app.config import Settings

MAX_ERROR_CHARS = 2000
DRIFT_WARN_SECONDS = 1.0
DRIFT_FATAL_SECONDS = 5.0
# Hard YouTube upload limits the validator must catch before a job wastes a quota.
MAX_DURATION_WARN_SECONDS = 12 * 3600
MAX_DURATION_FATAL_SECONDS = 24 * 3600
MAX_SIZE_WARN_BYTES = 100 * 1024**3
MAX_SIZE_FATAL_BYTES = 256 * 1024**3
RECOVERABLE_OUTPUT_CODES = frozenset({
    "probe_failed", "missing_video_stream", "missing_audio_stream",
    "invalid_duration", "unsupported_format", "av_drift", "decode_failed",
    "duration_mismatch", "resolution_mismatch", "fps_mismatch",
    "pixel_format_mismatch", "rotation_mismatch", "odd_dimensions",
})
_VIDEO_CODECS = frozenset({"h264", "hevc"})
_AUDIO_CODECS = frozenset({"aac", "mp3"})
_CONTAINERS = frozenset({"mp4", "mov", "m4v"})
# Expected-input tolerances: a render that drifts this far from what was asked for
# is a bug in the render, not an acceptable variation.
EXPECTED_DURATION_WARN_SECONDS = 1.0
EXPECTED_DURATION_FATAL_SECONDS = 5.0
EXPECTED_FPS_TOLERANCE = 0.5


@dataclass(frozen=True)
class VideoExpectation:
    """What a render was asked to produce. Any field left None is not checked.

    ``duration_seconds`` is the expected output length (WAV + intro/outro), so a
    truncated/mis-muxed render fails here instead of wasting a YouTube quota.
    ``pixel_format`` is the encoder's target (e.g. ``yuv420p``), ``rotation`` the
    container metadata (0/90/180/270)."""
    duration_seconds: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    pixel_format: str = ""
    rotation: int | None = None
    require_audio: bool = True

    @classmethod
    def from_any(cls, raw: "VideoExpectation | dict | None") -> "VideoExpectation | None":
        if raw is None:
            return None
        if isinstance(raw, VideoExpectation):
            return raw
        if not isinstance(raw, dict):
            return None
        try:
            return cls(
                duration_seconds=float(raw["duration_seconds"]) if raw.get("duration_seconds") is not None else None,
                width=int(raw["width"]) if raw.get("width") is not None else None,
                height=int(raw["height"]) if raw.get("height") is not None else None,
                fps=float(raw["fps"]) if raw.get("fps") is not None else None,
                pixel_format=str(raw["pixel_format"]) if raw.get("pixel_format") else "",
                rotation=int(raw["rotation"]) if raw.get("rotation") is not None else None,
                require_audio=bool(raw.get("require_audio", True)),
            )
        except (TypeError, ValueError):
            return None

    def as_dict(self) -> dict:
        return {
            "duration_seconds": self.duration_seconds,
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "pixel_format": self.pixel_format,
            "rotation": self.rotation,
        }


@dataclass(frozen=True)
class ValidationFacts:
    container: str = ""
    video_codec: str = ""
    audio_codec: str = ""
    video_duration: float = 0.0
    audio_duration: float = 0.0
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pixel_format: str = ""
    rotation: int = 0
    file_size_bytes: int = 0
    video_streams: int = 0
    audio_streams: int = 0


def validation_report_json(result: ValidationResult,
                           expected: "VideoExpectation | dict | None" = None) -> str:
    """Structured, persisted snapshot of a validation outcome (facts + verdict)."""
    f = result.facts
    report = {
        "valid": result.valid,
        "error_code": result.error_code,
        "message": result.message,
        "warnings": list(result.warnings),
        "elapsed_seconds": round(result.elapsed_seconds, 3),
        "facts": {
            "container": f.container,
            "video_codec": f.video_codec,
            "audio_codec": f.audio_codec,
            "video_duration": f.video_duration,
            "audio_duration": f.audio_duration,
            "width": f.width,
            "height": f.height,
            "fps": f.fps,
            "pixel_format": f.pixel_format,
            "rotation": f.rotation,
            "file_size_bytes": f.file_size_bytes,
            "video_streams": f.video_streams,
            "audio_streams": f.audio_streams,
        },
    }
    expectation = VideoExpectation.from_any(expected)
    if expectation is not None:
        report["expected"] = expectation.as_dict()
    return json.dumps(report, ensure_ascii=False)


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    error_code: str | None
    message: str
    warnings: tuple[str, ...]
    facts: ValidationFacts
    elapsed_seconds: float


def decode_timeout(duration_seconds: float) -> int:
    return min(21600, max(300, ceil(max(0.0, duration_seconds) * 2 + 120)))


def _result(started: float, *, valid: bool = False, code: str | None = None,
            message: str = "", warnings: tuple[str, ...] = (),
            facts: ValidationFacts | None = None) -> ValidationResult:
    return ValidationResult(valid, code, message[-MAX_ERROR_CHARS:], warnings,
                            facts or ValidationFacts(), time.monotonic() - started)


def _duration(stream: dict, format_info: dict) -> float:
    raw = stream.get("duration")
    if raw in (None, ""):
        raw = format_info.get("duration")
    value = float(raw)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("duration must be finite and positive")
    return value


def _expected_mismatch(facts: ValidationFacts, expected: VideoExpectation) -> tuple[str, str] | None:
    """First violated expectation, if any. None => everything expected checks out."""
    if expected.duration_seconds is not None:
        diff = abs(facts.video_duration - expected.duration_seconds)
        if diff > EXPECTED_DURATION_FATAL_SECONDS:
            return ("duration_mismatch",
                    f"video duration {facts.video_duration:.2f}s != expected {expected.duration_seconds:.2f}s")
    if expected.rotation is not None and facts.rotation != expected.rotation:
        return ("rotation_mismatch", f"rotation {facts.rotation} != expected {expected.rotation}")
    if expected.width is not None and expected.height is not None:
        swapped = facts.width == expected.height and facts.height == expected.width
        matched = (facts.width == expected.width and facts.height == expected.height) or (
            swapped and facts.rotation in (90, 270))
        if not matched:
            return ("resolution_mismatch",
                    f"resolution {facts.width}x{facts.height} != expected {expected.width}x{expected.height}")
        if facts.width % 2 or facts.height % 2:
            return ("odd_dimensions", f"odd dimensions {facts.width}x{facts.height} cannot be encoded by libx264")
    if expected.fps is not None and abs(facts.fps - expected.fps) > EXPECTED_FPS_TOLERANCE:
        return ("fps_mismatch", f"fps {facts.fps} != expected {expected.fps}")
    if expected.pixel_format and facts.pixel_format != expected.pixel_format:
        return ("pixel_format_mismatch",
                f"pixel format '{facts.pixel_format}' != expected '{expected.pixel_format}'")
    return None


def _expected_warnings(facts: ValidationFacts, expected: VideoExpectation) -> tuple[str, ...]:
    if expected.duration_seconds is None:
        return ()
    diff = abs(facts.video_duration - expected.duration_seconds)
    if diff > EXPECTED_DURATION_WARN_SECONDS:
        return (f"video duration {facts.video_duration:.2f}s differs from expected {expected.duration_seconds:.2f}s",)
    return ()


def validate_video(path: str | Path, *, expected: "VideoExpectation | dict | None" = None) -> ValidationResult:
    """Probe, decode and (optionally) enforce the expected output of a render.

    ``expected`` is keyword-only so every existing ``validate_video(path)`` caller
    keeps its behavior; the snapshot worker passes what the render was asked to
    produce so a mismatched file fails here instead of reaching YouTube."""
    expectation = VideoExpectation.from_any(expected)
    started = time.monotonic()
    media = Path(path)
    if not media.is_file():
        return _result(started, code="file_missing", message=f"file not found: {media}")
    if media.stat().st_size == 0:
        return _result(started, code="file_empty", message=f"file is empty: {media}")

    probe_cmd = [
        Settings.get_ffprobe_path(), "-v", "error", "-print_format", "json",
        "-show_streams", "-show_format", str(media),
    ]
    try:
        probe = subprocess.run(probe_cmd, capture_output=True, text=True)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return _result(started, code="tool_unavailable", message=str(exc))
    if probe.returncode != 0:
        return _result(started, code="probe_failed", message=probe.stderr or "ffprobe failed")
    try:
        info = json.loads(probe.stdout)
        streams = info.get("streams") or []
        format_info = info.get("format") or {}
    except (json.JSONDecodeError, TypeError, AttributeError) as exc:
        return _result(started, code="probe_failed", message=str(exc))

    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if video is None:
        return _result(started, code="missing_video_stream", message="no video stream")
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if audio is None and (expectation is None or expectation.require_audio):
        return _result(started, code="missing_audio_stream", message="no audio stream")
    try:
        video_duration = _duration(video, format_info)
        audio_duration = _duration(audio, format_info) if audio is not None else video_duration
    except (TypeError, ValueError, OverflowError) as exc:
        return _result(started, code="invalid_duration", message=str(exc))

    container = str(format_info.get("format_name") or "")
    video_codec = str(video.get("codec_name") or "").lower()
    audio_codec = str(audio.get("codec_name") or "").lower() if audio is not None else ""
    size_bytes = media.stat().st_size
    fps = 0.0
    try:
        raw_rate = video.get("avg_frame_rate") or video.get("r_frame_rate") or "0"
        if "/" in str(raw_rate):
            num, den = str(raw_rate).split("/", 1)
            fps = float(num) / float(den) if float(den) else 0.0
        else:
            fps = float(raw_rate)
    except (TypeError, ValueError, ZeroDivisionError):
        fps = 0.0
    try:
        rotation = int((video.get("tags") or {}).get("rotate") or 0)
        if rotation not in (0, 90, 180, 270):
            rotation = 0
    except (TypeError, ValueError):
        rotation = 0
    facts = ValidationFacts(
        container=container, video_codec=video_codec, audio_codec=audio_codec,
        video_duration=video_duration, audio_duration=audio_duration,
        width=int(video.get("width") or 0), height=int(video.get("height") or 0),
        fps=fps, pixel_format=str(video.get("pix_fmt") or "").lower(),
        rotation=rotation, file_size_bytes=size_bytes,
        video_streams=sum(1 for s in streams if s.get("codec_type") == "video"),
        audio_streams=sum(1 for s in streams if s.get("codec_type") == "audio"),
    )
    if video_duration > MAX_DURATION_FATAL_SECONDS:
        return _result(started, code="duration_too_long",
                       message=f"video duration {video_duration:.0f}s exceeds {MAX_DURATION_FATAL_SECONDS}s",
                       facts=facts)
    if size_bytes > MAX_SIZE_FATAL_BYTES:
        return _result(started, code="file_too_large",
                       message=f"video size {size_bytes} bytes exceeds {MAX_SIZE_FATAL_BYTES} bytes",
                       facts=facts)
    audio_supported = audio_codec in _AUDIO_CODECS if audio is not None else bool(expectation and not expectation.require_audio)
    if not (_CONTAINERS.intersection(part.strip().lower() for part in container.split(","))
            and video_codec in _VIDEO_CODECS and audio_supported):
        return _result(started, code="unsupported_format",
                       message=f"unsupported output: {container}/{video_codec}/{audio_codec}",
                       facts=facts)

    drift = abs(video_duration - audio_duration)
    if drift >= DRIFT_FATAL_SECONDS:
        return _result(started, code="av_drift",
                       message=f"audio/video duration drift is {drift:.3f}s", facts=facts)
    warnings = ((f"audio/video duration drift is {drift:.3f}s",)
                if drift >= DRIFT_WARN_SECONDS else ())
    if video_duration > MAX_DURATION_WARN_SECONDS:
        warnings = warnings + (f"video duration {video_duration:.0f}s is close to YouTube's limit",)
    if size_bytes > MAX_SIZE_WARN_BYTES:
        warnings = warnings + (f"video size {size_bytes} bytes is close to YouTube's limit",)

    if expectation is not None:
        mismatch = _expected_mismatch(facts, expectation)
        if mismatch is not None:
            code, message = mismatch
            return _result(started, code=code, message=message, facts=facts)
        warnings = warnings + _expected_warnings(facts, expectation)

    decode_cmd = [
        Settings.get_ffmpeg_path(), "-v", "error", "-xerror", "-i", str(media),
        "-map", "0:v:0",
    ]
    if audio is not None:
        decode_cmd.extend(["-map", "0:a:0"])
    decode_cmd.extend(["-f", "null", "-"])
    try:
        decode = subprocess.run(
            decode_cmd, capture_output=True, text=True,
            timeout=decode_timeout(max(video_duration, audio_duration)),
        )
    except subprocess.TimeoutExpired as exc:
        return _result(started, code="validation_timeout", message=str(exc), facts=facts)
    except (FileNotFoundError, PermissionError, OSError) as exc:
        return _result(started, code="tool_unavailable", message=str(exc), facts=facts)
    if decode.returncode != 0:
        return _result(started, code="decode_failed", message=decode.stderr or "ffmpeg decode failed",
                       facts=facts)
    return _result(started, valid=True, warnings=warnings, facts=facts)
