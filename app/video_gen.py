"""Video generation: static image, Ken Burns animated, multi-segment concat, standalone."""
from __future__ import annotations

import logging
import random
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from app.config import settings
from app.models import Book, Patch
from app.subtitle_gen import ass_bgr

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, dict], None]

# Background files with these extensions are treated as looping video
# backgrounds instead of still images. Single source of truth shared with the
# upload/preview routes.
VIDEO_BACKGROUND_EXTENSIONS = {".mp4", ".webm", ".mov"}

# Every segment's audio is encoded to exactly this format. concat_segments
# stream-copies the audio, writing a single AudioSpecificConfig taken from the
# first segment, so a segment that disagrees on rate or layout yields a file
# that muxes and probes cleanly but fails to decode ("channel element N is not
# allocated"). Segment audio must therefore never be inherited from the input:
# greeting audio is a raw user upload and narration is TTS output whose rate
# depends on the engine. 48kHz stereo is YouTube's recommended upload format.
AUDIO_SAMPLE_RATE = 48000
AUDIO_CHANNELS = 2


def is_video_background(path: str | Path | None) -> bool:
    """True if the background path points to a loopable video (by extension)."""
    if not path:
        return False
    return Path(path).suffix.lower() in VIDEO_BACKGROUND_EXTENSIONS


def _emit(on_progress: ProgressCallback | None, event: str, **fields) -> None:
    if on_progress is not None:
        try:
            on_progress(event, fields)
        except Exception:
            pass


def _probe_audio_seconds(path: str) -> float | None:
    """Duration of an audio file in seconds, or None if it cannot be determined.

    Returning None rather than raising keeps generate_segment working on inputs
    ffprobe cannot read: the caller falls back to '-shortest' alone, which is the
    behaviour that shipped before the duration bound was added.
    """
    try:
        result = subprocess.run(
            [settings.get_ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
        seconds = float(result.stdout.strip())
    except (OSError, subprocess.SubprocessError, AttributeError, TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _resolve_fit_mode(mode: str, width: int, height: int) -> str:
    """Resolve 'auto' against the frame shape; pass any explicit mode through."""
    if mode != "auto":
        return mode
    return "blur" if height >= width else "contain"


def _build_fit_filter(width: int, height: int, mode: str = "contain") -> str:
    """Build the filter chain that fits an arbitrary source into width x height.

    'contain' letterboxes the source inside the frame (the original behaviour,
    and the only sensible choice while both source and target are 16:9).
    'cover' fills the frame and crops the overflow. 'blur' fills the frame with
    a blurred, cropped copy of the source and centres the untouched image on
    top - the standard treatment for portrait output, where the background
    library is landscape and 'contain' would leave two thirds of the frame
    black.

    'auto' resolves to 'blur' as soon as the frame stops being landscape, and
    to 'contain' otherwise. Resolving here rather than at the call sites keeps
    every caller free to pass the stored config value straight through.

    'blur' returns a multi-chain graph (chains separated by ';') with one
    implicit input and one implicit output. That composes both as a bare '-vf'
    argument and as the body of a labelled '[0:v]...[out]' filter_complex
    chain, so callers do not have to special-case it. Labels are prefixed with
    'fit' to stay clear of the waveform graph's labels.
    """
    mode = _resolve_fit_mode(mode, width, height)
    if mode == "cover":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height}"
        )
    if mode == "blur":
        return (
            "split=2[fitbg][fitfg];"
            f"[fitbg]scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},gblur=sigma=25[fitbgb];"
            f"[fitfg]scale={width}:{height}:force_original_aspect_ratio=decrease[fitfgs];"
            "[fitbgb][fitfgs]overlay=(W-w)/2:(H-h)/2"
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
    )


# Windows-only, matching the rest of this codebase's font handling (see
# image_overlay.py's _FONT_PATHS): the burned-in subtitle style is resolved
# by name through Windows' own DirectWrite font provider, which this ffmpeg
# build's libass uses automatically. fontsdir is kept as an explicit,
# verified-working fallback rather than relied on alone - see
# _build_subtitle_filter's docstring.
_SUBTITLE_FONTS_DIR = r"C:\Windows\Fonts"
_SUBTITLE_ALIGNMENT_BY_POSITION = {"top": 8, "center": 5, "bottom": 2}


def _escape_ffmpeg_filter_path(path) -> str:
    """Escape a filesystem path for use as a value inside an ffmpeg
    filtergraph option string (e.g. subtitles=filename=...:fontsdir=...).

    Backslashes become forward slashes (both are accepted on Windows) and the
    value is wrapped in single quotes so ',' and ':' inside it are read
    literally by ffmpeg's own filtergraph parser - except a drive-letter
    colon, which still needs its own backslash escape even inside single
    quotes, or libass's *separate* argument parser (one layer further in)
    truncates the value at the first colon. Confirmed against a real ffmpeg
    build (subprocess, not a shell) rather than assumed from memory - both
    layers have their own quoting rules and disagreeing with either silently
    breaks the filter instead of raising.
    """
    normalized = str(path).replace("\\", "/")
    escaped = normalized.replace(":", r"\:")
    return f"'{escaped}'"


def _build_subtitle_filter(subtitle_path: Path, config: dict) -> str | None:
    """Build the ffmpeg 'subtitles' filter for burning in captions, or None
    when subtitles are off or this segment's audio has no caption sidecar.

    A missing sidecar is a silent no-op, not an error: a voice-clip intro or
    outro is never TTS'd chunk by chunk (see subtitle_gen's module docstring)
    and so never gets one, and a patch synthesized before this feature
    shipped simply predates it. Either way the segment still renders, just
    without captions.

    Uses the 'subtitles' filter rather than 'ass': only 'subtitles' exposes
    force_style, which is what lets font size/colour/position come from
    whatever video_config is current *at render time* rather than being
    frozen into the .ass file when subtitle_gen wrote it at TTS time - so a
    pure style change never requires regenerating cues/timing, only
    whatever renders next.
    """
    if not config.get("subtitle_enabled") or not subtitle_path.is_file():
        return None
    font_size = config.get("subtitle_font_size", 46)
    color = ass_bgr(config.get("subtitle_color", "#ffffff"))
    alignment = _SUBTITLE_ALIGNMENT_BY_POSITION.get(config.get("subtitle_position", "bottom"), 2)
    force_style = f"Fontsize={font_size},PrimaryColour={color},Alignment={alignment}"
    filename_arg = _escape_ffmpeg_filter_path(subtitle_path)
    fontsdir_arg = _escape_ffmpeg_filter_path(_SUBTITLE_FONTS_DIR)
    return f"subtitles=filename={filename_arg}:fontsdir={fontsdir_arg}:force_style='{force_style}'"


def _build_zoompan_filter(image_type: str, width: int, height: int, fps: int, duration: float) -> str:
    """Build ffmpeg zoompan filter string for Ken Burns effects."""
    total_frames = int(duration * fps)
    if total_frames < 1:
        total_frames = 1

    if image_type == "zoom-in":
        return (
            f"zoompan=z='min(zoom+0.0015,1.5)':d={total_frames}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={width}x{height}:fps={fps}"
        )
    elif image_type == "zoom-out":
        return (
            f"zoompan=z='if(eq(on,1),1.5,max(zoom-0.0015,1.0))':d={total_frames}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":s={width}x{height}:fps={fps}"
        )
    elif image_type == "pan-left":
        return (
            f"zoompan=z='1.2':d={total_frames}"
            f":x='iw*0.2*(1-on/{total_frames})':y='ih/2-(ih/zoom/2)'"
            f":s={width}x{height}:fps={fps}"
        )
    elif image_type == "pan-right":
        return (
            f"zoompan=z='1.2':d={total_frames}"
            f":x='iw*0.2*(on/{total_frames})':y='ih/2-(ih/zoom/2)'"
            f":s={width}x{height}:fps={fps}"
        )
    else:
        return (
            f"zoompan=z='1':d={total_frames}:s={width}x{height}:fps={fps}"
        )


def _waveform_chains(config: dict, width: int, audio_input: int, video_label: str) -> tuple[list[str], str, str]:
    """Build a high-contrast waveform overlay and preserve narration for output mixing."""
    height = int(config.get("waveform_height", 120))
    style = config.get("waveform_style", "line")
    color = str(config.get("waveform_color", "#ffffff")).lstrip("#")
    opacity = float(config.get("waveform_opacity", 0.85))
    layout = config.get("waveform_layout", "horizontal")
    background = str(config.get("waveform_background_color", "#050816")).lstrip("#")
    background_opacity = float(config.get("waveform_background_opacity", 0.55))
    position = config.get("waveform_position", "bottom")
    overlay_width = height if layout in {"vertical", "circular"} else width
    x = "40" if layout == "vertical" else "(W-w)/2"
    y = {"top": "40", "center": "(H-h)/2", "bottom": "H-h-40"}.get(position, "H-h-40")
    panel_x = "0" if layout == "horizontal" else ("16" if layout == "vertical" else "(iw-%d)/2-24" % overlay_width)
    panel_y = {"top": "16", "center": "(ih-%d)/2-24" % height, "bottom": "ih-%d-64" % height}.get(position, "ih-%d-64" % height)
    panel_width = width if layout == "horizontal" else overlay_width + 48
    panel_height = height + 48
    if layout == "circular":
        red, green, blue = (int(color[index:index + 2], 16) for index in (0, 2, 4))
        visualizer = (
            f"avectorscope=s={height}x{height}:mode=polar:draw=aaline:scale=sqrt:"
            f"rc={red}:gc={green}:bc={blue}:rf=0:gf=0:bf=0,"
            f"colorkey=0x000000:0.12:0.08,format=rgba,colorchannelmixer=aa={opacity}"
        )
    else:
        visualizer = f"showwaves=s={width}x{height}:mode={style}:colors=0x{color}"
        if layout == "vertical":
            visualizer += ",transpose=clock,scale=%d:%d" % (overlay_width, height)
        visualizer += f",format=rgba,colorchannelmixer=aa={opacity}"
    chains = [
        f"[{audio_input}:a]asplit=2[narration][waveaudio]",
        f"{video_label}drawbox=x={panel_x}:y={panel_y}:w={panel_width}:h={panel_height}:color=0x{background}@{background_opacity}:t=fill[withpanel]",
        f"[waveaudio]{visualizer},split=2[wave][waveglowsrc]",
        "[waveglowsrc]gblur=sigma=8[waveglow]",
        f"[withpanel][waveglow]overlay=x={x}:y={y}:shortest=1[withglow]",
        f"[withglow][wave]overlay=x={x}:y={y}:shortest=1[vout]",
    ]
    return chains, "[vout]", "[narration]"


def generate_segment(
    image_path: str,
    audio_path: str,
    out_path: str,
    *,
    image_type: str = "none",
    resolution: tuple[int, int] = (1920, 1080),
    fps: int = 30,
    fit_mode: str = "contain",
    audio_bitrate: str = "320k",
    crf: int = 23,
    use_nvenc: bool = False,
    music_path: str | None = None,
    music_volume: float = 0.15,
    codec: str = "libx264",
    quality: int | None = None,
    marquee_path: str | None = None,
    marquee_meta: str | None = None,
    waveform_config: dict | None = None,
    progress_bar: bool = False,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Generate a single video segment from image + audio.

    image_path may be a still image OR a looping video background (see
    VIDEO_BACKGROUND_EXTENSIONS). For a video background the clip is stream-looped
    for the audio duration; image_type (Ken Burns) is ignored and the video's own
    audio track is dropped (only narration + optional music are kept).

    image_type: 'none' (static), 'zoom-in', 'zoom-out', 'pan-left', 'pan-right'
    fit_mode: 'contain' (letterbox), 'cover' (crop to fill), 'blur' (blurred
        backdrop + centred image). See _build_fit_filter.
    music_path: optional background music file (looped, mixed at music_volume ratio)

    on_progress: optional callback(event: str, fields: dict) for progress logging.
    Events: segment.start, segment.ffmpeg_start, segment.ffmpeg_done, segment.done,
            segment.failed.
    """
    if music_path is not None and not Path(music_path).exists():
        raise FileNotFoundError(f"music file not found: {music_path}")

    video_codec = "h264_nvenc" if use_nvenc or codec == "h264_nvenc" else "libx264"
    width, height = resolution
    is_video_bg = is_video_background(image_path)

    _emit(on_progress, "segment.start", path=out_path, image_type=image_type,
          resolution=f"{width}x{height}", fps=fps, codec=video_codec,
          video_background=is_video_bg)

    encode_quality = crf if quality is None else quality
    if use_nvenc or codec == "h264_nvenc":
        quality_args = ["-cq", str(encode_quality)]
        tune_args = []
    else:
        quality_args = ["-crf", str(encode_quality)]
        # '-tune stillimage' only helps genuine still frames; a video background
        # has real motion, so skip it there.
        tune_args = [] if is_video_bg else ["-tune", "stillimage"]

    if is_video_bg:
        # Loop the clip indefinitely; '-shortest' cuts it at the audio duration.
        inputs = [
            "-stream_loop", "-1", "-i", image_path,
            "-i", audio_path,
        ]
    else:
        inputs = [
            "-loop", "1", "-i", image_path,
            "-i", audio_path,
        ]
    next_idx = 2
    music_idx: int | None = None
    if music_path:
        inputs.extend(["-stream_loop", "-1", "-i", music_path])
        music_idx = next_idx
        next_idx += 1

    # Both input shapes are infinite ('-loop 1' on a still, '-stream_loop -1' on a
    # clip), so the output length comes from the narration. '-shortest' alone is
    # not enough once the audio is resampled to AUDIO_SAMPLE_RATE: the resampler's
    # latency delays the audio EOF and roughly 1.5s of extra video frames slip
    # through, leaving the video track longer than the audio. Bounding the output
    # with an explicit '-t' keeps the two tracks aligned.
    _emit(on_progress, "segment.probe_duration", path=out_path, audio=audio_path)
    narration_seconds = _probe_audio_seconds(audio_path)
    duration_args = ["-t", f"{narration_seconds:.6f}"] if narration_seconds else []

    if image_type == "none" or is_video_bg:
        # 'fps' must be pinned explicitly: '-loop 1' on a still image defaults to
        # 25fps and a video background inherits its own rate. Segments that
        # disagree get time-stretched by concat_segments' stream copy.
        base_vf = f"{_build_fit_filter(width, height, fit_mode)},fps={fps}"
    else:
        zp_filter = _build_zoompan_filter(
            image_type, width, height, fps, narration_seconds or 10.0
        )
        # zoompan's 's=' only sets the output size, it does not preserve the
        # source aspect, so a landscape still fed straight into a portrait frame
        # comes out stretched. Fitting first fixes that - but it also downscales
        # the source to the frame size before the zoom, which costs sharpness at
        # full zoom. Only pay that price when the caller actually asked to
        # reframe; 'contain' keeps the original full-resolution behaviour.
        resolved = _resolve_fit_mode(fit_mode, width, height)
        prefix = "" if resolved == "contain" else f"{_build_fit_filter(width, height, resolved)},"
        base_vf = f"{prefix}{zp_filter},format=yuv420p"
    if progress_bar and narration_seconds:
        base_vf += f",drawbox=x=0:y=ih-8:w='iw*t/{narration_seconds:.6f}':h=8:color=0xbaff39@0.9:t=fill"

    waveform = waveform_config if waveform_config and waveform_config.get("waveform_enabled") else None
    subtitle_filter = _build_subtitle_filter(Path(audio_path).with_suffix(".ass"), waveform_config or {})
    audio_chains: list[str] = []
    narration_label = "[1:a]"
    video_map_label = "[vout]"
    video_chains: list[str] = []
    if waveform:
        waveform_chains, video_map_label, narration_label = _waveform_chains(waveform, width, 1, "[vbase]")
        video_chains = [f"[0:v]{base_vf}[vbase]", *waveform_chains]
    if music_idx is not None:
        audio_chains.append(f"[{music_idx}:a]volume={music_volume}[music]")
        audio_chains.append(f"{narration_label}[music]amix=inputs=2:duration=first:normalize=0[aout]")
        audio_map_label = "[aout]"
    else:
        audio_map_label = narration_label

    if music_idx is not None or waveform:
        # Label the video chain: ffmpeg 5+ rejects mapping raw 0:v once it is
        # consumed by the complex filtergraph.
        chains = video_chains + audio_chains if waveform else audio_chains + [f"[0:v]{base_vf}[vout]"]
        if subtitle_filter:
            chains.append(f"{video_map_label}{subtitle_filter}[vsub]")
            video_map_label = "[vsub]"
        cmd = [
            settings.get_ffmpeg_path(), "-y",
            *inputs,
            "-filter_complex", ";".join(chains),
            "-map", video_map_label,
            "-map", audio_map_label,
            "-c:v", video_codec,
            *tune_args,
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-ar", str(AUDIO_SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS),
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            *quality_args,
            *duration_args,
            "-shortest",
            out_path,
        ]
    else:
        # A video background carries its own audio; map streams explicitly so the
        # narration (input 1) is kept and the background's audio is dropped. Still
        # images have no audio, so default stream selection is fine there.
        map_args = ["-map", "0:v", "-map", "1:a"] if is_video_bg else []
        final_vf = f"{base_vf},{subtitle_filter}" if subtitle_filter else base_vf
        cmd = [
            settings.get_ffmpeg_path(), "-y",
            *inputs,
            "-vf", final_vf,
            *map_args,
            "-c:v", video_codec,
            *tune_args,
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-ar", str(AUDIO_SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS),
            "-pix_fmt", "yuv420p",
            "-r", str(fps),
            *quality_args,
            *duration_args,
            "-shortest",
            out_path,
        ]

    _emit(on_progress, "segment.ffmpeg_start", path=out_path)
    t0 = time.monotonic()
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        stderr_tail = (exc.stderr or "")[-500:]
        _emit(on_progress, "segment.failed", path=out_path,
              returncode=exc.returncode, stderr_tail=stderr_tail)
        raise

    elapsed = time.monotonic() - t0
    out_size = Path(out_path).stat().st_size if Path(out_path).exists() else 0
    _emit(on_progress, "segment.ffmpeg_done", path=out_path,
          elapsed_seconds=round(elapsed, 2), size_bytes=out_size)
    _emit(on_progress, "segment.done", path=out_path)


def _probe_frame_rate(path: str) -> float:
    """Container-declared framerate (r_frame_rate) of a file's video stream."""
    result = subprocess.run(
        [settings.get_ffprobe_path(), "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=r_frame_rate",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    # An unreadable rate means "unknown", never a hard failure: the guard below
    # must not be able to break a concat that ffmpeg itself would accept.
    try:
        raw = result.stdout.strip()
        num, _, den = raw.partition("/")
        rate = float(num) / float(den or 1)
    except (AttributeError, TypeError, ValueError, ZeroDivisionError):
        return 0.0
    return rate if rate > 0 else 0.0


def _assert_uniform_frame_rate(segment_paths: list[str]) -> None:
    """Reject a stream-copy concat whose inputs disagree on framerate.

    The MP4 muxer applies the first input's frame duration to every packet, so a
    mismatch silently stretches part of the video track. The result uploads fine
    but YouTube cannot process it, which is far worse than failing here.
    """
    rates = [(p, _probe_frame_rate(p)) for p in segment_paths]
    known = [r for _, r in rates if r > 0]
    if not known:
        return
    baseline = known[0]
    offenders = [
        (p, r) for p, r in rates if r > 0 and abs(r - baseline) / baseline > 0.005
    ]
    if offenders:
        detail = ", ".join(f"{Path(p).name}={r:.3f}fps" for p, r in offenders)
        raise ValueError(
            f"refusing to concat segments with mixed framerate "
            f"(baseline {baseline:.3f}fps): {detail}"
        )


def _probe_audio_format(path: str) -> tuple[str, int, int] | None:
    """(codec, sample_rate, channels) of a file's first audio stream, or None."""
    try:
        result = subprocess.run(
            [settings.get_ffprobe_path(), "-v", "error", "-select_streams", "a:0",
             "-show_entries", "stream=codec_name,sample_rate,channels",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, check=True,
        )
        codec, rate, channels = [l for l in result.stdout.strip().splitlines() if l]
        return codec, int(rate), int(channels)
    except (OSError, subprocess.SubprocessError, AttributeError, TypeError, ValueError):
        # An unreadable format means "unknown", never a hard failure: the guard
        # below must not be able to break a concat that ffmpeg itself accepts.
        return None


def _assert_uniform_audio_format(segment_paths: list[str]) -> None:
    """Reject a stream-copy concat whose inputs disagree on audio format.

    The MP4 muxer writes one AudioSpecificConfig for the output track, taken from
    the first input. Packets from a segment encoded at another sample rate or
    channel count are copied in verbatim, so the file muxes and probes cleanly
    but any decoder configured from that first header hits invalid syntax at the
    boundary. The result uploads fine but YouTube cannot process it, which is far
    worse than failing here.
    """
    formats = [(p, _probe_audio_format(p)) for p in segment_paths]
    known = [(p, f) for p, f in formats if f is not None]
    if not known:
        return
    baseline = known[0][1]
    offenders = [(p, f) for p, f in known if f != baseline]
    if offenders:
        detail = ", ".join(
            f"{Path(p).name}={c}/{r}Hz/{ch}ch" for p, (c, r, ch) in offenders
        )
        c, r, ch = baseline
        raise ValueError(
            f"refusing to concat segments with mixed audio format "
            f"(baseline {c}/{r}Hz/{ch}ch): {detail}"
        )


def concat_segments(
    segment_paths: list[str],
    out_path: str,
    *,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Concat multiple segment videos into one using ffmpeg concat demuxer."""
    if not segment_paths:
        raise ValueError("No segments to concat")

    _emit(on_progress, "concat.start", count=len(segment_paths), path=out_path)

    if len(segment_paths) == 1:
        Path(out_path).write_bytes(Path(segment_paths[0]).read_bytes())
        _emit(on_progress, "concat.done", count=1, path=out_path, mode="copy_single")
        return

    _assert_uniform_frame_rate(segment_paths)
    _assert_uniform_audio_format(segment_paths)

    list_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    try:
        for p in segment_paths:
            safe = p.replace("\\", "/").replace("'", "'\\''")
            list_file.write(f"file '{safe}'\n")
        list_file.close()

        cmd = [
            settings.get_ffmpeg_path(), "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file.name,
            "-c", "copy",
            out_path,
        ]
        _emit(on_progress, "concat.ffmpeg_start", count=len(segment_paths))
        t0 = time.monotonic()
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr_tail = (exc.stderr or "")[-500:]
            _emit(on_progress, "concat.failed", returncode=exc.returncode,
                  stderr_tail=stderr_tail)
            raise
        elapsed = time.monotonic() - t0
        out_size = Path(out_path).stat().st_size if Path(out_path).exists() else 0
        _emit(on_progress, "concat.ffmpeg_done", count=len(segment_paths),
              elapsed_seconds=round(elapsed, 2), size_bytes=out_size)
    finally:
        Path(list_file.name).unlink(missing_ok=True)

    _emit(on_progress, "concat.done", count=len(segment_paths), path=out_path)


def concat_video_segments(segment_paths: list[str], out_path: str) -> None:
    """Concat pre-rendered video-only gameplay clips without audio assertions."""
    if not segment_paths:
        raise ValueError("No video segments to concat")
    _assert_uniform_frame_rate(segment_paths)
    list_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    try:
        for path in segment_paths:
            safe = path.replace("\\", "/").replace("'", "'\\''")
            list_file.write(f"file '{safe}'\n")
        list_file.close()
        subprocess.run([settings.get_ffmpeg_path(), "-y", "-f", "concat", "-safe", "0",
                        "-i", list_file.name, "-c", "copy", out_path],
                       check=True, capture_output=True, text=True)
    finally:
        Path(list_file.name).unlink(missing_ok=True)


def _probe_duration(path: str) -> float:
    result = subprocess.run(
        [settings.get_ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, check=True,
    )
    return max(0.01, float(result.stdout.strip()))


def generate_background_sequence(
    backgrounds: list[str], audio_path: str, out_path: str, *,
    resolution: tuple[int, int], fps: int, image_duration: float,
    fit_mode: str = "contain",
    mode: str = "sequential", seed: str = "", music_path: str | None = None,
    music_volume: float = 0.15, codec: str = "libx264", quality: int = 23,
    audio_bitrate: str = "192k", on_progress: ProgressCallback | None = None,
    start_index: int = 0,
    crossfade: bool = False, crossfade_seconds: float = 1,
    ken_burns: bool = False, progress_bar: bool = False,
    waveform_config: dict | None = None,
) -> None:
    """Render rotating silent backgrounds, then mux narration/music once."""
    duration = _probe_duration(audio_path)
    valid = [p for p in backgrounds if Path(p).is_file()]
    plan = plan_background_segments(valid, duration, image_duration, mode, seed=seed, start_index=start_index)
    if not plan:
        raise ValueError("No valid backgrounds for sequence")
    width, height = resolution
    video_codec = "h264_nvenc" if codec == "h264_nvenc" else "libx264"
    fade = 0.0
    if crossfade and len(plan) > 1:
        fade = min(float(crossfade_seconds), min(length for _, length in plan) / 2)
        # plan_background_segments lays the pieces end to end to cover the
        # narration, but every xfade overlaps its two pieces, so the crossfaded
        # visual comes out (n-1)*fade shorter than the audio and the final mux's
        # -shortest clips that much off the tail of the narration. Lengthen each
        # piece after the first to pay for the overlap it is about to lose.
        plan = [(background, length + fade if index else length)
                for index, (background, length) in enumerate(plan)]
    with tempfile.TemporaryDirectory(prefix="video_backgrounds_") as temp:
        pieces = []
        for index, (background, length) in enumerate(plan):
            piece = str(Path(temp) / f"piece_{index:04d}.mp4")
            if is_video_background(background):
                inputs = [settings.get_ffmpeg_path(), "-y", "-stream_loop", "-1", "-i", background]
            else:
                inputs = [settings.get_ffmpeg_path(), "-y", "-loop", "1", "-i", background]
            filters = _build_fit_filter(width, height, fit_mode)
            if ken_burns and not is_video_background(background):
                filters += f",zoompan=z='min(zoom+0.0005,1.15)':d={max(1, int(length * fps))}:s={width}x{height}:fps={fps}"
            if progress_bar:
                filters += f",drawbox=x=0:y=ih-8:w='iw*t/{max(length, 0.01)}':h=8:color=white@0.85:t=fill"
            cmd = inputs + [
                "-t", str(length), "-vf", filters,
                "-an", "-c:v", video_codec,
                "-pix_fmt", "yuv420p", "-r", str(fps),
                ("-cq" if video_codec == "h264_nvenc" else "-crf"), str(quality),
                piece,
            ]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            pieces.append(piece)
        visual = str(Path(temp) / "visual.mp4")
        if crossfade and len(pieces) > 1:
            graph = []
            for i in range(len(pieces)):
                graph.append(f"[{i}:v]settb=AVTB[v{i}]")
            current = "[v0]"
            elapsed = plan[0][1]
            for i in range(1, len(pieces)):
                out = f"[x{i}]"
                graph.append(f"{current}[v{i}]xfade=transition=fade:duration={fade}:offset={max(0, elapsed - fade)}{out}")
                current = out
                elapsed += plan[i][1] - fade
            # Windows caps a command line at 32767 characters, and both the xfade
            # graph and the -i list grow with the piece count: a long patch (one
            # piece per image_duration seconds) overflowed the cap and died in
            # CreateProcess with WinError 206 before ffmpeg ever started. Keep the
            # graph in a script file and pass the pieces as bare filenames
            # relative to their own directory.
            script = Path(temp) / "xfade.txt"
            script.write_text(";".join(graph), encoding="utf-8")
            cmd = [settings.get_ffmpeg_path(), "-y"]
            for piece in pieces:
                cmd += ["-i", Path(piece).name]
            cmd += ["-filter_complex_script", script.name, "-map", current, "-an", "-c:v", video_codec,
                    "-pix_fmt", "yuv420p", "-r", str(fps),
                    ("-cq" if video_codec == "h264_nvenc" else "-crf"), str(quality),
                    Path(visual).name]
            subprocess.run(cmd, check=True, capture_output=True, text=True, cwd=temp)
        else:
            concat_segments(pieces, visual)
        inputs = [settings.get_ffmpeg_path(), "-y", "-i", visual, "-i", audio_path]
        chains = []
        audio_map = "1:a"
        video_map = "0:v:0"
        waveform = waveform_config if waveform_config and waveform_config.get("waveform_enabled") else None
        if waveform:
            waveform_chains, video_map, audio_map = _waveform_chains(waveform, width, 1, "[0:v]")
            chains.extend(waveform_chains)
        subtitle_filter = _build_subtitle_filter(Path(audio_path).with_suffix(".ass"), waveform_config or {})
        if subtitle_filter:
            # video_map may still be the raw stream selector "0:v:0" here (no
            # waveform ran) rather than a filtergraph label - filter_complex
            # needs a bracketed input, so wrap it the first time any video
            # filter becomes necessary.
            video_in = video_map if video_map.startswith("[") else f"[{video_map}]"
            chains.append(f"{video_in}{subtitle_filter}[vsub]")
            video_map = "[vsub]"
        if music_path:
            inputs += ["-stream_loop", "-1", "-i", music_path]
            chains.extend(["[2:a]volume=" + str(music_volume) + "[music]", f"{audio_map}[music]amix=inputs=2:duration=first:normalize=0[aout]"])
            audio_map = "[aout]"
        cmd = inputs
        if chains:
            cmd += ["-filter_complex", ";".join(chains)]
        # A subtitle burn (like the waveform overlay) rewrites pixels, so it
        # forces a re-encode; the fast stream-copy path is only valid when
        # neither is active and the concatenated 'visual' can pass through
        # untouched.
        reencode_video = bool(waveform or subtitle_filter)
        cmd += ["-map", video_map, "-map", audio_map, "-c:v", video_codec if reencode_video else "copy"]
        if reencode_video:
            cmd += ["-pix_fmt", "yuv420p", "-r", str(fps),
                    ("-cq" if video_codec == "h264_nvenc" else "-crf"), str(quality)]
        cmd += ["-c:a", "aac", "-b:a", audio_bitrate,
                "-ar", str(AUDIO_SAMPLE_RATE), "-ac", str(AUDIO_CHANNELS),
                "-shortest", out_path]
        subprocess.run(cmd, check=True, capture_output=True, text=True)


def resolve_patch_image(patch: Patch, book: Book | None, default_image: str) -> str | None:
    """Resolve the image for a patch: patch.image_path -> book.background_image_path -> default."""
    if patch.image_path and Path(patch.image_path).exists():
        return patch.image_path
    if book and book.background_image_path and Path(book.background_image_path).exists():
        return book.background_image_path
    if Path(default_image).exists():
        return default_image
    return None


def resolve_configured_patch_image(patch, config: dict | None, fallback: str) -> str | None:
    """Choose one valid shared background for a patch, honoring patch override."""
    if getattr(patch, "image_path", None) and Path(patch.image_path).exists():
        return patch.image_path
    config = config or {}
    candidates = [p for p in config.get("backgrounds", []) if isinstance(p, str) and Path(p).exists()]
    if candidates:
        if config.get("background_mode") == "random":
            rng = random.Random(f"{fallback}:{getattr(patch, 'id', 0)}")
            return rng.choice(candidates)
        return candidates[getattr(patch, "patch_index", 0) % len(candidates)]
    return fallback if fallback and Path(fallback).exists() else None


def plan_background_segments(backgrounds: list[str], duration: float, image_duration: float, mode: str = "sequential", *, seed: str = "", start_index: int = 0) -> list[tuple[str, float]]:
    """Return timed background segments whose total duration equals duration."""
    if duration <= 0 or image_duration <= 0 or not backgrounds:
        return []
    ordered = list(backgrounds)
    if mode == "random":
        random.Random(seed).shuffle(ordered)
    result = []
    remaining = float(duration)
    index = start_index
    while remaining > 0:
        length = min(float(image_duration), remaining)
        result.append((ordered[index % len(ordered)], length))
        remaining -= length
        index += 1
    return result


def generate_full_video(
    patches: list[Patch],
    book: Book,
    out_path: str,
    *,
    default_image: str,
    use_nvenc: bool = False,
    music_path: str | None = None,
    music_volume: float = 0.15,
    codec: str = "libx264",
    quality: int = 23,
    audio_bitrate: str = "320k",
    video_config: dict | None = None,
    intro_audio: str | None = None,
    outro_audio: str | None = None,
    font_path: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Generate a full video by creating segments per patch and concatenating.

    music_path: optional background music file looped at music_volume ratio.
    font_path: passed to image_overlay.ensure_patch_overlay() for text rendering.

    on_progress: optional callback(event, fields) for progress logging.
    Events: video.start, video.segment_skipped, video.segments_done, video.done, video.failed.
    """
    from app import image_overlay

    w, h = (book.video_resolution or "1920x1080").split("x")
    resolution = (int(w), int(h))
    fps = book.video_fps or 30
    default_anim = book.default_image_animation or "none"
    fit_mode = (video_config or {}).get("fit_mode") or "auto"

    eligible = [p for p in patches if p.audio_path]
    _emit(on_progress, "video.start", path=out_path, total_patches=len(patches),
          eligible_patches=len(eligible), resolution=f"{w}x{h}", fps=fps,
          codec="h264_nvenc" if use_nvenc else "libx264")

    segment_paths: list[str] = []
    tmp_dir = Path(out_path).parent / "_segments"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        for i, patch in enumerate(patches):
            if not patch.audio_path:
                _emit(on_progress, "video.segment_skipped",
                      patch_index=patch.patch_index, reason="no_audio")
                continue

            seg_path = str(tmp_dir / f"seg_{i:04d}.mp4")

            def _seg_progress(event: str, fields: dict, _p=patch) -> None:
                _emit(on_progress, event, patch_index=_p.patch_index,
                          patch_id=_p.id, **{k: v for k, v in fields.items() if k != "path"})

            shared = [p for p in (video_config or {}).get("backgrounds", []) if isinstance(p, str)]
            raw_for_greeting = resolve_configured_patch_image(patch, video_config, resolve_patch_image(patch, book, default_image) or default_image)
            greeting_paths = []
            if raw_for_greeting and intro_audio:
                intro_path = str(tmp_dir / f"intro_{i:04d}.mp4")
                generate_segment(raw_for_greeting, intro_audio, intro_path, image_type="none", resolution=resolution, fps=fps, fit_mode=fit_mode, codec=codec, quality=quality, audio_bitrate=audio_bitrate)
                greeting_paths.append(intro_path)
            if len(shared) > 1 and not (getattr(patch, "image_path", None) and Path(patch.image_path).exists()):
                generate_background_sequence(
                    shared, patch.audio_path, seg_path, resolution=resolution, fps=fps,
                    fit_mode=fit_mode,
                    image_duration=float((video_config or {}).get("image_duration_seconds", 15)),
                    mode=(video_config or {}).get("background_mode", "sequential"),
                    seed=f"{getattr(book, 'id', '')}-{patch.id}", music_path=music_path,
                    music_volume=music_volume, codec=codec, quality=quality,
                    audio_bitrate=audio_bitrate, on_progress=_seg_progress,
                    start_index=patch.patch_index,
                    crossfade=bool((video_config or {}).get("crossfade_enabled")),
                    crossfade_seconds=float((video_config or {}).get("crossfade_seconds", 1)),
                    ken_burns=bool((video_config or {}).get("ken_burns_enabled")),
                    progress_bar=bool((video_config or {}).get("progress_bar_enabled")),
                    waveform_config=video_config,
                )
                if raw_for_greeting and outro_audio:
                    outro_path = str(tmp_dir / f"outro_{i:04d}.mp4")
                    generate_segment(raw_for_greeting, outro_audio, outro_path, image_type="none", resolution=resolution, fps=fps, fit_mode=fit_mode, codec=codec, quality=quality, audio_bitrate=audio_bitrate)
                    greeting_paths.append(outro_path)
                segment_paths.extend(greeting_paths[:1])
                segment_paths.append(seg_path)
                if len(greeting_paths) > 1:
                    segment_paths.append(greeting_paths[-1])
                continue

            raw_bg = resolve_configured_patch_image(patch, video_config, resolve_patch_image(patch, book, default_image) or default_image)
            if not raw_bg:
                _emit(on_progress, "video.segment_skipped",
                      patch_index=patch.patch_index, reason="no_image")
                continue

            if is_video_background(raw_bg):
                image = raw_bg
                anim = "none"
            else:
                overlay = image_overlay.ensure_patch_overlay(book, patch, font_path, background_path=raw_bg)
                image = overlay or raw_bg
                anim = patch.image_type if patch.image_type and patch.image_type != "static" else default_anim

            generate_segment(
                image, patch.audio_path, seg_path,
                image_type=anim,
                resolution=resolution,
                fps=fps,
                fit_mode=fit_mode,
                use_nvenc=use_nvenc,
                music_path=music_path,
                music_volume=music_volume,
                codec=codec,
                quality=quality,
                audio_bitrate=audio_bitrate,
                waveform_config=video_config,
                on_progress=_seg_progress,
            )
            segment_paths.extend(greeting_paths[:1])
            segment_paths.append(seg_path)
            if raw_for_greeting and outro_audio:
                outro_path = str(tmp_dir / f"outro_{i:04d}.mp4")
                generate_segment(raw_for_greeting, outro_audio, outro_path, image_type="none", resolution=resolution, fps=fps, fit_mode=fit_mode, codec=codec, quality=quality, audio_bitrate=audio_bitrate)
                segment_paths.append(outro_path)
            _emit(on_progress, "video.segment_done",
                  patch_index=patch.patch_index, patch_id=patch.id,
                  segment_index=len(segment_paths),
                  progress=f"{len(segment_paths)}/{len(eligible)}")

        if not segment_paths:
            _emit(on_progress, "video.failed", reason="no_segments")
            raise ValueError("No segments were generated")

        _emit(on_progress, "video.segments_done", count=len(segment_paths))
        concat_segments(segment_paths, out_path, on_progress=on_progress)

        out_size = Path(out_path).stat().st_size if Path(out_path).exists() else 0
        _emit(on_progress, "video.done", path=out_path, size_bytes=out_size)
    except Exception as exc:
        _emit(on_progress, "video.failed", error=str(exc))
        raise
    finally:
        for p in segment_paths:
            Path(p).unlink(missing_ok=True)
        if tmp_dir.exists():
            try:
                tmp_dir.rmdir()
            except OSError:
                pass


def generate_standalone_video(
    audio_path: str,
    image_path: str,
    out_path: str,
    *,
    resolution: str = "1920x1080",
    fps: int = 30,
    fit_mode: str = "auto",
    codec: str = "libx264",
    audio_bitrate: str = "320k",
    image_type: str = "none",
    crf: int = 23,
    music_path: str | None = None,
    music_volume: float = 0.15,
    intro_audio: str | None = None,
    outro_audio: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Generate a standalone video from a single audio + image (Video Creator page)."""
    w, h = resolution.split("x")
    res = (int(w), int(h))
    use_nvenc = codec == "h264_nvenc"
    if not intro_audio and not outro_audio:
        generate_segment(
            image_path, audio_path, out_path, image_type=image_type, resolution=res,
            fps=fps, fit_mode=fit_mode, audio_bitrate=audio_bitrate, crf=crf, use_nvenc=use_nvenc,
            music_path=music_path, music_volume=music_volume, on_progress=on_progress,
        )
        return

    with tempfile.TemporaryDirectory(prefix="video_greeting_") as tmp:
        segments: list[str] = []
        if intro_audio:
            intro_path = str(Path(tmp) / "intro.mp4")
            generate_segment(image_path, intro_audio, intro_path, resolution=res, fps=fps,
                             fit_mode=fit_mode, audio_bitrate=audio_bitrate, crf=crf,
                             use_nvenc=use_nvenc)
            segments.append(intro_path)
        main_path = str(Path(tmp) / "main.mp4")
        generate_segment(
            image_path, audio_path, main_path, image_type=image_type, resolution=res,
            fps=fps, fit_mode=fit_mode, audio_bitrate=audio_bitrate, crf=crf, use_nvenc=use_nvenc,
            music_path=music_path, music_volume=music_volume, on_progress=on_progress,
        )
        segments.append(main_path)
        if outro_audio:
            outro_path = str(Path(tmp) / "outro.mp4")
            generate_segment(image_path, outro_audio, outro_path, resolution=res, fps=fps,
                             fit_mode=fit_mode, audio_bitrate=audio_bitrate, crf=crf,
                             use_nvenc=use_nvenc)
            segments.append(outro_path)
        concat_segments(segments, out_path, on_progress=on_progress)
