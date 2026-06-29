"""Video generation: static image, Ken Burns animated, multi-segment concat, standalone."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from app.models import Book, Patch


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
) -> None:
    """Generate a single video segment from image + audio.

    image_type: 'none' (static), 'zoom-in', 'zoom-out', 'pan-left', 'pan-right'
    """
    video_codec = "h264_nvenc" if use_nvenc else "libx264"
    width, height = resolution

    if use_nvenc:
        quality_args = ["-cq", str(crf)]
        tune_args = []
    else:
        quality_args = ["-crf", str(crf)]
        tune_args = ["-tune", "stillimage"]

    if image_type == "none":
        cmd = [
            "ffmpeg", "-y",
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
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", audio_path],
            capture_output=True, text=True,
        )
        duration = float(probe.stdout.strip()) if probe.stdout.strip() else 10.0

        zp_filter = _build_zoompan_filter(image_type, width, height, fps, duration)
        vf = f"{zp_filter},format=yuv420p"

        cmd = [
            "ffmpeg", "-y",
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

    subprocess.run(cmd, check=True, capture_output=True, text=True)


def concat_segments(segment_paths: list[str], out_path: str) -> None:
    """Concat multiple segment videos into one using ffmpeg concat demuxer."""
    if not segment_paths:
        raise ValueError("No segments to concat")
    if len(segment_paths) == 1:
        Path(out_path).write_bytes(Path(segment_paths[0]).read_bytes())
        return

    list_file = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False)
    try:
        for p in segment_paths:
            safe = p.replace("\\", "/").replace("'", "'\\''")
            list_file.write(f"file '{safe}'\n")
        list_file.close()

        cmd = [
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_file.name,
            "-c", "copy",
            out_path,
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    finally:
        Path(list_file.name).unlink(missing_ok=True)


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
) -> None:
    """Generate a full video by creating segments per patch and concatenating."""
    w, h = (book.video_resolution or "1920x1080").split("x")
    resolution = (int(w), int(h))
    fps = book.video_fps or 30
    default_anim = book.default_image_animation or "none"

    segment_paths: list[str] = []
    tmp_dir = Path(out_path).parent / "_segments"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        for i, patch in enumerate(patches):
            if not patch.audio_path:
                continue
            image = resolve_patch_image(patch, book, default_image)
            if not image:
                raise ValueError(f"No image available for patch {patch.patch_index}")

            anim = patch.image_type if patch.image_type and patch.image_type != "static" else default_anim

            seg_path = str(tmp_dir / f"seg_{i:04d}.mp4")
            generate_segment(
                image, patch.audio_path, seg_path,
                image_type=anim,
                resolution=resolution,
                fps=fps,
                use_nvenc=use_nvenc,
            )
            segment_paths.append(seg_path)

        if not segment_paths:
            raise ValueError("No segments were generated")

        concat_segments(segment_paths, out_path)
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
    )
