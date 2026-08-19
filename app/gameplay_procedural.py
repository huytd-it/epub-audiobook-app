"""Deterministic parameter generators for the asset-free procedural gameplay family.

These games ship no PNG assets at all: every frame is painted from analytic wave fields,
particle glow and palette LUTs (see :mod:`app.gameplay_effects`). The simulation only freezes
the parameters and the visual beats, so a clip re-renders identically on retry.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass

from app.gameplay_models import GameplayReplay

SCHEMA_VERSION = 3
TICK_RATE = 20

# Curtain colours are frozen into the replay so two seeds of the same game look different.
AURORA_COLORS = ((72, 255, 176), (86, 176, 255), (188, 120, 255), (255, 148, 196), (128, 255, 232))
PLASMA_PALETTES = ("ember", "abyss", "orchid", "jade")
WATER_PALETTES = ("lagoon", "abyss", "moonlit")
BLOOM_PALETTES = ("spectrum", "orchid", "ember")
SILK_PALETTES = ("spectrum", "jade", "orchid")
WARP_PALETTES = ("starlight", "ember", "abyss")


@dataclass(frozen=True)
class ProceduralSpec:
    id: str
    name: str
    waveform_policy: str
    description: str


PROCEDURAL_GAMES = (
    ProceduralSpec("aurora_veil", "Aurora Veil", "allowed_with_safe_area",
                   "Rèm cực quang trôi trên nền sao, sáng dần theo từng đợt."),
    ProceduralSpec("plasma_tide", "Plasma Tide", "default_off",
                   "Sóng plasma giao thoa với các đường viền phát sáng."),
    ProceduralSpec("ripple_pond", "Ripple Pond", "allowed_with_safe_area",
                   "Mặt nước tĩnh với những vòng sóng lan và ánh khúc xạ."),
    ProceduralSpec("lumen_bloom", "Lumen Bloom", "default_off",
                   "Hoa ánh sáng xoè theo dãy Fibonacci, xoay chậm và đổi sắc."),
    ProceduralSpec("silk_current", "Silk Current", "default_off",
                   "Hàng nghìn hạt sáng chảy theo dòng xoáy, để lại vệt lụa."),
    ProceduralSpec("starfall_warp", "Starfall Warp", "forbidden",
                   "Bay xuyên trường sao với vệt kéo dài và sao chổi."),
)

PROCEDURAL_IDS = frozenset(spec.id for spec in PROCEDURAL_GAMES)


def _duration(rng: random.Random) -> float:
    return float(rng.randint(180, 300))


def _beats(rng: random.Random, duration: float, kind: str, spacing: float) -> list[dict]:
    """Evenly spread visual beats with jittered strength; the renderer reads them as envelopes."""
    count = max(3, int(duration // spacing))
    return [{"t": round((index + 1) * duration / (count + 1), 3), "type": kind, "index": index,
             "strength": round(rng.uniform(0.45, 1.0), 3)} for index in range(count)]


def _replay(game_id: str, seed: int, duration: float, payload: dict, metrics: dict) -> GameplayReplay:
    return GameplayReplay(SCHEMA_VERSION, game_id, seed, duration, TICK_RATE, "1", "1", "1",
                          payload, {"status": "complete", "metrics": metrics})


def _aurora_veil(seed: int, config: dict) -> GameplayReplay:
    rng = random.Random(seed)
    duration = _duration(rng)
    curtains = []
    # Analogous neighbours, two or three of them: more curtains than that sum to white.
    for index in range(rng.randint(2, 3)):
        curtains.append({
            "color": list(AURORA_COLORS[(seed + index) % len(AURORA_COLORS)]),
            "base": round(rng.uniform(-0.30, 0.28), 4),
            "thickness": round(rng.uniform(0.16, 0.34), 4),
            "brightness": round(rng.uniform(0.55, 1.0), 4),
            "shimmer": round(rng.uniform(9.0, 26.0), 3),
            "waves": [[round(rng.uniform(0.05, 0.20), 4), round(rng.uniform(0.5, 2.6), 4),
                       round(rng.uniform(0.02, 0.09), 4), round(rng.uniform(0, math.tau), 4)]
                      for _ in range(3)],
        })
    events = [{"t": 0.0, "type": "start"}, *_beats(rng, duration, "surge", 34.0)]
    surges = len(events) - 1
    events.append({"t": duration, "type": "result", "surges": surges})
    payload = {"preset": config.get("preset", "calm"), "curtains": curtains,
               "star_seed": rng.randrange(1 << 30), "star_count": rng.randint(160, 240),
               "events": events}
    return _replay("aurora_veil", seed, duration, payload, {"surges": surges})


def _plasma_tide(seed: int, config: dict) -> GameplayReplay:
    rng = random.Random(seed)
    duration = _duration(rng)
    payload = {
        "preset": config.get("preset", "calm"),
        # Two separable axis terms stay 1-D at render time; only the diagonal and radial terms
        # cost a full-field sine, which keeps the per-frame budget flat.
        "axis": [[round(rng.uniform(1.6, 4.4), 4), round(rng.uniform(0.05, 0.19), 4),
                  round(rng.uniform(0, math.tau), 4)] for _ in range(2)],
        "diagonal": [round(rng.uniform(1.2, 3.0), 4), round(rng.uniform(1.2, 3.0), 4),
                     round(rng.uniform(0.04, 0.15), 4), round(rng.uniform(0, math.tau), 4)],
        "radial": [round(rng.uniform(2.4, 5.6), 4), round(rng.uniform(0.06, 0.2), 4)],
        "center": [round(rng.uniform(-0.45, 0.45), 4), round(rng.uniform(-0.35, 0.35), 4)],
        "palette": rng.choice(PLASMA_PALETTES),
        "bands": rng.randint(4, 8),
        "events": [{"t": 0.0, "type": "start"}],
    }
    payload["events"].extend(_beats(rng, duration, "swell", 28.0))
    swells = len(payload["events"]) - 1
    payload["events"].append({"t": duration, "type": "result", "swells": swells})
    return _replay("plasma_tide", seed, duration, payload, {"swells": swells})


def _ripple_pond(seed: int, config: dict) -> GameplayReplay:
    rng = random.Random(seed)
    duration = _duration(rng)
    drops: list[dict] = []
    t = 2.5
    while t < duration - 6.0:
        drops.append({"t": round(t, 3), "x": round(rng.uniform(-0.9, 0.9), 4),
                      "y": round(rng.uniform(-0.85, 0.85), 4),
                      "amp": round(rng.uniform(0.55, 1.0), 4),
                      "speed": round(rng.uniform(0.10, 0.18), 4),
                      # Wavelengths below ~0.1 alias into moire once the field is upscaled.
                      "wavelength": round(rng.uniform(0.11, 0.2), 4),
                      "life": round(rng.uniform(6.0, 9.5), 3)})
        t += rng.uniform(3.5, 6.5)
    events = [{"t": 0.0, "type": "start"}]
    events.extend({"t": drop["t"], "type": "drop", "index": index} for index, drop in enumerate(drops))
    events.append({"t": duration, "type": "result", "drops": len(drops)})
    payload = {"preset": config.get("preset", "calm"), "drops": drops,
               "palette": rng.choice(WATER_PALETTES),
               "swell": [round(rng.uniform(1.2, 2.6), 4), round(rng.uniform(0.03, 0.08), 4)],
               "events": events}
    return _replay("ripple_pond", seed, duration, payload, {"drops": len(drops)})


def _lumen_bloom(seed: int, config: dict) -> GameplayReplay:
    rng = random.Random(seed)
    duration = _duration(rng)
    layers = [{"count": rng.randint(190, 340), "radius": round(rng.uniform(0.75, 1.15), 4),
               "spin": round(rng.uniform(0.015, 0.075), 4) * rng.choice((-1, 1)),
               "hue": round(rng.random(), 4), "size": round(rng.uniform(0.9, 1.9), 3),
               "tilt": round(rng.uniform(0.78, 1.0), 3)} for _ in range(3)]
    events = [{"t": 0.0, "type": "start"}, *_beats(rng, duration, "bloom", 30.0)]
    stages = len(events) - 1
    events.append({"t": duration, "type": "result", "stages": stages})
    payload = {"preset": config.get("preset", "calm"), "layers": layers,
               "palette": rng.choice(BLOOM_PALETTES),
               "breath": round(rng.uniform(0.045, 0.11), 4),
               "events": events}
    return _replay("lumen_bloom", seed, duration, payload, {"stages": stages})


def _silk_current(seed: int, config: dict) -> GameplayReplay:
    rng = random.Random(seed)
    duration = _duration(rng)
    payload = {
        "preset": config.get("preset", "calm"),
        # Stream-function terms; the renderer takes their analytic curl so the flow never
        # collapses into sinks and the trails stay silky.
        "terms": [[round(rng.uniform(0.9, 2.6), 4), round(rng.uniform(0.9, 2.6), 4),
                   round(rng.uniform(0.05, 0.22), 4), round(rng.uniform(0.25, 0.7), 4),
                   round(rng.uniform(0, math.tau), 4)] for _ in range(3)],
        "particle_seed": rng.randrange(1 << 30),
        "particle_count": rng.choice((2600, 3200, 4000)),
        "decay": round(rng.uniform(0.93, 0.975), 4),
        "palette": rng.choice(SILK_PALETTES),
        "events": [{"t": 0.0, "type": "start"}],
    }
    payload["events"].extend(_beats(rng, duration, "current", 32.0))
    shifts = len(payload["events"]) - 1
    payload["events"].append({"t": duration, "type": "result", "shifts": shifts})
    return _replay("silk_current", seed, duration, payload, {"shifts": shifts})


def _starfall_warp(seed: int, config: dict) -> GameplayReplay:
    rng = random.Random(seed)
    duration = _duration(rng)
    comets = [{"t": round(value, 3), "angle": round(rng.uniform(0, math.tau), 4),
               "strength": round(rng.uniform(0.6, 1.0), 3)}
              for value in sorted(rng.uniform(8.0, duration - 8.0) for _ in range(rng.randint(4, 7)))]
    events = [{"t": 0.0, "type": "start"}]
    events.extend({"t": comet["t"], "type": "comet", "index": index} for index, comet in enumerate(comets))
    events.append({"t": duration, "type": "result", "comets": len(comets)})
    payload = {"preset": config.get("preset", "calm"), "star_seed": rng.randrange(1 << 30),
               "star_count": rng.randint(720, 1100),
               "speed": round(rng.uniform(0.045, 0.095), 4),
               "twist": round(rng.uniform(-0.22, 0.22), 4),
               "palette": rng.choice(WARP_PALETTES), "comets": comets, "events": events}
    return _replay("starfall_warp", seed, duration, payload, {"comets": len(comets)})


_SIMULATORS = {
    "aurora_veil": _aurora_veil,
    "plasma_tide": _plasma_tide,
    "ripple_pond": _ripple_pond,
    "lumen_bloom": _lumen_bloom,
    "silk_current": _silk_current,
    "starfall_warp": _starfall_warp,
}


def simulate_procedural(game_id: str, seed: int, config: dict | None = None) -> GameplayReplay:
    try:
        simulator = _SIMULATORS[game_id]
    except KeyError as exc:
        raise ValueError(f"unknown procedural game: {game_id}") from exc
    return simulator(seed, config or {})


__all__ = ["PROCEDURAL_GAMES", "PROCEDURAL_IDS", "ProceduralSpec", "simulate_procedural"]
