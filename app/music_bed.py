"""Background music placed under the last seconds of every chapter.

The old mix looped one track under the whole narration at a fixed volume. That
fights the voice for an hour-long chapter, so music now plays only as a short
*bed* under the closing ``chapter_end_seconds`` of each chapter and fades out on
the chapter boundary - a cue that the chapter is wrapping up, not a soundtrack.

Chapter boundaries come from the patch audio's ``.timeline.json`` sidecar (the
same one YouTube chapter markers use). Audio without a valid sidecar - a Text
Studio edit, a standalone upload - is treated as one chapter, so the music lands
under its last seconds.

The bed is rendered once per narration file, ahead of the video mux, into a
plain WAV no longer than the narration. Everything downstream keeps treating it
as "the music file": the same ``music_volume`` slider, the same amix, no special
casing in the render paths beyond not looping it. The music sits *under* the
voice, so the video, its subtitles and its chapter timestamps keep their length.
"""
from __future__ import annotations

import logging
import random
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_RENDER_TIMEOUT = 900

# Bed format. Matched to video_gen.AUDIO_SAMPLE_RATE/AUDIO_CHANNELS so the mux
# does not resample it a second time.
BED_SAMPLE_RATE = 48000
BED_CHANNELS = 2

# Defaults, mirrored by video_config.VIDEO_DEFAULTS (the settings UI) - a caller
# that passes an empty config still gets a sane bed.
DEFAULT_CHAPTER_END_SECONDS = 15
MIN_CHAPTER_END_SECONDS = 1
MAX_CHAPTER_END_SECONDS = 300
# Fade length at each edge of a piece when fading is on; halved for a piece
# too short to hold two full fades.
FADE_SECONDS = 2.0
# Below this a placed piece is a click, not a cue - such a chapter is skipped.
MIN_PIECE_SECONDS = 0.25
# One ffmpeg input per chapter; a runaway timeline must not build a
# 5000-input filtergraph.
MAX_PIECES = 240


class MusicBedError(RuntimeError):
    """ffmpeg refused to render the bed."""


@dataclass(frozen=True)
class BedOptions:
    """Validated chapter-end music settings for one render."""

    chapter_end_seconds: int = DEFAULT_CHAPTER_END_SECONDS
    random_start: bool = False
    fade: bool = True


@dataclass(frozen=True)
class Piece:
    """One music placement: ``length`` seconds of the track, read from
    ``offset`` into it, laid at ``start`` seconds into the narration."""

    start: float
    length: float
    offset: float = 0.0


def _int(raw, fallback: int, low: int, high: int) -> int:
    if isinstance(raw, bool):
        return fallback
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, value))


def is_enabled(config: dict | None) -> bool:
    """Chapter-end placement applies whenever a render passes a music config.

    ``None`` is the standalone Video Creator, which has no book settings and
    keeps looping its music under the whole narration.
    """
    return isinstance(config, dict)


def parse_options(config: dict | None) -> BedOptions:
    """Turn a render-config dict into ``BedOptions``.

    Reads the keys of the video config (``music_chapter_end_seconds`` /
    ``music_random_start`` / ``music_fade_enabled``), so a caller can hand in
    the whole video config or the render-config snapshot. Snapshots frozen
    before this mode shipped carry none of them and get the defaults.
    """
    if not isinstance(config, dict):
        return BedOptions()
    return BedOptions(
        chapter_end_seconds=_int(config.get("music_chapter_end_seconds"), DEFAULT_CHAPTER_END_SECONDS,
                                 MIN_CHAPTER_END_SECONDS, MAX_CHAPTER_END_SECONDS),
        random_start=bool(config.get("music_random_start", False)),
        fade=bool(config.get("music_fade_enabled", True)),
    )


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


def chapter_spans(audio_path: str | Path, total_duration: float) -> list[tuple[float, float]]:
    """``(start, end)`` seconds of every chapter in the narration.

    A chapter runs from its first spoken frame to the next chapter's first
    spoken frame, so its end includes the chapter pause - the music fades out
    as the next chapter starts speaking. Falls back to one chapter covering the
    whole file when the timeline sidecar is missing or does not match the WAV.
    """
    from app.youtube_metadata import load_timeline

    timeline = load_timeline(audio_path)
    starts = [float(chapter["start_seconds"]) for chapter in (timeline or {}).get("chapters", [])]
    if not starts:
        return [(0.0, total_duration)]
    ends = starts[1:] + [total_duration]
    return [(start, end) for start, end in zip(starts, ends) if end > start]


def plan_pieces(
    chapters: list[tuple[float, float]], options: BedOptions, *,
    music_duration: float | None = None, seed: str = "",
) -> list[Piece]:
    """One piece under the closing ``chapter_end_seconds`` of each chapter.

    A chapter shorter than the window gets music for its whole length. With
    ``random_start`` each piece reads from a random point in the track instead
    of its opening bars; the RNG is seeded per chapter so a retried render
    lays the same music in the same places.
    """
    pieces: list[Piece] = []
    for index, (start, end) in enumerate(chapters):
        piece_start = max(start, end - options.chapter_end_seconds)
        length = end - piece_start
        if length < MIN_PIECE_SECONDS:
            continue
        offset = 0.0
        if options.random_start and music_duration and music_duration > length:
            offset = random.Random(f"{seed}:{index}").uniform(0.0, music_duration - length)
        pieces.append(Piece(start=round(piece_start, 3), length=round(length, 3), offset=round(offset, 3)))
        if len(pieces) >= MAX_PIECES:
            logger.warning("chapter music: capping at %s pieces", MAX_PIECES)
            break
    return pieces


def build_filter_graph(pieces: list[Piece], options: BedOptions) -> str:
    """The filter_complex graph placing one music copy per chapter end.

    Input ``i`` is the music file opened for piece ``i`` (see
    :func:`build_bed_command`); it is trimmed to the piece, optionally faded at
    both edges and delayed to the piece's start. ``amix`` with ``normalize=0``
    sums them without touching levels - the pieces never overlap, so summing
    is exact.
    """
    if not pieces:
        raise ValueError("no music pieces to render")
    chains: list[str] = []
    for index, piece in enumerate(pieces):
        label = f"[b{index}]" if len(pieces) > 1 else "[bed]"
        steps = [f"atrim={piece.offset:.3f}:{piece.offset + piece.length:.3f}", "asetpts=N/SR/TB"]
        fade = min(FADE_SECONDS, piece.length / 2) if options.fade else 0.0
        if fade > 0:
            steps.append(f"afade=t=in:st=0:d={fade:.3f}")
            steps.append(f"afade=t=out:st={max(0.0, piece.length - fade):.3f}:d={fade:.3f}")
        delay_ms = int(round(piece.start * 1000))
        if delay_ms > 0:
            steps.append(f"adelay={delay_ms}:all=1")
        chains.append(f"[{index}:a]" + ",".join(steps) + label)
    if len(pieces) > 1:
        inputs = "".join(f"[b{index}]" for index in range(len(pieces)))
        chains.append(f"{inputs}amix=inputs={len(pieces)}:normalize=0:dropout_transition=0[bed]")
    return ";".join(chains)


def build_bed_command(music_path: str | Path, out_path: str | Path, pieces: list[Piece],
                      script_path: str | Path) -> list[str]:
    """ffmpeg argv rendering the bed (exposed for tests).

    The music is opened once per piece instead of split from a single input:
    each piece then owns its own decoder, and no branch has to buffer while
    another one drains. ``-stream_loop -1`` lets a piece longer than the track
    (a 15s window over a 10s sting) keep playing instead of cutting to silence.
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


def build_chapter_bed(
    audio_path: str | Path, music_path: str | Path, out_path: str | Path, config: dict | None
) -> str | None:
    """Render the chapter-end music bed for one narration file.

    Returns the bed path, or ``None`` when there is nothing to place (an empty
    or unreadable narration - the caller then renders without music). Only a
    broken ffmpeg render raises ``MusicBedError``.
    """
    if not is_enabled(config):
        return None
    options = parse_options(config)
    total = probe_duration(audio_path)
    if not total or total <= 0:
        logger.info("chapter music: cannot read duration of %s, rendering without music", audio_path)
        return None
    pieces = plan_pieces(
        chapter_spans(audio_path, total), options,
        music_duration=probe_duration(music_path), seed=str(audio_path),
    )
    if not pieces:
        return None

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # The graph grows with the chapter count and Windows caps a command line at
    # 32767 characters, so it goes to a script file (same reason video_gen's
    # xfade graph does).
    script_path = out_path.with_suffix(".graph.txt")
    script_path.write_text(build_filter_graph(pieces, options), encoding="utf-8")
    try:
        result = subprocess.run(
            build_bed_command(music_path, out_path, pieces, script_path),
            capture_output=True, text=True, timeout=_RENDER_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise MusicBedError("render nhạc nền cuối chương quá thời gian cho phép") from exc
    except OSError as exc:
        raise MusicBedError(f"không chạy được ffmpeg cho nhạc nền: {exc}") from exc
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0 or not out_path.is_file() or out_path.stat().st_size == 0:
        raise MusicBedError(
            f"ffmpeg thất bại khi dựng nhạc nền (mã {result.returncode}): "
            f"{(result.stderr or '').strip()[-400:]}"
        )
    logger.info("chapter music: %s đoạn nhạc cuối chương cho %s", len(pieces), audio_path)
    return str(out_path)
