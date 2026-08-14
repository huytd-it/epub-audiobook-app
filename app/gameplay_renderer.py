"""CPU renderer that streams deterministic RGB frames directly to FFmpeg."""
from __future__ import annotations

import math
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.gameplay_models import Replay


def _font(size: int):
    for path in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def render_replay(replay: Replay, output_path: str, *, resolution=(1920, 1080), fps=30,
                  quality=23, on_progress=None) -> None:
    width, height = resolution
    frame_count = max(1, int(round(replay.duration_seconds * fps)))
    cmd = [settings.get_ffmpeg_path(), "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
           "-s", f"{width}x{height}", "-r", str(fps), "-i", "-", "-an", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", str(quality), "-pix_fmt", "yuv420p", output_path]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None
    names = {f["key"]: f["name"] for f in replay.roster}
    eliminated = {}
    events = [e for e in replay.events if e["type"] == "elimination"]
    for event in events:
        eliminated[event["target"]] = float(event["t"])
    roster = replay.roster
    arena_radius = int(min(width, height) * 0.39)
    cx, cy = width // 2, height // 2
    title_font, hud_font, small_font = _font(max(18, height // 28)), _font(max(16, height // 38)), _font(max(12, height // 52))
    sprite_size = max(16, height // 35)
    sprites = {}
    asset_dir = Path(str(replay.themes[0].get("asset_dir") or "")) if replay.themes else Path()
    if asset_dir.is_dir():
        for class_name in ("tank", "assassin", "ranger"):
            try:
                with Image.open(asset_dir / f"{class_name}.png") as sprite:
                    sprites[class_name] = sprite.convert("RGBA").resize((sprite_size, sprite_size), Image.Resampling.LANCZOS)
            except (OSError, ValueError):
                sprites.clear()
                break
    try:
        for index in range(frame_count):
            t = index / fps
            image = Image.new("RGB", (width, height), "#050817")
            draw = ImageDraw.Draw(image)
            zone = max(0.16, 1.0 - 0.82 * min(1.0, t / 270.0))
            zr = int(arena_radius * zone)
            draw.ellipse((cx-arena_radius, cy-arena_radius, cx+arena_radius, cy+arena_radius),
                         fill="#0b1431", outline="#1ce8ff", width=max(2, width//480))
            draw.ellipse((cx-zr, cy-zr, cx+zr, cy+zr), outline="#baff39", width=max(3, width//320))
            for obstacle in replay.map.get("obstacles", []):
                x, y = cx + int(obstacle["x"]*arena_radius), cy + int(obstacle["y"]*arena_radius)
                r = max(3, int(obstacle["radius"]*arena_radius))
                draw.rounded_rectangle((x-r, y-r, x+r, y+r), radius=r//3, fill="#182548", outline="#51678f")
            alive = [f for f in roster if eliminated.get(f["key"], replay.duration_seconds + 1) > t]
            for fighter_index, fighter in enumerate(roster):
                death = eliminated.get(fighter["key"])
                if death is not None and death <= t:
                    if t-death < 0.8:
                        angle = 2*math.pi*fighter_index/len(roster) + t*0.13
                        x, y = cx + int(math.cos(angle)*zr*0.75), cy + int(math.sin(angle)*zr*0.75)
                        pr = int(24*(1-(t-death)/0.8))+2
                        draw.ellipse((x-pr, y-pr, x+pr, y+pr), outline="#f8ffb0", width=3)
                    continue
                angle = 2*math.pi*fighter_index/len(roster) + t*(0.055 + (fighter_index%5)*0.004)
                orbit = zr * (0.35 + 0.45*((fighter_index*7)%11)/10)
                x, y = cx + int(math.cos(angle)*orbit), cy + int(math.sin(angle)*orbit)
                color = {"tank":"#20e7ff", "assassin":"#ff45c8", "ranger":"#baff39"}[fighter["class_name"]]
                radius = max(5, height//95)
                sprite = sprites.get(fighter["class_name"])
                if sprite is not None:
                    image.paste(sprite, (x-sprite_size//2, y-sprite_size//2), sprite)
                else:
                    draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color, outline="white", width=2)
                bar_width = max(18, sprite_size)
                draw.rectangle((x-bar_width//2, y-sprite_size//2-7, x+bar_width//2, y-sprite_size//2-4), fill="#26334f")
                draw.rectangle((x-bar_width//2, y-sprite_size//2-7, x+bar_width//2, y-sprite_size//2-4), outline="#ffffff")
                draw.text((x-radius*2, y+radius+2), fighter["name"], fill="white", font=small_font)
            alive_count = len(alive)
            status = "CHAMPION" if alive_count == 1 else "TOP 3" if alive_count <= 3 else "TOP 10" if alive_count <= 10 else "ALIVE"
            draw.text((width*0.035, height*0.04), f"{status}  {alive_count}", fill="#f7fbff", font=title_font)
            feed = [e for e in events if 0 <= t-float(e["t"]) <= 5][-4:]
            for line, event in enumerate(reversed(feed)):
                text = f"{names[event['actor']]} eliminated {names[event['target']]}"
                draw.text((width*0.72, height*0.05 + line*(height*0.034)), text, fill="#dbe8ff", font=small_font)
            if t >= replay.duration_seconds - 6:
                winner = names[replay.winner_key]
                box = (width*0.27, height*0.38, width*0.73, height*0.62)
                draw.rounded_rectangle(box, radius=24, fill="#070c22", outline="#baff39", width=4)
                draw.text((width*0.38, height*0.43), "CHAMPION", fill="#baff39", font=title_font)
                draw.text((width*0.42, height*0.52), winner, fill="white", font=hud_font)
            process.stdin.write(np.asarray(image, dtype=np.uint8).tobytes())
            if on_progress and index % max(fps, 1) == 0:
                on_progress(index, frame_count)
        process.stdin.close()
        stderr = process.stderr.read() if process.stderr else b""
        code = process.wait()
        if code:
            raise RuntimeError(f"gameplay ffmpeg failed ({code}): {stderr[-1000:].decode(errors='replace')}")
    except Exception:
        process.kill()
        raise
