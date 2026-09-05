"""Export a short looping MP4 preview for every gameplay catalog entry.

The previews intentionally reuse the same deterministic simulators and painters as the
video renderer, so the gallery never promises motion the rendered clip cannot produce.
Run from the repository root with ``python scripts/generate_gameplay_videos.py``.

Uses ``imageio-ffmpeg`` when available and otherwise falls back to an ``ffmpeg`` binary on
PATH. Frame rate is kept low (~12 fps) and the loop is short (~3 s) so the file stays well
under a few hundred kilobytes per game even at 960x540.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

try:
    import imageio_ffmpeg
except ImportError:  # A system FFmpeg is enough for this small maintenance script.
    imageio_ffmpeg = None

# Allow this maintenance script to run directly from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.gameplay_effects import ProceduralClip  # noqa: E402
from app.gameplay_registry import list_games, simulate_game  # noqa: E402
from app.gameplay_retro import is_retro  # noqa: E402
from app.gameplay_retro_render import RetroClip  # noqa: E402


SIZE = (960, 540)
OUTPUT_DIR = Path("frontend/public/gameplay")
SEED = 20260819

DURATION_SECONDS = 3.0
FPS = 12
LOOP_DURATION = int(DURATION_SECONDS * FPS)  # 36 frames per clip is plenty for a card preview


def _ffmpeg_exe() -> str | None:
    return imageio_ffmpeg.get_ffmpeg_exe() if imageio_ffmpeg is not None else shutil.which("ffmpeg")


def _frames_for(game_id: str) -> list[Image.Image]:
    """Render the deterministic preview frames for a single game id."""
    width, height = SIZE
    rate = FPS
    # A high hi-score keeps the still from claiming a rank the catalog cannot back up.
    replay = simulate_game(game_id, SEED + sum(map(ord, game_id)), {"hi_score": 10_000})
    if is_retro(game_id):
        clip = RetroClip(game_id, replay.payload, replay.duration_seconds, width, height, rate)
        # The retro painter fast-forwards its engine internally, so one call per frame.
        return [clip.frame(index, index / rate) for index in range(LOOP_DURATION)]
    clip = ProceduralClip(game_id, replay.payload, replay.duration_seconds, width, height, rate)
    # Stateful styles (silk and starfall) need a few prior frames to form their trails, so
    # warm the clip up to the loop's starting tick once and then take a full pass.
    warm_up = max(0, int(replay.duration_seconds * rate) - LOOP_DURATION)
    for index in range(warm_up):
        clip.frame(index, index / rate)
    start_tick = warm_up
    return [clip.frame(start_tick + index, (start_tick + index) / rate)
            for index in range(LOOP_DURATION)]


def _encode(frames: list[Image.Image], target: Path) -> None:
    """Pipe raw RGB24 frames to ffmpeg and write a small, browser-friendly MP4."""
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg:
        raise RuntimeError("ffmpeg binary not available")
    width, height = frames[0].size
    cmd = [
        ffmpeg, "-y", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24",
        "-s", f"{width}x{height}", "-r", str(FPS),
        "-i", "-",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-crf", "28",
        "-movflags", "+faststart",
        "-an",
        str(target),
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert proc.stdin is not None
    try:
        for image in frames:
            proc.stdin.write(image.tobytes())
        proc.stdin.close()
        proc.wait()
    finally:
        if proc.stdin and not proc.stdin.closed:
            proc.stdin.close()
    if proc.returncode != 0:
        stderr = proc.stderr.read().decode("utf-8", errors="ignore") if proc.stderr else ""
        raise RuntimeError(f"ffmpeg failed for {target.name}: {stderr.strip()}")


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ffmpeg = _ffmpeg_exe()
    if not ffmpeg or not Path(ffmpeg).exists():
        raise SystemExit("ffmpeg binary not available; install FFmpeg or imageio-ffmpeg.")
    for game in list_games():
        game_id = game["id"]
        target = OUTPUT_DIR / f"{game_id}.mp4"
        # Skip work that is already done so re-runs are cheap.
        if target.exists() and target.stat().st_size > 0:
            print(f"skip {target}")
            continue
        print(f"render {target}")
        frames = _frames_for(game_id)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp) / target.name
            _encode(frames, tmp_path)
            shutil.move(tmp_path, target)


if __name__ == "__main__":
    main()
