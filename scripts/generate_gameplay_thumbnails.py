"""Export one representative still for every gameplay catalog entry.

The thumbnails intentionally use the same deterministic simulators and painters as the
video renderer, so the catalog never promises a visual style the rendered clip cannot
produce. Run from the repository root with ``python scripts/generate_gameplay_thumbnails.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

# Allow this maintenance script to run directly from the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.gameplay_effects import ProceduralClip
from app.gameplay_registry import list_games, simulate_game
from app.gameplay_retro import is_retro
from app.gameplay_retro_render import RetroClip


SIZE = (960, 540)
OUTPUT_DIR = Path("frontend/public/gameplay")
SEED = 20260819


def _legacy_frame() -> Image.Image:
    """A compact still matching the built-in Neon Battle Royale palette."""
    width, height = SIZE
    image = Image.new("RGB", SIZE, "#050817")
    draw = ImageDraw.Draw(image)
    cx, cy, radius = width // 2, height // 2, int(height * 0.36)
    draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill="#0b1431", outline="#1ce8ff", width=4)
    draw.ellipse((cx - int(radius * 0.74), cy - int(radius * 0.74), cx + int(radius * 0.74), cy + int(radius * 0.74)), outline="#baff39", width=4)
    colors = ("#20e7ff", "#ff45c8", "#baff39")
    for index in range(18):
        x = cx + int(radius * 0.68 * __import__("math").cos(index * 0.95))
        y = cy + int(radius * 0.68 * __import__("math").sin(index * 0.95))
        r = 13 + (index % 3) * 3
        draw.ellipse((x - r, y - r, x + r, y + r), fill=colors[index % len(colors)], outline="white", width=2)
    return image


def _frame(game_id: str) -> Image.Image:
    if game_id == "battle_royale":
        return _legacy_frame()
    # A high hi-score keeps the still from claiming a rank the catalog cannot back up.
    replay = simulate_game(game_id, SEED + sum(map(ord, game_id)), {"hi_score": 10_000})
    width, height = SIZE
    # Far enough in that a retro board has a real score on it and a trail style has trails.
    t = 90.0 if is_retro(game_id) else min(24.0, replay.duration_seconds * 0.18)
    rate = 12
    if is_retro(game_id):
        # The retro painter fast-forwards its engine internally, so one call is enough.
        clip = RetroClip(game_id, replay.payload, replay.duration_seconds, width, height, rate)
        return clip.frame(int(t * rate), t)
    clip = ProceduralClip(game_id, replay.payload, replay.duration_seconds, width, height, rate)
    # Stateful styles (silk and starfall) need a few prior frames to form their trails.
    image = None
    for index in range(int(t * rate) + 1):
        image = clip.frame(index, index / rate)
    assert image is not None
    return image


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for game in list_games():
        path = OUTPUT_DIR / f"{game['id']}.jpg"
        if path.exists():
            continue
        _frame(game["id"]).save(path, "JPEG", quality=90, optimize=True)
        print(path)


if __name__ == "__main__":
    main()
