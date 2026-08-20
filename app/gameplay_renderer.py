"""CPU renderer that streams deterministic RGB frames directly to FFmpeg."""
from __future__ import annotations

import math
import random
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.gameplay_effects import ProceduralClip, is_procedural
from app.gameplay_models import GameplayReplay, Replay
from app.gameplay_retro import is_retro
from app.gameplay_retro_render import RetroClip


def _font(size: int):
    for path in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _zone_radius(t: float, arena_radius: int) -> float:
    zone = max(0.16, 1.0 - 0.82 * min(1.0, t / 270.0))
    return arena_radius * zone


def _fighter_position(index: int, count: int, t: float, cx: int, cy: int, zr: float) -> tuple[float, float]:
    angle = 2 * math.pi * index / count + t * (0.055 + (index % 5) * 0.004)
    orbit = zr * (0.35 + 0.45 * ((index * 7) % 11) / 10)
    return cx + math.cos(angle) * orbit, cy + math.sin(angle) * orbit


def _sector_points(cx: int, cy: int, arena_radius: int, count: int = 6) -> list[tuple[float, float]]:
    """Evenly spaced sweep stops around the arena, visited in clockwise order."""
    ring = arena_radius * 0.55
    return [(cx + ring * math.cos(2 * math.pi * i / count - math.pi / 2),
             cy + ring * math.sin(2 * math.pi * i / count - math.pi / 2)) for i in range(count)]


def _build_camera_keyframes(seed: int, duration: float, cx: int, cy: int, arena_radius: int,
                            roster: list[dict], events: list[dict]) -> list[tuple[float, float, float, float]]:
    """Deterministic (time, zoom, focus_x, focus_y) keyframes; the frame loop smoothsteps between them.
    The camera sweeps the arena's regions in order (one at a time), pausing at a wide shot between
    laps, and snaps to the exact elimination spot when one falls inside the region's visit window."""
    rng = random.Random(seed ^ 0x5EED)
    key_to_index = {f["key"]: i for i, f in enumerate(roster)}
    count = len(roster)
    sectors = _sector_points(cx, cy, arena_radius)
    keyframes: list[tuple[float, float, float, float]] = [(0.0, 1.0, float(cx), float(cy))]
    t = 0.0
    sector_index = 0
    lap = 0
    while True:
        t += rng.uniform(6.0, 9.5)
        if t >= duration - 3.0:
            break
        if sector_index == 0 and lap > 0:
            keyframes.append((t, 1.0, float(cx), float(cy)))
            t += rng.uniform(2.0, 3.5)
            if t >= duration - 3.0:
                break
        base_x, base_y = sectors[sector_index]
        focus_x, focus_y, zoom = base_x, base_y, rng.uniform(1.5, 1.9)
        nearby = [e for e in events if abs(float(e["t"]) - t) < 5.0]
        if nearby:
            event = min(nearby, key=lambda e: abs(float(e["t"]) - t))
            ai, ti = key_to_index.get(event["actor"]), key_to_index.get(event["target"])
            if ai is not None and ti is not None:
                et = float(event["t"])
                ezr = _zone_radius(et, arena_radius)
                ax, ay = _fighter_position(ai, count, et, cx, cy, ezr)
                tx, ty = _fighter_position(ti, count, et, cx, cy, ezr)
                focus_x, focus_y = (ax + tx) / 2, (ay + ty) / 2
                zoom = rng.uniform(1.8, 2.2)
        keyframes.append((t, zoom, focus_x, focus_y))
        sector_index = (sector_index + 1) % len(sectors)
        if sector_index == 0:
            lap += 1
    keyframes.append((duration, 1.0, float(cx), float(cy)))
    return keyframes


def _camera_at(keyframes: list[tuple[float, float, float, float]], t: float) -> tuple[float, float, float]:
    for i in range(len(keyframes) - 1):
        t0, z0, x0, y0 = keyframes[i]
        t1, z1, x1, y1 = keyframes[i + 1]
        if t <= t1 or i == len(keyframes) - 2:
            u = min(1.0, max(0.0, (t - t0) / max(0.001, t1 - t0)))
            u = u * u * (3 - 2 * u)
            return z0 + (z1 - z0) * u, x0 + (x1 - x0) * u, y0 + (y1 - y0) * u
    return 1.0, float(keyframes[0][2]), float(keyframes[0][3])


def _apply_camera(world: Image.Image, zoom: float, fx: float, fy: float, width: int, height: int) -> Image.Image:
    if zoom <= 1.001:
        return world
    view_w, view_h = width / zoom, height / zoom
    x0 = min(max(0.0, fx - view_w / 2), width - view_w)
    y0 = min(max(0.0, fy - view_h / 2), height - view_h)
    crop = world.crop((int(x0), int(y0), int(x0 + view_w), int(y0 + view_h)))
    return crop.resize((width, height), Image.Resampling.LANCZOS)


_ATTACK_COLORS = {"tank": (32, 231, 255), "assassin": (255, 69, 200), "ranger": (186, 255, 57)}


def _draw_attack_effects(world: Image.Image, roster: list[dict], events: list[dict], t: float,
                         cx: int, cy: int, arena_radius: int) -> None:
    """Paint a fading beam + impact burst between attacker and target around each elimination's timestamp."""
    key_to_index = {f["key"]: i for i, f in enumerate(roster)}
    count = len(roster)
    overlay = None
    odraw = None
    for event in events:
        dt = t - float(event["t"])
        if dt < -0.35 or dt > 0.55:
            continue
        ai, ti = key_to_index.get(event["actor"]), key_to_index.get(event["target"])
        if ai is None or ti is None:
            continue
        if overlay is None:
            overlay = Image.new("RGBA", world.size, (0, 0, 0, 0))
            odraw = ImageDraw.Draw(overlay)
        et = float(event["t"])
        zr = _zone_radius(et, arena_radius)
        ax, ay = _fighter_position(ai, count, et, cx, cy, zr)
        tx, ty = _fighter_position(ti, count, et, cx, cy, zr)
        color = _ATTACK_COLORS.get(roster[ai]["class_name"], (255, 255, 255))
        if dt < 0:
            progress = (dt + 0.35) / 0.35
            alpha = int(210 * progress)
            beam_width = max(2, int(2 + 5 * progress))
        else:
            progress = dt / 0.55
            alpha = int(255 * max(0.0, 1.0 - progress) ** 1.4)
            beam_width = max(2, int(8 * (1 - progress) + 2))
        odraw.line((ax, ay, tx, ty), fill=color + (alpha,), width=beam_width)
        if 0.0 <= dt < 0.2:
            burst = 6 + 30 * (dt / 0.2)
            burst_alpha = int(255 * (1 - dt / 0.2))
            odraw.ellipse((tx - burst, ty - burst, tx + burst, ty + burst), outline=color + (burst_alpha,),
                          width=max(2, int(4 * (1 - dt / 0.2)) + 1))
    if overlay is not None:
        world.paste(overlay, (0, 0), overlay)


def _render_battle_royale(replay: Replay, output_path: str, *, resolution=(1920, 1080), fps=30,
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
    title_font, hud_font, small_font = _font(max(18, height // 28)), _font(max(16, height // 38)), _font(max(14, height // 44))
    sprite_size = max(30, height // 16)
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
    camera_keyframes = _build_camera_keyframes(replay.seed, replay.duration_seconds, cx, cy, arena_radius,
                                               roster, events)
    try:
        for index in range(frame_count):
            t = index / fps
            world = Image.new("RGB", (width, height), "#050817")
            draw = ImageDraw.Draw(world)
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
                x, y = _fighter_position(fighter_index, len(roster), t, cx, cy, zr)
                x, y = int(x), int(y)
                color = {"tank":"#20e7ff", "assassin":"#ff45c8", "ranger":"#baff39"}[fighter["class_name"]]
                radius = max(10, sprite_size//2)
                sprite = sprites.get(fighter["class_name"])
                if sprite is not None:
                    world.paste(sprite, (x-sprite_size//2, y-sprite_size//2), sprite)
                else:
                    draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color, outline="white", width=2)
                bar_width = max(18, sprite_size)
                draw.rectangle((x-bar_width//2, y-sprite_size//2-7, x+bar_width//2, y-sprite_size//2-4), fill="#26334f")
                draw.rectangle((x-bar_width//2, y-sprite_size//2-7, x+bar_width//2, y-sprite_size//2-4), outline="#ffffff")
                draw.text((x-radius*2, y+radius+2), fighter["name"], fill="white", font=small_font)
            _draw_attack_effects(world, roster, events, t, cx, cy, arena_radius)
            zoom, fx, fy = _camera_at(camera_keyframes, t)
            image = _apply_camera(world, zoom, fx, fy, width, height)
            hud = ImageDraw.Draw(image)
            alive_count = len(alive)
            status = "CHAMPION" if alive_count == 1 else "TOP 3" if alive_count <= 3 else "TOP 10" if alive_count <= 10 else "ALIVE"
            hud.text((width*0.035, height*0.04), f"{status}  {alive_count}", fill="#f7fbff", font=title_font)
            feed = [e for e in events if 0 <= t-float(e["t"]) <= 5][-4:]
            for line, event in enumerate(reversed(feed)):
                text = f"{names[event['actor']]} eliminated {names[event['target']]}"
                hud.text((width*0.72, height*0.05 + line*(height*0.034)), text, fill="#dbe8ff", font=small_font)
            if t >= replay.duration_seconds - 6:
                winner = names[replay.winner_key]
                box = (width*0.27, height*0.38, width*0.73, height*0.62)
                hud.rounded_rectangle(box, radius=24, fill="#070c22", outline="#baff39", width=4)
                hud.text((width*0.38, height*0.43), "CHAMPION", fill="#baff39", font=title_font)
                hud.text((width*0.42, height*0.52), winner, fill="white", font=hud_font)
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
        process.wait()
        raise


def _render_generic(replay: GameplayReplay, output_path: str, *, resolution=(1920, 1080), fps=30,
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
    # Both families carry per-frame state — particles and trails, or a running match — so one
    # painter serves the whole clip instead of a stateless per-frame function.
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


def render_replay(replay: Replay | GameplayReplay, output_path: str, *, resolution=(1920, 1080), fps=30,
                  quality=23, on_progress=None) -> None:
    if isinstance(replay, GameplayReplay):
        _render_generic(replay, output_path, resolution=resolution, fps=fps, quality=quality,
                        on_progress=on_progress)
        return
    _render_battle_royale(replay, output_path, resolution=resolution, fps=fps, quality=quality,
                          on_progress=on_progress)
