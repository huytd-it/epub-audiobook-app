"""Burned-in caption generation from the TTS chunk plan - no speech
recognition involved.

Every TTS engine synthesizes patch narration chunk by chunk (see
app.jobqueue.handlers.audiobook_tts.synthesize_patch): the exact text of each
chunk and the exact frame count of its audio are both known with certainty at
synthesis time - the same ground truth app.audio_merge.build_chapter_marks
already uses for the chapter-timeline sidecar. Reusing that ground truth for
captions means the chunk-level timing is always exactly right: there is no
transcription step, and so no risk of a speech recognizer mis-hearing a word
and drawing a caption that doesn't match what was actually said.

A TTS chunk (up to ~400 characters, tens of seconds) is far too long for a
single caption card, so within each chunk this splits the text into
caption-sized cues with app.chunker.split_into_tts_chunks (the same
sentence-respecting packer used to build TTS input, just called with a much
smaller max_chars) and spreads the chunk's known [start, end) window across
those cues in proportion to each cue's character count. That is only an
approximation *within* a chunk - a longer clause is not necessarily spoken
proportionally longer - but the error is bounded by a single chunk's
duration (a few seconds at most), and the use case (readable video captions,
not frame-accurate karaoke) does not need better than that.
"""
from __future__ import annotations

import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app import audio_merge
from app.chunker import split_into_tts_chunks

logger = logging.getLogger(__name__)

DEFAULT_MAX_CHARS_PER_CUE = 80

# Fixed authoring resolution for every .ass sidecar. libass scales font size,
# outline and shadow to whatever the actual output frame is at burn time
# (ScaledBorderAndShadow: yes in the header below), so this does not need to
# match the book's video_resolution - and deliberately doesn't, since a
# caption file is written once at TTS time but may get burned into renders
# at several different resolutions over the book's lifetime (video_config.py's
# resolution can change independently of already-synthesized audio).
PLAY_RES = (1920, 1080)

DEFAULT_FONT_NAME = "Segoe UI"
DEFAULT_FONT_SIZE = 46
DEFAULT_COLOR = "#FFFFFF"
DEFAULT_POSITION = "bottom"

_ALIGNMENT_BY_POSITION = {"top": 8, "center": 5, "bottom": 2}


@dataclass(frozen=True)
class Cue:
    text: str
    start_seconds: float
    end_seconds: float


def build_cues(
    plan: list[dict], frame_counts: list[int], sample_rate: int, pause_ms,
    *, max_chars_per_cue: int = DEFAULT_MAX_CHARS_PER_CUE,
) -> list[Cue]:
    """Chunk-exact cue boundaries, sub-split for on-screen readability.

    Mirrors app.audio_merge.build_chapter_marks' layout math exactly (the gap
    from pause_ms before every chunk but the first) so the same frame_counts
    already computed for the chapter-timeline sidecar can be reused here
    unchanged - but at every chunk, not just chapter starts. ``pause_ms`` takes
    the same uniform-or-per-chunk shapes audio_merge accepts, so captions stay
    aligned once chapter breaks get their own longer pause.
    """
    if len(plan) != len(frame_counts):
        raise ValueError("plan and frame_counts must be the same length")
    if sample_rate <= 0:
        raise ValueError("sample_rate must be positive")
    pauses = audio_merge.resolve_pauses(pause_ms, len(plan))
    cues: list[Cue] = []
    cursor = 0.0
    for index, (item, frames) in enumerate(zip(plan, frame_counts)):
        cursor += pauses[index] / 1000
        chunk_start = cursor
        chunk_duration = frames / sample_rate
        cursor += chunk_duration
        text = (item.get("text") or "").strip()
        if not text:
            continue
        pieces = split_into_tts_chunks(text, max_chars=max_chars_per_cue)
        if not pieces:
            continue
        total_chars = sum(len(piece) for piece in pieces) or 1
        piece_cursor = chunk_start
        for i, piece in enumerate(pieces):
            # The last piece snaps to the chunk's true end (== cursor) rather
            # than the accumulated start + duration*share: floating-point
            # drift across several proportional shares would otherwise leave
            # this cue's end a few microseconds short of - or past - the
            # exact chunk boundary that build_chapter_marks' own arithmetic
            # produces, and callers rely on that boundary being exact.
            piece_end = cursor if i == len(pieces) - 1 else piece_cursor + chunk_duration * (len(piece) / total_chars)
            cues.append(Cue(piece, piece_cursor, piece_end))
            piece_cursor = piece_end
    return cues


def _ass_timestamp(seconds: float) -> str:
    """H:MM:SS.CC - centisecond precision, ASS's native unit."""
    total_centis = round(max(0.0, seconds) * 100)
    centis = total_centis % 100
    total_secs = total_centis // 100
    secs = total_secs % 60
    total_mins = total_secs // 60
    mins = total_mins % 60
    hours = total_mins // 60
    return f"{hours}:{mins:02d}:{secs:02d}.{centis:02d}"


def _escape_ass_text(text: str) -> str:
    # \N is libass's hard line break. A cue is never expected to contain a
    # literal newline (cues come from chunker's sentence packer), but any
    # stray one is escaped rather than left to split into an unintended
    # second dialogue line.
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", "\\N")


def ass_bgr(hex_color: str) -> str:
    """'#RRGGBB' -> ASS's '&H00BBGGRR' (BGR channel order, 00 = fully opaque)."""
    hex_color = hex_color.lstrip("#")
    r, g, b = hex_color[0:2], hex_color[2:4], hex_color[4:6]
    return f"&H00{b}{g}{r}".upper()


_HEADER_TEMPLATE = """[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font_name},{font_size},{color},&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,3,0,{alignment},20,20,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def render_ass(
    cues: list[Cue], *, font_name: str = DEFAULT_FONT_NAME, font_size: int = DEFAULT_FONT_SIZE,
    color: str = DEFAULT_COLOR, position: str = DEFAULT_POSITION,
) -> str:
    """Render cues to ASS document text.

    The baked-in style here is only a fallback for viewing the file outside
    the app: video_gen.py's burn step overrides font size/colour/position at
    render time via the ffmpeg 'subtitles' filter's force_style, straight
    from whatever video_config is current then - so a style change alone
    never requires rebuilding cues/timing, only whatever renders next.
    """
    width, height = PLAY_RES
    margin_v = round(height * 0.06)
    header = _HEADER_TEMPLATE.format(
        width=width, height=height, font_name=font_name, font_size=font_size,
        color=ass_bgr(color), alignment=_ALIGNMENT_BY_POSITION.get(position, 2),
        margin_v=margin_v,
    )
    lines = [header]
    for cue in cues:
        if cue.end_seconds <= cue.start_seconds:
            continue
        lines.append(
            f"Dialogue: 0,{_ass_timestamp(cue.start_seconds)},{_ass_timestamp(cue.end_seconds)},"
            f"Caption,,0,0,0,,{_escape_ass_text(cue.text)}\n"
        )
    return "".join(lines)


def write_ass(path, cues: list[Cue], **style_kwargs) -> None:
    """Atomic write. A half-written .ass either fails to parse (safe) or, if
    the truncation happens to land on a line boundary, silently drops the
    tail of the captions - worth the temp-file + os.replace() to rule out,
    the same way audio_merge.write_timeline treats its own sidecar."""
    path = Path(path)
    content = render_ass(cues, **style_kwargs)
    fd, temp_path = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp:
            temp.write(content)
            temp.flush()
            os.fsync(temp.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            Path(temp_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def try_write_ass(path, cues: list[Cue], **style_kwargs) -> None:
    """Best-effort write_ass - a missing subtitle sidecar only costs
    captions on that patch, which must never fail an otherwise-good TTS run
    (mirrors audio_merge.try_write_timeline)."""
    try:
        write_ass(path, cues, **style_kwargs)
    except Exception:
        logger.warning("failed to write subtitle sidecar %s", path, exc_info=True)


def try_generate(
    path, plan: list[dict], frame_counts: list[int], sample_rate: int, pause_ms,
    *, max_chars_per_cue: int = DEFAULT_MAX_CHARS_PER_CUE, **style_kwargs,
) -> None:
    """build_cues + write_ass in one best-effort call.

    The intended call site (synthesize_patch) already treats the whole
    subtitle sidecar as optional the same way it treats the chapter-timeline
    sidecar - but build_cues can itself raise (e.g. a caller-side length
    mismatch), and that exception has to be caught here too, not just around
    the write: a caption bug must never take down an otherwise-successful TTS
    run. try_write_ass alone only guards the write half.
    """
    try:
        cues = build_cues(plan, frame_counts, sample_rate, pause_ms, max_chars_per_cue=max_chars_per_cue)
        write_ass(path, cues, **style_kwargs)
    except Exception:
        logger.warning("failed to generate subtitle sidecar %s", path, exc_info=True)
