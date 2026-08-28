"""Generate browser previews for entries in the gameplay catalog."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from app.gameplay_effects import ProceduralClip
from app.gameplay_registry import simulate_game
from app.gameplay_retro import is_retro
from app.gameplay_retro_render import RetroClip

SIZE = (960, 540)
FPS = 12
FRAME_COUNT = 36
SEED = 20260819
PUBLIC_DIR = Path(__file__).resolve().parents[1] / "frontend" / "public" / "gameplay"


def _frames(game_id: str) -> tuple[list[Image.Image], Image.Image]:
    width, height = SIZE
    replay = simulate_game(game_id, SEED + sum(map(ord, game_id)), {"hi_score": 10_000})
    if is_retro(game_id):
        clip = RetroClip(game_id, replay.payload, replay.duration_seconds, width, height, FPS)
        frames = [clip.frame(index, index / FPS) for index in range(FRAME_COUNT)]
        return frames, clip.frame(int(90 * FPS), 90.0)

    clip = ProceduralClip(game_id, replay.payload, replay.duration_seconds, width, height, FPS)
    start = max(0, int(replay.duration_seconds * FPS) - FRAME_COUNT)
    for index in range(start):
        clip.frame(index, index / FPS)
    frames = [clip.frame(start + index, (start + index) / FPS) for index in range(FRAME_COUNT)]

    still_clip = ProceduralClip(game_id, replay.payload, replay.duration_seconds, width, height, FPS)
    still_index = int(min(24.0, replay.duration_seconds * 0.18) * FPS)
    still = frames[0]
    for index in range(still_index + 1):
        still = still_clip.frame(index, index / FPS)
    return frames, still


def _encode(frames: list[Image.Image], target: Path) -> None:
    try:
        import imageio_ffmpeg
    except ModuleNotFoundError as exc:
        raise RuntimeError("Thiếu imageio-ffmpeg. Chạy: pip install imageio-ffmpeg") from exc
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    width, height = frames[0].size
    command = [
        ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", str(FPS), "-i", "-", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "28", "-movflags",
        "+faststart", "-an", str(target),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None
    try:
        for frame in frames:
            process.stdin.write(frame.tobytes())
        process.stdin.close()
        process.wait()
    finally:
        if process.stdin and not process.stdin.closed:
            process.stdin.close()
    if process.returncode:
        stderr = process.stderr.read().decode("utf-8", errors="ignore") if process.stderr else ""
        raise RuntimeError(f"ffmpeg failed for {target.name}: {stderr.strip()}")


def generate_preview(game_id: str, output_dir: Path = PUBLIC_DIR) -> dict[str, str]:
    """Render and atomically publish the MP4 and poster for one catalog game."""
    output_dir.mkdir(parents=True, exist_ok=True)
    frames, still = _frames(game_id)
    video = output_dir / f"{game_id}.mp4"
    poster = output_dir / f"{game_id}.jpg"
    with tempfile.TemporaryDirectory(dir=output_dir) as tmp:
        temporary = Path(tmp)
        _encode(frames, temporary / video.name)
        still.save(temporary / poster.name, "JPEG", quality=90, optimize=True)
        shutil.move(temporary / video.name, video)
        shutil.move(temporary / poster.name, poster)
    return {"video": f"/gameplay/{video.name}", "poster": f"/gameplay/{poster.name}"}
