"""CPU renderer that streams deterministic RGB frames directly to FFmpeg."""
from __future__ import annotations

import math
import random
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from app.config import settings
from app.gameplay_models import GameplayReplay, Replay
from app.gameplay_registry import get_game


def _font(size: int):
    for path in ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"):
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _load_role_sprites(asset_dir: str | None, assets: dict, roles: tuple[str, ...]) -> dict[str, Image.Image]:
    """Load theme-pack PNGs declared for these roles; missing/unreadable roles fall back to primitives."""
    sprites: dict[str, Image.Image] = {}
    if not asset_dir or not roles:
        return sprites
    base = Path(asset_dir)
    if not base.is_dir():
        return sprites
    for role in roles:
        filename = assets.get(role)
        if not filename:
            continue
        try:
            with Image.open(base / filename) as sprite:
                sprites[role] = sprite.convert("RGBA")
        except (OSError, ValueError):
            continue
    return sprites


def _sprite_icon(sprite: Image.Image, size: int) -> Image.Image:
    size = max(1, size)
    return sprite.resize((size, size), Image.Resampling.LANCZOS)


def _faded(icon: Image.Image, factor: float) -> Image.Image:
    if factor >= 0.999:
        return icon
    faded = icon.copy()
    alpha = faded.split()[3].point(lambda a: int(a * factor))
    faded.putalpha(alpha)
    return faded


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
    theme = replay.themes[0] if replay.themes else {}
    sprites = _load_role_sprites(theme.get("asset_dir"), theme.get("assets") or {},
                                 get_game(replay.game_id).sprite_roles)
    try:
        for index in range(frame_count):
            t = index / fps
            progress = min(1.0, t / max(replay.duration_seconds, 0.001))
            if replay.game_id == "garden_cycle":
                image = _garden_frame(payload, t, progress, width, height, sprites)
            elif replay.game_id == "orbit_drift":
                image = _orbit_frame(payload, t, progress, width, height, sprites)
            elif replay.game_id in {"aquarium_ecosystem", "parcel_route", "cloud_runner"}:
                image = _pixel_ambient_frame(replay.game_id, payload, t, progress, width, height, sprites)
            elif replay.game_id in {"marble_flow", "territory_bloom", "signal_garden"}:
                image = _neon_ambient_frame(replay.game_id, payload, t, progress, width, height, sprites)
            else:
                raise ValueError(f"no renderer for game {replay.game_id}")
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


def _garden_frame(payload: dict, t: float, progress: float, width: int, height: int,
                  sprites: dict[str, Image.Image]) -> Image.Image:
    image = Image.new("RGB", (width, height), "#17231d")
    draw = ImageDraw.Draw(image)
    horizon = int(height * 0.22)
    draw.rectangle((0, 0, width, horizon), fill="#8fc6b4")
    draw.ellipse((width * 0.78, height * 0.05, width * 0.86, height * 0.13), fill="#f5d789")
    plots = payload.get("plots", [])
    columns, rows = 9, 5
    margin_x, margin_y = width * 0.08, height * 0.28
    cell_w, cell_h = (width - margin_x * 2) / columns, (height * 0.62) / rows
    colors = ("#729b45", "#e3a84f", "#d76557", "#9075b8", "#df8c45")
    tended = int(progress * max(1, len(plots)))
    route = payload.get("route", list(range(len(plots))))
    active = route[min(tended, len(route) - 1)] if route else 0
    for idx, plot in enumerate(plots):
        x0 = margin_x + plot["column"] * cell_w
        y0 = margin_y + plot["row"] * cell_h
        draw.rounded_rectangle((x0 + 3, y0 + 3, x0 + cell_w - 3, y0 + cell_h - 3),
                               radius=max(2, int(cell_h * 0.08)), fill="#59442f", outline="#846548")
        stage = 3 if idx in route[:tended] else max(0, int((t + plot["phase"]) / 18) % 4)
        if stage:
            crop_names = ("carrot", "wheat", "tomato", "lavender", "pumpkin")
            crop_key = plot.get("crop") if plot.get("crop") in crop_names else crop_names[0]
            crop = crop_names.index(crop_key)
            radius = max(2, int(min(cell_w, cell_h) * (0.06 + stage * 0.035)))
            cx, cy = int(x0 + cell_w / 2), int(y0 + cell_h / 2)
            sprite = sprites.get(crop_key)
            if sprite is not None:
                icon = _sprite_icon(sprite, radius * 2)
                image.paste(icon, (cx - icon.width // 2, cy - icon.height // 2), icon)
            else:
                draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), fill=colors[crop])
    if route:
        plot = plots[active]
        x = margin_x + (plot["column"] + 0.5) * cell_w
        y = margin_y + (plot["row"] + 0.5) * cell_h
        r = max(5, int(height * 0.018))
        draw.ellipse((x-r, y-r, x+r, y+r), fill="#f7e8be", outline="#374b32", width=max(1, width//640))
    return image


def _orbit_frame(payload: dict, t: float, progress: float, width: int, height: int,
                 sprites: dict[str, Image.Image]) -> Image.Image:
    image = Image.new("RGB", (width, height), "#050816")
    draw = ImageDraw.Draw(image)
    scale = min(width, height) * 0.42
    cx, cy = width / 2, height / 2
    for star in payload.get("stars", []):
        pulse = 0.45 + 0.55 * math.sin(t * 0.45 + star["phase"] * math.tau) ** 2
        shade = int(100 + pulse * 120)
        x, y, r = cx + star["x"] * width/2, cy + star["y"] * height/2, star["size"]
        draw.ellipse((x-r, y-r, x+r, y+r), fill=(shade, shade, min(255, shade+25)))
    gates = payload.get("gates", [])
    gate_sprite = sprites.get("gate")
    for index, gate in enumerate(gates):
        x, y = cx + gate["x"] * scale, cy + gate["y"] * scale
        radius = max(5, int(gate["size"] * scale))
        passed = index / max(1, len(gates)) >= progress
        if gate_sprite is not None:
            icon = _faded(_sprite_icon(gate_sprite, radius * 2), 1.0 if passed else 0.35)
            image.paste(icon, (int(x) - icon.width // 2, int(y) - icon.height // 2), icon)
        else:
            color = "#2fe8ff" if passed else "#5d587e"
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), outline=color, width=max(2, width//480))
    if gates:
        position = progress * (len(gates) - 1)
        left = int(position)
        right = min(len(gates)-1, left+1)
        blend = position-left
        gx = gates[left]["x"] * (1-blend) + gates[right]["x"] * blend
        gy = gates[left]["y"] * (1-blend) + gates[right]["y"] * blend
        x, y = cx + gx*scale, cy + gy*scale
        size = max(7, int(height * 0.014))
        ship_sprite = sprites.get("ship")
        if ship_sprite is not None:
            icon = _sprite_icon(ship_sprite, size * 2)
            image.paste(icon, (int(x) - icon.width // 2, int(y) - icon.height // 2), icon)
        else:
            draw.polygon(((x+size, y), (x-size, y-size*0.7), (x-size*0.4, y), (x-size, y+size*0.7)),
                         fill="#b9ff36", outline="#ffffff")
    return image


def _pixel_ambient_frame(game_id: str, payload: dict, t: float, progress: float,
                         width: int, height: int, sprites: dict[str, Image.Image]) -> Image.Image:
    palettes = {
        "aquarium_ecosystem": ("#13354a", "#1d7080", "#f2c66d"),
        "parcel_route": ("#8fb8a0", "#d8c18a", "#b75f4b"),
        "cloud_runner": ("#83b7cf", "#dcecc4", "#f2cb6c"),
    }
    background, ground, accent = palettes[game_id]
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)
    if game_id == "aquarium_ecosystem":
        draw.rectangle((0, height*0.82, width, height), fill="#735d45")
    else:
        draw.rectangle((0, height*0.64, width, height), fill=ground)
        for x in range(0, width, max(24, width//12)):
            draw.rectangle((x, height*0.55, x+width*0.055, height*0.64), fill="#e6d8b2")
    for entity in payload.get("entities", []):
        x = ((entity["x"] + 1 + t * entity["speed"]) % 2 - 1) * width/2 + width/2
        y = (entity["y"] + 1) * height/2
        size = max(4, int(height * (0.012 + entity["kind"] * 0.003)))
        sprite = sprites.get(f"kind_{entity['kind']}")
        if sprite is not None:
            icon = _sprite_icon(sprite, size * 2)
            image.paste(icon, (int(x) - icon.width // 2, int(y) - icon.height // 2), icon)
        elif game_id == "aquarium_ecosystem":
            draw.ellipse((x-size, y-size*0.6, x+size, y+size*0.6), fill=accent)
            draw.polygon(((x-size, y), (x-size*1.6, y-size*0.6), (x-size*1.6, y+size*0.6)), fill=accent)
        elif game_id == "parcel_route" and entity["kind"] == 0:
            draw.rounded_rectangle((x-size*1.5, y-size, x+size*1.5, y+size), radius=3, fill=accent)
        else:
            draw.ellipse((x-size, y-size, x+size, y+size), fill=accent)
    return image


def _neon_ambient_frame(game_id: str, payload: dict, t: float, progress: float,
                        width: int, height: int, sprites: dict[str, Image.Image]) -> Image.Image:
    image = Image.new("RGB", (width, height), "#050816")
    draw = ImageDraw.Draw(image)
    nodes = payload.get("nodes", [])
    points = [(width/2 + node["x"]*width*0.42, height/2 + node["y"]*height*0.42) for node in nodes]
    colors = {"marble_flow": "#16f2ff", "territory_bloom": "#ff3dcb", "signal_garden": "#b9ff36"}
    color = colors[game_id]
    line_width = max(2, width//640)
    node_sprite = sprites.get("node")
    for index in range(len(points)-1):
        draw.line((points[index], points[index+1]), fill="#26345d", width=line_width)
    for index, (x, y) in enumerate(points):
        pulse = 0.55 + 0.45*math.sin(t*0.7 + index) ** 2
        radius = max(5, int(height*0.012*pulse))
        active = index / max(1, len(points)-1) <= progress
        if node_sprite is not None:
            icon = _faded(_sprite_icon(node_sprite, radius * 2), 1.0 if active else 0.35)
            image.paste(icon, (int(x) - icon.width // 2, int(y) - icon.height // 2), icon)
        else:
            draw.ellipse((x-radius, y-radius, x+radius, y+radius), outline=color if active else "#4c5270", width=line_width)
    if points:
        position = progress * (len(points)-1)
        left = int(position)
        right = min(len(points)-1, left+1)
        blend = position-left
        x = points[left][0]*(1-blend)+points[right][0]*blend
        y = points[left][1]*(1-blend)+points[right][1]*blend
        radius = max(6, int(height*0.014))
        draw.ellipse((x-radius, y-radius, x+radius, y+radius), fill=color, outline="#ffffff")
    return image


def render_replay(replay: Replay | GameplayReplay, output_path: str, *, resolution=(1920, 1080), fps=30,
                  quality=23, on_progress=None) -> None:
    if isinstance(replay, GameplayReplay):
        _render_generic(replay, output_path, resolution=resolution, fps=fps, quality=quality,
                        on_progress=on_progress)
        return
    _render_battle_royale(replay, output_path, resolution=resolution, fps=fps, quality=quality,
                          on_progress=on_progress)
