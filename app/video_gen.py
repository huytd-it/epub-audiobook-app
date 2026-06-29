"""Video generation: static image, Ken Burns animated, multi-segment concat, standalone."""
from __future__ import annotations

import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from app.ffmpeg import get_ffmpeg_path, get_ffprobe_path
from app.models import Book, Patch

ProgressCallback = Callable[[str, dict], None]


def _emit(on_progress: ProgressCallback | None, event: str, **fields) -> None:
    if on_progress is not None:
        try:
            on_progress(event, fields)
        except Exception:
            pass


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


def generate_segment(
    image_path: str,
    audio_path: str,
    out_path: str,
    *,
    image_type: str = "none",
    resolution: tuple[int, int] = (1920, 1080),
    fps: int = 30,
    audio_bitrate: str = "192k",
    crf: int = 23,
    use_nvenc: bool = False,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Generate a single video segment from image + audio.

    image_type: 'none' (static), 'zoom-in', 'zoom-out', 'pan-left', 'pan-right'

    on_progress: optional callback(event: str, fields: dict) for progress logging.
    Events: segment.start, segment.ffmpeg_start, segment.ffmpeg_done, segment.done,
            segment.failed.
    """
    video_codec = "h264_nvenc" if use_nvenc else "libx264"
    width, height = resolution

    _emit(on_progress, "segment.start", path=out_path, image_type=image_type,
          resolution=f"{width}x{height}", fps=fps, codec=video_codec)

    if use_nvenc:
        quality_args = ["-cq", str(crf)]
        tune_args = []
    else:
        quality_args = ["-crf", str(crf)]
        tune_args = ["-tune", "stillimage"]

    if image_type == "none":
        cmd = [
            get_ffmpeg_path(), "-y",
            "-loop", "1", "-i", image_path,
            "-i", audio_path,
            "-c:v", video_codec,
            *tune_args,
            "-vf", f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
            "-r", str(fps),
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-pix_fmt", "yuv420p",
            *quality_args,
            "-shortest",
            out_path,
        ]
    else:
        _emit(on_progress, "segment.probe_duration", path=out_path, audio=audio_path)
        probe = subprocess.run(
            [get_ffprobe_path(), "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True,
        )
        duration = float(probe.stdout.strip()) if probe.stdout.strip() else 10.0

        zp_filter = _build_zoompan_filter(image_type, width, height, fps, duration)
        vf = f"{zp_filter},format=yuv420p"

        cmd = [
            get_ffmpeg_path(), "-y",
            "-loop", "1", "-i", image_path,
            "-i", audio_path,
            "-vf", vf,
            "-c:v", video_codec,
            *tune_args,
            "-c:a", "aac", "-b:a", audio_bitrate,
            "-pix_fmt", "yuv420p",
            *quality_args,
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

    list_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    try:
        for p in segment_paths:
            safe = p.replace("\\", "/").replace("'", "'\\''")
            list_file.write(f"file '{safe}'\n")
        list_file.close()

        cmd = [
            get_ffmpeg_path(), "-y",
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


def resolve_patch_image(patch: Patch, book: Book | None, default_image: str) -> str | None:
    """Resolve the image for a patch: patch.image_path -> book.background_image_path -> default."""
    if patch.image_path and Path(patch.image_path).exists():
        return patch.image_path
    if book and book.background_image_path and Path(book.background_image_path).exists():
        return book.background_image_path
    if Path(default_image).exists():
        return default_image
    return None


def generate_full_video(
    patches: list[Patch],
    book: Book,
    out_path: str,
    *,
    default_image: str,
    use_nvenc: bool = False,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Generate a full video by creating segments per patch and concatenating.

    on_progress: optional callback(event, fields) for progress logging.
    Events: video.start, video.segment_skipped, video.segments_done, video.done, video.failed.
    """
    w, h = (book.video_resolution or "1920x1080").split("x")
    resolution = (int(w), int(h))
    fps = book.video_fps or 30
    default_anim = book.default_image_animation or "none"

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
            image = resolve_patch_image(patch, book, default_image)
            if not image:
                _emit(on_progress, "video.segment_skipped",
                      patch_index=patch.patch_index, reason="no_image")
                continue

            anim = patch.image_type if patch.image_type and patch.image_type != "static" else default_anim
            seg_path = str(tmp_dir / f"seg_{i:04d}.mp4")

            def _seg_progress(event: str, fields: dict) -> None:
                _emit(on_progress, event, patch_index=patch.patch_index,
                      patch_id=patch.id, **{k: v for k, v in fields.items() if k != "path"})

            generate_segment(
                image, patch.audio_path, seg_path,
                image_type=anim,
                resolution=resolution,
                fps=fps,
                use_nvenc=use_nvenc,
                on_progress=_seg_progress,
            )
            segment_paths.append(seg_path)
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
            tmp_dir.rmdir()


def generate_standalone_video(
    audio_path: str,
    image_path: str,
    out_path: str,
    *,
    resolution: str = "1920x1080",
    fps: int = 30,
    codec: str = "libx264",
    audio_bitrate: str = "192k",
    image_type: str = "none",
    crf: int = 23,
    on_progress: ProgressCallback | None = None,
) -> None:
    """Generate a standalone video from a single audio + image (Video Creator page)."""
    w, h = resolution.split("x")
    res = (int(w), int(h))
    use_nvenc = codec == "h264_nvenc"
    generate_segment(
        image_path, audio_path, out_path,
        image_type=image_type,
        resolution=res,
        fps=fps,
        audio_bitrate=audio_bitrate,
        crf=crf,
        use_nvenc=use_nvenc,
        on_progress=on_progress,
    )
