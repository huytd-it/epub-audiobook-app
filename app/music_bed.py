"""Background music that only fills the silent gaps in the narration.

The old mix looped one track under the whole narration at a fixed volume. That
works for a trailer but fights the voice for an hour-long chapter, so the
default is now a *bed*: the music is placed only where the narration is
actually silent - the pause between chapters, the breath between chunks - and
nowhere else.

The bed is rendered once per segment, ahead of the video mux, into a plain WAV
that is exactly as long as the last gap it fills. Everything downstream keeps
treating it as "the music file": the same ``music_volume`` slider, the same
amix, no special casing in the render paths beyond not looping it.

Two ffmpeg passes do the work:

1. ``silencedetect`` on the narration reports every silence at least
   ``min_gap_ms`` long (:func:`detect_silence_gaps`).
2. One ``filter_complex`` opens the music once per gap, trims it to the gap
   length, fades both edges and delays it to the gap's start; ``amix`` sums the
   pieces (:func:`build_gap_bed`).

Because each gap gets the music *from its own start*, a chapter break plays the
opening bars rather than whatever the loop happened to be on - which is what
makes it read as a deliberate sting instead of a bed that keeps ducking.
"""
from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_DETECT_TIMEOUT = 900
_RENDER_TIMEOUT = 900

# Bed format. Matched to video_gen.AUDIO_SAMPLE_RATE/AUDIO_CHANNELS so the mux
# does not resample it a second time.
BED_SAMPLE_RATE = 48000
BED_CHANNELS = 2

# Defaults, mirrored by video_config.VIDEO_DEFAULTS (the settings UI) - a caller
# that passes no config at all still gets a sane bed.
DEFAULT_MIN_GAP_MS = 1500
DEFAULT_FADE_MS = 400
DEFAULT_THRESHOLD_DB = -40.0
# Keep the music off the edges of the gap: silencedetect reports the exact
# moment the level crosses the threshold, and a word's tail sits just under it.
DEFAULT_EDGE_PAD_MS = 120
# Slack allowed when detecting, so a gap exactly as long as the threshold (the
# default chapter pause is exactly the default threshold) is not missed by a
# millisecond of rounding. See build_detect_command.
DETECT_TOLERANCE_MS = 50
# Below this a placed piece is a click, not a sting - such a gap is skipped.
MIN_PIECE_MS = 250
# One ffmpeg input per gap; a runaway detection (a mis-set threshold on a quiet
# recording) must not build a 5000-input filtergraph.
MAX_PIECES = 240

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")


class MusicBedError(RuntimeError):
    """ffmpeg refused to render the bed."""


@dataclass(frozen=True)
class GapOptions:
    """Validated gap-music settings for one render."""

    enabled: bool = False
    min_gap_ms: int = DEFAULT_MIN_GAP_MS
    fade_ms: int = DEFAULT_FADE_MS
    threshold_db: float = DEFAULT_THRESHOLD_DB
    edge_pad_ms: int = DEFAULT_EDGE_PAD_MS


def _int(raw, fallback: int, low: int, high: int) -> int:
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, value))


def parse_options(config: dict | None) -> GapOptions:
    """Turn a render-config dict (or ``None``) into ``GapOptions``.

    Accepts the snapshot shape used by the render config - ``music_gap_only`` /
    ``music_gap_min_ms`` / ``music_gap_fade_ms`` - as well as the short keys
    (``enabled`` / ``min_gap_ms`` / ``fade_ms``), so a caller can hand in either
    the whole video config or a purpose-built dict.
    """
    config = config or {}
    if not isinstance(config, dict):
        return GapOptions()
    enabled = config.get("music_gap_only", config.get("enabled", False))
    return GapOptions(
        enabled=bool(enabled),
        min_gap_ms=_int(config.get("music_gap_min_ms", config.get("min_gap_ms")), DEFAULT_MIN_GAP_MS, 200, 60000),
        fade_ms=_int(config.get("music_gap_fade_ms", config.get("fade_ms")), DEFAULT_FADE_MS, 0, 5000),
        threshold_db=float(config.get("music_gap_threshold_db", config.get("threshold_db")) or DEFAULT_THRESHOLD_DB),
        edge_pad_ms=_int(config.get("music_gap_edge_pad_ms", config.get("edge_pad_ms")), DEFAULT_EDGE_PAD_MS, 0, 2000),
    )


def is_enabled(config: dict | None) -> bool:
    return parse_options(config).enabled


def parse_silence_log(stderr: str, total_duration: float | None = None) -> list[tuple[float, float]]:
    """Pull ``[start, end)`` pairs out of ffmpeg's silencedetect output.

    A silence that runs to the end of the file has no ``silence_end`` line, so
    it is closed at ``total_duration`` when that is known and dropped otherwise
    (an unbounded gap would make the bed longer than the narration).
    """
    gaps: list[tuple[float, float]] = []
    pending: float | None = None
    for line in (stderr or "").splitlines():
        start = _SILENCE_START_RE.search(line)
        if start:
            pending = max(0.0, float(start.group(1)))
            continue
        end = _SILENCE_END_RE.search(line)
        if end and pending is not None:
            gaps.append((pending, max(pending, float(end.group(1)))))
            pending = None
    if pending is not None and total_duration and total_duration > pending:
        gaps.append((pending, float(total_duration)))
    return gaps


def build_detect_command(audio_path: str | Path, options: GapOptions) -> list[str]:
    """ffmpeg argv for the silencedetect pass (exposed for tests).

    Detection asks for a hair less than the configured gap. The interesting case
    is a threshold set to exactly the chapter pause (both default to 1500ms):
    the silence between two chapters is then *exactly* the length being asked
    for, and a millisecond of rounding either way would make every chapter break
    fall through. ``plan_pieces`` still filters, and its own minimum is looser
    still, so the tolerance widens the window rather than shifting it.
    """
    seconds = max(0.05, (options.min_gap_ms - DETECT_TOLERANCE_MS) / 1000)
    return [
        settings.get_ffmpeg_path(), "-hide_banner", "-nostdin", "-vn",
        "-i", str(audio_path),
        "-af", f"silencedetect=noise={options.threshold_db:g}dB:d={seconds:g}",
        "-f", "null", "-",
    ]


def probe_duration(path: str | Path) -> float | None:
    try:
        result = subprocess.run(
            [settings.get_ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        value = (result.stdout or "").strip()
        return float(value) if value else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def detect_silence_gaps(audio_path: str | Path, options: GapOptions) -> list[tuple[float, float]]:
    """Every silence in ``audio_path`` at least ``min_gap_ms`` long.

    Returns ``[]`` when ffmpeg is unavailable or fails: no bed is better than a
    failed render, and the caller falls back to leaving the music out.
    """
    try:
        result = subprocess.run(
            build_detect_command(audio_path, options),
            capture_output=True, text=True, timeout=_DETECT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("silencedetect failed for %s", audio_path, exc_info=True)
        return []
    if result.returncode != 0:
        logger.warning("silencedetect returned %s for %s: %s",
                       result.returncode, audio_path, (result.stderr or "")[-400:])
        return []
    return parse_silence_log(result.stderr, probe_duration(audio_path))


def plan_pieces(
    gaps: list[tuple[float, float]], options: GapOptions, *, music_duration: float | None = None
) -> list[tuple[float, float]]:
    """Turn detected silences into ``(start_seconds, length_seconds)`` pieces.

    Each gap is inset by ``edge_pad_ms`` at both ends so the music never clips
    the tail of a word, then dropped if what remains is too short to hear as
    anything but a click. A piece is never longer than the music itself - the
    track plays once from its start and stops, it does not loop inside one gap.
    """
    # Milliseconds throughout: the threshold comparison sits exactly on values
    # like 1500 - 2*400, where float subtraction lands a hair below and would
    # drop a gap that does qualify.
    pieces: list[tuple[float, float]] = []
    # Same tolerance the detection pass allows (see build_detect_command), so a
    # gap that only just qualified there is not thrown away here.
    minimum_ms = max(
        MIN_PIECE_MS,
        options.min_gap_ms - DETECT_TOLERANCE_MS - 2 * options.edge_pad_ms,
    )
    for start, end in gaps:
        start_ms = round(start * 1000) + options.edge_pad_ms
        length_ms = round(end * 1000) - options.edge_pad_ms - start_ms
        if length_ms < minimum_ms:
            continue
        if music_duration and music_duration > 0:
            length_ms = min(length_ms, round(music_duration * 1000))
        pieces.append((start_ms / 1000, length_ms / 1000))
        if len(pieces) >= MAX_PIECES:
            logger.warning("gap music: capping at %s pieces", MAX_PIECES)
            break
    return pieces


def build_filter_graph(pieces: list[tuple[float, float]], options: GapOptions) -> str:
    """The filter_complex graph placing one music copy per gap.

    Input ``i`` is the music file opened for piece ``i`` (see
    :func:`build_bed_command`); it is trimmed to the gap, faded at both edges
    and delayed to the gap's start. ``amix`` with ``normalize=0`` sums them
    without touching levels - the pieces never overlap, so summing is exact.
    """
    if not pieces:
        raise ValueError("no gap pieces to render")
    chains: list[str] = []
    for index, (start, length) in enumerate(pieces):
        fade = min(options.fade_ms / 1000, length / 2)
        label = f"[b{index}]" if len(pieces) > 1 else "[bed]"
        steps = [f"atrim=0:{length:.3f}", "asetpts=N/SR/TB"]
        if fade > 0:
            steps.append(f"afade=t=in:st=0:d={fade:.3f}")
            steps.append(f"afade=t=out:st={max(0.0, length - fade):.3f}:d={fade:.3f}")
        delay_ms = int(round(start * 1000))
        if delay_ms > 0:
            steps.append(f"adelay={delay_ms}:all=1")
        chains.append(f"[{index}:a]" + ",".join(steps) + label)
    if len(pieces) > 1:
        inputs = "".join(f"[b{index}]" for index in range(len(pieces)))
        chains.append(f"{inputs}amix=inputs={len(pieces)}:normalize=0:dropout_transition=0[bed]")
    return ";".join(chains)


def build_bed_command(music_path: str | Path, out_path: str | Path, pieces: list[tuple[float, float]],
                      script_path: str | Path) -> list[str]:
    """ffmpeg argv rendering the bed (exposed for tests).

    The music is opened once per piece instead of split from a single input:
    each piece then owns its own decoder and starts at 00:00, and no branch has
    to buffer while another one drains. ``-stream_loop -1`` covers a gap longer
    than the track - ``plan_pieces`` caps the length when the duration is known,
    but the loop makes a wrong probe harmless.
    """
    cmd = [settings.get_ffmpeg_path(), "-y", "-hide_banner", "-nostdin"]
    for _ in pieces:
        cmd += ["-stream_loop", "-1", "-i", str(music_path)]
    cmd += [
        "-filter_complex_script", str(script_path),
        "-map", "[bed]",
        "-c:a", "pcm_s16le", "-ar", str(BED_SAMPLE_RATE), "-ac", str(BED_CHANNELS),
        "-map_metadata", "-1", str(out_path),
    ]
    return cmd


def build_gap_bed(
    audio_path: str | Path, music_path: str | Path, out_path: str | Path, config: dict | None
) -> str | None:
    """Render the gap-only music bed for one narration file.

    Returns the bed path, or ``None`` when the narration has no gap worth
    filling (the caller then renders with no music at all, which is the point of
    the mode). Never raises for a detection miss; only a broken ffmpeg render
    raises ``MusicBedError``.
    """
    options = parse_options(config)
    if not options.enabled:
        return None
    gaps = detect_silence_gaps(audio_path, options)
    pieces = plan_pieces(gaps, options, music_duration=probe_duration(music_path))
    if not pieces:
        logger.info("gap music: no silence >= %sms in %s, rendering without music",
                    options.min_gap_ms, audio_path)
        return None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # The graph grows with the gap count and Windows caps a command line at
    # 32767 characters, so it goes to a script file (same reason
    # video_gen's xfade graph does).
    script_path = out_path.with_suffix(".graph.txt")
    script_path.write_text(build_filter_graph(pieces, options), encoding="utf-8")
    try:
        result = subprocess.run(
            build_bed_command(music_path, out_path, pieces, script_path),
            capture_output=True, text=True, timeout=_RENDER_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise MusicBedError("render nhạc nền theo khoảng lặng quá thời gian cho phép") from exc
    except OSError as exc:
        raise MusicBedError(f"không chạy được ffmpeg cho nhạc nền: {exc}") from exc
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        raise MusicBedError(
            f"ffmpeg thất bại khi dựng nhạc nền (mã {result.returncode}): "
            f"{(result.stderr or '').strip()[-400:]}"
        )
    logger.info("gap music: %s đoạn nhạc chèn vào khoảng lặng của %s", len(pieces), audio_path)
    return str(out_path)
