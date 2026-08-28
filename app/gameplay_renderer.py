"""CPU renderer that streams deterministic RGB frames directly to FFmpeg."""
from __future__ import annotations

import subprocess

import numpy as np
from PIL import Image, ImageFont
from pathlib import Path

from app.config import settings
from app.gameplay_effects import ProceduralClip, is_procedural
from app.gameplay_models import GameplayReplay
from app.gameplay_retro import is_retro
from app.gameplay_retro_render import RetroClip


def _font(size: int):
    for path in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_replay(replay: GameplayReplay, output_path: str, *, resolution=(1920, 1080), fps=30,
                  quality=23, on_progress=None) -> None:
    width, height = resolution
    if width <= 0 or height <= 0 or fps <= 0:
        raise ValueError("invalid gameplay render dimensions")
    frame_count = max(1, int(round(replay.duration_seconds * fps)))
    cmd = [settings.get_ffmpeg_path(), "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", str(quality), "-pix_fmt", "yuv420p", output_path]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
    if process.stdin is None:
        process.kill()
        raise RuntimeError("gameplay ffmpeg stdin unavailable")
    payload = replay.payload
    if is_procedural(replay.game_id):
        painter = ProceduralClip(replay.game_id, payload, replay.duration_seconds, width, height, fps)
    elif is_retro(replay.game_id):
        painter = RetroClip(replay.game_id, payload, replay.duration_seconds, width, height, fps)
    else:
        raise ValueError(f"no renderer for game {replay.game_id}")
    try:
        for index in range(frame_count):
            image = painter.frame(index, index / fps)
            process.stdin.write(np.asarray(image, dtype=np.uint8).tobytes())
            if on_progress and index % max(fps, 1) == 0:
                on_progress(index, frame_count)
        process.stdin.close()
        code = process.wait()
        if code:
            raise RuntimeError(f"gameplay ffmpeg failed ({code})")
        if on_progress:
            on_progress(frame_count, frame_count)
    except Exception:
        process.kill()
        process.wait()
        raise
