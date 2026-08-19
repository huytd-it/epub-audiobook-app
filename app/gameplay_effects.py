"""Asset-free frame painters for the procedural gameplay family.

Nothing here loads an image: colour comes from palette LUTs, shape from analytic wave fields
and light from additive particle splats plus a two-tap bloom. Fields are evaluated on a small
internal grid (``FIELD_HEIGHT``) and upscaled to the clip resolution, which keeps the frame
budget flat across 1080p/720p/vertical profiles and softens the glow for free.

A clip is rendered by one :class:`ProceduralClip` instance whose ``frame`` is called once per
frame in ascending order; the particle games integrate state between calls. Every value comes
from the frozen replay payload, so a re-render of the same replay produces the same frames.
"""
from __future__ import annotations

import math

import numpy as np
from PIL import Image, ImageFilter

from app.gameplay_procedural import PROCEDURAL_IDS

FIELD_HEIGHT = 448
GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))

PALETTES: dict[str, tuple[tuple[float, tuple[int, int, int]], ...]] = {
    "abyss": ((0.0, (4, 6, 22)), (0.35, (18, 44, 96)), (0.6, (28, 132, 170)),
              (0.82, (120, 226, 214)), (1.0, (238, 252, 255))),
    "ember": ((0.0, (10, 4, 16)), (0.3, (84, 18, 52)), (0.58, (190, 52, 48)),
              (0.8, (248, 146, 54)), (1.0, (255, 240, 190))),
    "orchid": ((0.0, (8, 4, 24)), (0.32, (62, 20, 96)), (0.6, (146, 44, 168)),
               (0.82, (236, 110, 186)), (1.0, (255, 226, 246))),
    "jade": ((0.0, (3, 14, 16)), (0.34, (10, 62, 62)), (0.6, (26, 142, 118)),
             (0.82, (120, 224, 168)), (1.0, (238, 255, 236))),
    "lagoon": ((0.0, (2, 12, 30)), (0.36, (10, 58, 88)), (0.62, (22, 130, 150)),
               (0.84, (126, 226, 214)), (1.0, (240, 254, 255))),
    "moonlit": ((0.0, (6, 8, 24)), (0.34, (30, 40, 80)), (0.62, (84, 104, 164)),
                (0.84, (176, 196, 244)), (1.0, (246, 250, 255))),
    "starlight": ((0.0, (2, 4, 14)), (0.3, (24, 34, 78)), (0.58, (96, 132, 214)),
                  (0.82, (198, 220, 255)), (1.0, (255, 255, 255))),
    "spectrum": ((0.0, (28, 16, 72)), (0.22, (40, 110, 214)), (0.44, (32, 196, 178)),
                 (0.64, (214, 206, 64)), (0.82, (238, 96, 120)), (1.0, (250, 214, 246))),
}


def is_procedural(game_id: str) -> bool:
    return game_id in PROCEDURAL_IDS


def _lut(name: str) -> np.ndarray:
    stops = PALETTES.get(name) or PALETTES["abyss"]
    positions = np.array([stop[0] for stop in stops], dtype=np.float32)
    colors = np.array([stop[1] for stop in stops], dtype=np.float32)
    ramp = np.linspace(0.0, 1.0, 256, dtype=np.float32)
    return np.stack([np.interp(ramp, positions, colors[:, channel]) for channel in range(3)],
                    axis=1).astype(np.float32)


def _shade(field: np.ndarray, lut: np.ndarray) -> np.ndarray:
    """Map a 0..1 scalar field through a palette LUT into an (h, w, 3) float image."""
    return lut[np.clip(field * 255.0, 0.0, 255.0).astype(np.uint8)]


def _sample(lut: np.ndarray, positions: np.ndarray) -> np.ndarray:
    return lut[np.clip(np.mod(positions, 1.0) * 255.0, 0.0, 255.0).astype(np.uint8)]


def _smoothstep(value: np.ndarray | float):
    clamped = np.clip(value, 0.0, 1.0)
    return clamped * clamped * (3.0 - 2.0 * clamped)


def _glow(rgb: np.ndarray, strength: float = 0.6) -> np.ndarray:
    """Two-tap bloom -- a tight halo plus a wide wash -- added into ``rgb`` in place."""
    height, width, _ = rgb.shape
    tight = Image.fromarray(np.clip(rgb, 0.0, 255.0).astype(np.uint8)).resize(
        (max(4, width // 4), max(4, height // 4)), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.6))
    wide = tight.resize((max(2, tight.width // 4), max(2, tight.height // 4)),
                        Image.BILINEAR).filter(ImageFilter.GaussianBlur(2.2))
    # Both taps are combined while still small, so the halo is upscaled only once.
    halo = np.asarray(tight, dtype=np.float32) * 0.55
    halo += np.asarray(wide.resize(tight.size, Image.BILINEAR), dtype=np.float32)
    rgb += np.asarray(Image.fromarray(np.clip(halo * strength, 0.0, 255.0).astype(np.uint8)).resize(
        (width, height), Image.BILINEAR), dtype=np.float32)
    return rgb


def _splat(buffer: np.ndarray, xs: np.ndarray, ys: np.ndarray, colors: np.ndarray, weight) -> None:
    """Additive bilinear point splat; points outside the buffer are dropped."""
    height, width, _ = buffer.shape
    xs = np.clip(np.nan_to_num(xs, nan=-1e6, posinf=1e6, neginf=-1e6), -1e6, 1e6)
    ys = np.clip(np.nan_to_num(ys, nan=-1e6, posinf=1e6, neginf=-1e6), -1e6, 1e6)
    fx = np.floor(xs)
    fy = np.floor(ys)
    dx = (xs - fx).astype(np.float32)[:, None]
    dy = (ys - fy).astype(np.float32)[:, None]
    x0 = fx.astype(np.int32)
    y0 = fy.astype(np.int32)
    tinted = colors * weight
    for offset_x, offset_y, wx, wy in ((0, 0, 1.0 - dx, 1.0 - dy), (1, 0, dx, 1.0 - dy),
                                       (0, 1, 1.0 - dx, dy), (1, 1, dx, dy)):
        px = x0 + offset_x
        py = y0 + offset_y
        inside = (px >= 0) & (px < width) & (py >= 0) & (py < height)
        if not inside.any():
            continue
        np.add.at(buffer, (py[inside], px[inside]), (tinted * (wx * wy))[inside])


def _beat_arrays(events: list[dict]) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    grouped: dict[str, list[tuple[float, float]]] = {}
    for event in events or ():
        kind = str(event.get("type"))
        if kind in {"start", "result"}:
            continue
        grouped.setdefault(kind, []).append((float(event.get("t", 0.0)),
                                             float(event.get("strength", 1.0))))
    return {kind: (np.array([item[0] for item in items], dtype=np.float32),
                   np.array([item[1] for item in items], dtype=np.float32))
            for kind, items in grouped.items()}


class ProceduralClip:
    """Stateful painter for one procedural replay; ``frame`` is called in ascending order."""

    def __init__(self, game_id: str, payload: dict, duration: float, width: int, height: int, fps: int):
        if game_id not in PROCEDURAL_IDS:
            raise ValueError(f"no procedural renderer for game {game_id}")
        if width <= 0 or height <= 0 or fps <= 0:
            raise ValueError("invalid procedural render dimensions")
        self.game_id = game_id
        self.payload = payload
        self.duration = max(float(duration), 0.001)
        self.size = (int(width), int(height))
        self.fps = int(fps)
        self.dt = 1.0 / self.fps
        self.h = min(int(height), FIELD_HEIGHT)
        self.w = max(2, int(round(int(width) * self.h / int(height))))
        aspect = self.w / self.h
        self.aspect = aspect
        self.gy = np.linspace(-1.0, 1.0, self.h, dtype=np.float32)[:, None]
        self.gx = np.linspace(-aspect, aspect, self.w, dtype=np.float32)[None, :]
        self.lut = _lut(str(payload.get("palette", "abyss")))
        self.beats = _beat_arrays(payload.get("events") or [])
        self.center = (self.w / 2.0, self.h / 2.0)
        self.pixel_scale = min(self.w, self.h) * 0.5
        falloff = np.clip((self.gx / aspect) ** 2 + self.gy ** 2, 0.0, 1.6)
        self.vignette = (1.0 - 0.42 * falloff).astype(np.float32)[..., None]
        # A rotating set of low-amplitude noise tiles removes banding from the smooth gradients
        # that libx264 would otherwise quantise into visible steps.
        grain = np.random.default_rng(0x6D17)
        self.dither = [(grain.standard_normal((self.h, self.w, 1)) * 1.7).astype(np.float32)
                       for _ in range(4)]
        self._cursor = 0
        self._setup()

    # ---------------------------------------------------------------- helpers

    def _pulse(self, kind: str, t: float, attack: float = 1.4, release: float = 6.5) -> float:
        """Summed one-shot envelopes for the replay's beats of ``kind`` at time ``t``."""
        beat = self.beats.get(kind)
        if beat is None:
            return 0.0
        times, strengths = beat
        age = t - times
        rising = _smoothstep((age + attack) / attack)
        decay = np.exp(-np.maximum(age, 0.0) / release)
        return float(np.sum(strengths * rising * decay))

    def _setup(self) -> None:
        setup = {"aurora_veil": self._setup_aurora, "plasma_tide": self._setup_plasma,
                 "ripple_pond": self._setup_ripple, "lumen_bloom": self._setup_bloom,
                 "silk_current": self._setup_silk, "starfall_warp": self._setup_warp}
        self._paint = {"aurora_veil": self._paint_aurora, "plasma_tide": self._paint_plasma,
                       "ripple_pond": self._paint_ripple, "lumen_bloom": self._paint_bloom,
                       "silk_current": self._paint_silk, "starfall_warp": self._paint_warp}[self.game_id]
        setup[self.game_id]()

    def frame(self, index: int, t: float) -> Image.Image:
        self._cursor = index
        rgb = self._paint(t, min(1.0, t / self.duration))
        rgb *= self.vignette
        rgb += self.dither[index % len(self.dither)]
        image = Image.fromarray(np.clip(rgb, 0.0, 255.0).astype(np.uint8), "RGB")
        return image if image.size == self.size else image.resize(self.size, Image.BILINEAR)

    def _star_layers(self, seed: int, count: int, brightness: float) -> tuple[np.ndarray, np.ndarray]:
        """Two interleaved static star buffers; cross-fading them animates the twinkle for free."""
        rng = np.random.default_rng(seed)
        xs = rng.random(count, dtype=np.float32) * self.w
        ys = rng.random(count, dtype=np.float32) * self.h
        tone = rng.random(count, dtype=np.float32) * 0.45 + 0.5
        colors = _sample(_lut("starlight"), tone) * brightness
        weight = (rng.random(count, dtype=np.float32) ** 2.2 * 0.9 + 0.1)[:, None]
        layers = (np.zeros((self.h, self.w, 3), np.float32), np.zeros((self.h, self.w, 3), np.float32))
        for parity, layer in enumerate(layers):
            take = np.arange(count) % 2 == parity
            _splat(layer, xs[take], ys[take], colors[take], weight[take])
        return layers

    # ---------------------------------------------------------------- aurora veil

    def _setup_aurora(self) -> None:
        payload = self.payload
        self.curtains = [{
            "color": np.array(curtain["color"], dtype=np.float32) / 255.0,
            "base": float(curtain["base"]),
            "inv_thickness": 1.0 / max(0.02, float(curtain["thickness"])),
            "brightness": float(curtain["brightness"]),
            # Three incommensurate striation frequencies; one alone reads as a picket fence.
            "stria": (float(curtain["shimmer"]), float(curtain["shimmer"]) * 1.618,
                      float(curtain["shimmer"]) * 2.71),
            "waves": [[float(value) for value in wave] for wave in curtain["waves"]],
        } for curtain in payload.get("curtains", [])]
        self.stars_a, self.stars_b = self._star_layers(int(payload.get("star_seed", 1)),
                                                       int(payload.get("star_count", 180)), 150.0)
        # Curtains hang from the top of the frame and dissolve before the lower third, which
        # keeps the waveform safe area clean.
        self.vfade = (np.clip((0.85 - self.gy) / 1.7, 0.0, 1.0) ** 1.5
                      * np.clip((self.gy + 1.02) / 0.42, 0.0, 1.0)).astype(np.float32)
        self.horizon = (np.clip((self.gy - 0.35) / 0.65, 0.0, 1.0) ** 2).astype(np.float32)[..., None]

    def _paint_aurora(self, t: float, progress: float) -> np.ndarray:
        surge = 0.42 * self._pulse("surge", t, attack=2.2, release=7.5)
        twinkle = 0.72 + 0.28 * math.sin(t * 0.8)
        rgb = self.stars_a * twinkle + self.stars_b * (1.72 - twinkle)
        for curtain in self.curtains:
            ridge = curtain["base"]
            for amplitude, frequency, speed, phase in curtain["waves"]:
                ridge = ridge + amplitude * np.sin(frequency * self.gx + speed * math.tau * t + phase)
            distance = (self.gy - ridge) * curtain["inv_thickness"]
            # Aurora light climbs away from the ridge: a long soft tail up, a hard lower edge.
            above = np.clip(-distance, 0.0, None)
            below = np.clip(distance, 0.0, None)
            body = 1.0 / ((1.0 + above * above * 0.22) * (1.0 + below * below * 7.0))
            first, second, third = curtain["stria"]
            rays = (0.55 * np.sin(first * self.gx + t * 0.21)
                    + 0.30 * np.sin(second * self.gx - t * 0.13 + 1.7)
                    + 0.15 * np.sin(third * self.gx + t * 0.08 + 3.1))
            rays = 0.34 + 0.66 * rays * rays
            veil = body * rays * (curtain["brightness"] * (255.0 * (0.52 + 0.45 * surge))) * self.vfade
            rgb += veil[..., None] * curtain["color"]
        rgb += self.horizon * np.array([6.0, 12.0, 26.0], dtype=np.float32)
        return _glow(rgb, 0.5)

    # ---------------------------------------------------------------- plasma tide

    def _setup_plasma(self) -> None:
        payload = self.payload
        self.axis = [[float(value) for value in term] for term in payload.get("axis", [])]
        self.diagonal = [float(value) for value in payload.get("diagonal", [2.0, 2.0, 0.1, 0.0])]
        self.radial = [float(value) for value in payload.get("radial", [3.0, 0.1])]
        self.bands = int(payload.get("bands", 6))
        cx, cy = (float(value) for value in payload.get("center", [0.0, 0.0]))
        self.radius = np.sqrt((self.gx - cx) ** 2 + (self.gy - cy) ** 2).astype(np.float32)

    def _paint_plasma(self, t: float, progress: float) -> np.ndarray:
        swell = 0.35 * self._pulse("swell", t, attack=3.0, release=9.0)
        (fx, sx, px), (fy, sy, py) = self.axis[0], self.axis[1]
        value = (np.sin(fx * self.gx + sx * math.tau * t + px)
                 + np.sin(fy * self.gy + sy * math.tau * t + py))
        kx, ky, speed, phase = self.diagonal
        value = value + np.sin(kx * self.gx + ky * self.gy + speed * math.tau * t + phase)
        value = value + np.sin(self.radial[0] * self.radius - self.radial[1] * math.tau * t)
        field = value * (0.125 * (1.0 + swell)) + 0.5
        rgb = _shade(field, self.lut)
        # Iso-contours of the same field read as glowing filaments riding the plasma.
        edge = np.abs(np.sin(field * (math.pi * self.bands)))
        edge *= edge
        edge *= edge
        edge *= edge
        rgb += edge[..., None] * (95.0 + 120.0 * swell)
        return _glow(rgb, 0.45)

    # ---------------------------------------------------------------- ripple pond

    def _setup_ripple(self) -> None:
        payload = self.payload
        self.drops = [{"t": float(drop["t"]), "x": float(drop["x"]), "y": float(drop["y"]),
                       "amp": float(drop["amp"]), "speed": float(drop["speed"]),
                       "k": math.tau / max(0.01, float(drop["wavelength"])),
                       "life": float(drop["life"])} for drop in payload.get("drops", [])]
        self.swell = [float(value) for value in payload.get("swell", [1.8, 0.05])]
        self._radius_cache: dict[int, np.ndarray] = {}
        self.depth = (0.55 + 0.45 * np.clip((1.0 - self.gy) / 2.0, 0.0, 1.0)).astype(np.float32)[..., None]

    def _drop_radius(self, index: int) -> np.ndarray:
        cached = self._radius_cache.get(index)
        if cached is None:
            drop = self.drops[index]
            cached = np.sqrt((self.gx - drop["x"]) ** 2 + (self.gy - drop["y"]) ** 2).astype(np.float32)
            self._radius_cache[index] = cached
        return cached

    def _paint_ripple(self, t: float, progress: float) -> np.ndarray:
        surface = (0.16 * np.sin(self.swell[0] * self.gx + self.swell[1] * math.tau * t)
                   * np.sin(self.swell[0] * 0.7 * self.gy - self.swell[1] * math.tau * t * 0.8))
        surface = np.ascontiguousarray(surface, dtype=np.float32)
        for index, drop in enumerate(self.drops):
            age = t - drop["t"]
            if age < 0.0 or age > drop["life"]:
                self._radius_cache.pop(index, None)
                continue
            front = self._drop_radius(index) - drop["speed"] * age
            envelope = (1.0 - age / drop["life"]) / (1.0 + front * front * 70.0)
            surface += (drop["amp"] * envelope) * np.sin(front * drop["k"])
        # Central-difference slopes stand in for a surface normal: a cheap, stable refraction.
        slope = ((np.roll(surface, -1, axis=1) - np.roll(surface, 1, axis=1)) * 2.4
                 + (np.roll(surface, -1, axis=0) - np.roll(surface, 1, axis=0)) * 1.2)
        shade = np.clip(0.44 + slope * 1.5, 0.0, 1.0)
        rgb = _shade(shade, self.lut) * self.depth
        specular = np.clip(shade * 1.4 - 0.75, 0.0, 1.0)
        specular *= specular
        specular *= specular
        rgb += specular[..., None] * np.array([150.0, 192.0, 236.0], dtype=np.float32)
        return _glow(rgb, 0.3)

    # ---------------------------------------------------------------- lumen bloom

    def _setup_bloom(self) -> None:
        self.layers = []
        for order, layer in enumerate(self.payload.get("layers", [])):
            count = int(layer["count"])
            index = np.arange(count, dtype=np.float32)
            self.layers.append({
                "angle": index * GOLDEN_ANGLE + order * 1.7,
                "radius": np.sqrt((index + 0.5) / count).astype(np.float32) * float(layer["radius"]),
                "spin": float(layer["spin"]),
                "tilt": float(layer["tilt"]),
                "phase": (index * 0.37 + order).astype(np.float32),
                # The lower half of a palette is near black, which would hide most seeds.
                "colors": _sample(self.lut, 0.45 + 0.53 * np.mod(index / count + float(layer["hue"]), 1.0)),
                "weight": (float(layer["size"]) * (0.45 + 0.55 * (index / count))).astype(np.float32)[:, None],
            })
        self.breath = float(self.payload.get("breath", 0.08))
        self.backdrop = _shade(np.clip(1.0 - np.sqrt((self.gx / self.aspect) ** 2 + self.gy ** 2), 0.0, 1.0)
                               * 0.32, self.lut) * 0.3

    def _paint_bloom(self, t: float, progress: float) -> np.ndarray:
        stage = self._pulse("bloom", t, attack=3.5, release=14.0)
        grow = (0.85 + 0.22 * _smoothstep(progress * 3.0) + 0.1 * stage) * self.pixel_scale
        breath = 1.0 + self.breath * math.sin(t * 0.33)
        buffer = self.backdrop.copy()
        cx, cy = self.center
        for layer in self.layers:
            angle = layer["angle"] + t * layer["spin"] * math.tau
            radius = layer["radius"] * (grow * breath)
            twinkle = (0.55 + 0.45 * np.sin(t * 0.9 + layer["phase"]))[:, None]
            weight = layer["weight"] * twinkle * (4.5 + 1.4 * stage)
            cos_angle, sin_angle = np.cos(angle), np.sin(angle)
            # Three stops along the same radius turn every dot into a short petal ray.
            for reach, falloff in ((0.88, 0.4), (1.0, 1.0), (1.13, 0.35)):
                _splat(buffer, cx + cos_angle * (radius * reach),
                       cy + sin_angle * (radius * reach) * layer["tilt"],
                       layer["colors"], weight * falloff)
        return _glow(buffer * 1.8, 0.9)

    # ---------------------------------------------------------------- silk current

    def _setup_silk(self) -> None:
        payload = self.payload
        self.terms = [[float(value) for value in term] for term in payload.get("terms", [])]
        count = int(payload.get("particle_count", 3000))
        rng = np.random.default_rng(int(payload.get("particle_seed", 7)))
        self.pos = np.stack([(rng.random(count, dtype=np.float32) * 2.0 - 1.0) * self.aspect,
                             rng.random(count, dtype=np.float32) * 2.0 - 1.0], axis=1)
        self.home = self.pos.copy()
        # Without a finite lifetime every particle settles on a streamline and the field empties.
        self.lifetime = max(2, int(self.fps * 6))
        self.respawn_phase = rng.integers(0, self.lifetime, count).astype(np.int32)
        self.particle_colors = _sample(self.lut, rng.random(count, dtype=np.float32) * 0.92 + 0.08) * 0.45
        self.decay = float(payload.get("decay", 0.93))
        self.trail = np.zeros((self.h, self.w, 3), np.float32)
        self.silk_backdrop = _shade(np.clip(0.16 + 0.1 * self.gy, 0.0, 1.0)
                                    * np.ones_like(self.gx), self.lut) * 0.18

    def _paint_silk(self, t: float, progress: float) -> np.ndarray:
        drift = 0.55 * self._pulse("current", t, attack=4.0, release=11.0)
        x = self.pos[:, 0]
        y = self.pos[:, 1]
        vx = np.zeros_like(x)
        vy = np.zeros_like(y)
        for kx, ky, omega, amplitude, phase in self.terms:
            wave_x = kx * x + omega * math.tau * t
            wave_y = ky * y + phase
            vx += amplitude * -ky * np.sin(wave_x) * np.sin(wave_y)
            vy += amplitude * -kx * np.cos(wave_x) * np.cos(wave_y)
        # A beat rotates the whole field, so the silk visibly changes course instead of looping.
        turn = drift * 0.9
        cos_turn, sin_turn = math.cos(turn), math.sin(turn)
        step_x = (vx * cos_turn - vy * sin_turn) * self.dt * 0.55
        step_y = (vx * sin_turn + vy * cos_turn) * self.dt * 0.55
        self.pos[:, 0] = x + step_x
        self.pos[:, 1] = y + step_y
        # Wrapping would trap particles against the border -- the field is not periodic, so a
        # wrapped particle is pushed straight back out and burns a bright line into the trail.
        escaped = ((np.abs(self.pos[:, 0]) > self.aspect) | (np.abs(self.pos[:, 1]) > 1.0))
        reborn = escaped | (self.respawn_phase == (self._cursor % self.lifetime))
        self.pos[reborn] = self.home[reborn]
        self.trail *= self.decay
        weight = 0.5 + 0.3 * drift
        for back in (0.0, 0.5):
            # Splatting the segment midpoint keeps fast trails continuous instead of dotted.
            _splat(self.trail, ((self.pos[:, 0] - step_x * back) / self.aspect * 0.5 + 0.5) * self.w,
                   ((self.pos[:, 1] - step_y * back) * 0.5 + 0.5) * self.h,
                   self.particle_colors, weight)
        return _glow(self.trail + self.silk_backdrop, 0.85)

    # ---------------------------------------------------------------- starfall warp

    def _setup_warp(self) -> None:
        payload = self.payload
        count = int(payload.get("star_count", 900))
        rng = np.random.default_rng(int(payload.get("star_seed", 11)))
        angle = rng.random(count, dtype=np.float32) * math.tau
        radius = np.sqrt(rng.random(count, dtype=np.float32)) * 0.92 + 0.06
        self.warp_x = np.cos(angle) * radius
        self.warp_y = np.sin(angle) * radius
        self.warp_z = rng.random(count, dtype=np.float32)
        self.warp_colors = _sample(self.lut, rng.random(count, dtype=np.float32) * 0.4 + 0.5)
        self.warp_size = (0.3 + rng.random(count, dtype=np.float32) ** 2 * 1.5)[:, None]
        self.warp_speed = float(payload.get("speed", 0.07))
        self.twist = float(payload.get("twist", 0.0))
        self.comets = [{"t": float(comet["t"]), "angle": float(comet["angle"]),
                        "strength": float(comet["strength"])} for comet in payload.get("comets", [])]
        core = np.clip(1.0 - np.sqrt((self.gx / self.aspect) ** 2 + self.gy ** 2), 0.0, 1.0)
        self.warp_core = (core ** 2.2).astype(np.float32)[..., None] * np.array([9.0, 15.0, 36.0], np.float32)
        # Streaks come from a decaying trail buffer: continuous at any speed, and one splat
        # per frame instead of a dozen samples along each segment.
        self.warp_trail = np.zeros((self.h, self.w, 3), np.float32)
        self.warp_previous: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None

    def _paint_warp(self, t: float, progress: float) -> np.ndarray:
        buffer = self.warp_core.copy()
        cx, cy = self.center
        scale = self.pixel_scale * 0.5
        depth = 1.0 - np.mod(t * self.warp_speed + self.warp_z, 1.0)
        depth = np.maximum(depth, 0.045)
        spin = self.twist * (1.0 - depth) * math.tau
        cos_spin, sin_spin = np.cos(spin), np.sin(spin)
        px = self.warp_x * cos_spin - self.warp_y * sin_spin
        py = self.warp_x * sin_spin + self.warp_y * cos_spin
        brightness = ((1.0 - depth) ** 1.8)[:, None] * self.warp_size
        projection = scale / depth
        screen_x = cx + px * projection
        screen_y = cy + py * projection
        if self.warp_previous is None:
            previous_x, previous_y = screen_x, screen_y
        else:
            previous_x, previous_y, previous_depth = self.warp_previous
            # A star that just wrapped to the back of the field must not draw a streak across it.
            recycled = depth > previous_depth
            previous_x = np.where(recycled, screen_x, previous_x)
            previous_y = np.where(recycled, screen_y, previous_y)
        self.warp_previous = (screen_x, screen_y, depth)
        self.warp_trail *= 0.74
        for blend in (0.34, 0.67, 1.0):
            _splat(self.warp_trail, previous_x + (screen_x - previous_x) * blend,
                   previous_y + (screen_y - previous_y) * blend, self.warp_colors,
                   brightness * (26.0 / 3.0))
        buffer += self.warp_trail
        for comet in self.comets:
            age = t - comet["t"]
            if age < -0.4 or age > 2.2:
                continue
            envelope = comet["strength"] * math.exp(-max(age, 0.0) * 1.6) * _smoothstep((age + 0.4) / 0.4)
            trail = np.linspace(0.12, 1.0, 26, dtype=np.float32)
            reach = self.pixel_scale * 2.2 * trail
            _splat(buffer, cx + math.cos(comet["angle"]) * reach, cy + math.sin(comet["angle"]) * reach,
                   np.tile(np.array([[220.0, 236.0, 255.0]], np.float32), (trail.size, 1)),
                   (trail ** 3 * (60.0 * envelope))[:, None])
        return _glow(buffer, 0.75)


def build_clip(replay, width: int, height: int, fps: int) -> ProceduralClip:
    return ProceduralClip(replay.game_id, replay.payload, replay.duration_seconds, width, height, fps)


__all__ = ["PALETTES", "ProceduralClip", "build_clip", "is_procedural"]
