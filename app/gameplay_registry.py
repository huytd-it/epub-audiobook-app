"""Registry and deterministic simulations for versioned gameplay backgrounds."""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass
from typing import Callable

from app.gameplay_models import GameplayReplay


@dataclass(frozen=True)
class GameDefinition:
    id: str
    name: str
    family: str
    simulation_version: str
    renderer_version: str
    waveform_policy: str
    description: str
    simulate: Callable[[int, dict], GameplayReplay]
    sprite_roles: tuple[str, ...] = ()


def _duration(rng: random.Random) -> float:
    return float(rng.randint(180, 300))


def _garden_cycle(seed: int, config: dict) -> GameplayReplay:
    rng = random.Random(seed)
    duration = _duration(rng)
    crops = ("carrot", "wheat", "tomato", "lavender", "pumpkin")
    plots = []
    for row in range(5):
        for column in range(9):
            plots.append({"row": row, "column": column, "crop": rng.choice(crops),
                          "phase": round(rng.uniform(0, 12), 3)})
    route = list(range(len(plots)))
    rng.shuffle(route)
    events = [{"t": 0.0, "type": "start"}]
    interval = max(2.5, (duration - 10.0) / len(route))
    for index, plot_index in enumerate(route):
        events.append({"t": round(5.0 + index * interval, 3), "type": "tend",
                       "plot": plot_index, "stage": index % 4})
    events.append({"t": duration, "type": "result", "plots_tended": len(plots)})
    return GameplayReplay(3, "garden_cycle", seed, duration, 20, "1", "1", "1",
                          {"preset": config.get("preset", "calm"), "plots": plots,
                           "route": route, "events": events},
                          {"status": "complete", "metrics": {"plots_tended": len(plots)}})


def _orbit_drift(seed: int, config: dict) -> GameplayReplay:
    rng = random.Random(seed)
    duration = _duration(rng)
    gates = []
    gate_count = 14
    for index in range(gate_count):
        angle = index * math.tau / gate_count + rng.uniform(-0.18, 0.18)
        radius = rng.uniform(0.25, 0.82)
        gates.append({"x": round(math.cos(angle) * radius, 4),
                      "y": round(math.sin(angle) * radius, 4),
                      "size": round(rng.uniform(0.045, 0.085), 4)})
    stars = [{"x": round(rng.uniform(-1, 1), 4), "y": round(rng.uniform(-1, 1), 4),
              "size": rng.randint(1, 3), "phase": round(rng.random(), 4)} for _ in range(90)]
    events = [{"t": 0.0, "type": "start"}]
    for index in range(gate_count):
        events.append({"t": round((index + 1) * duration / (gate_count + 1), 3),
                       "type": "gate", "gate": index})
    events.append({"t": duration, "type": "result", "gates": gate_count})
    return GameplayReplay(3, "orbit_drift", seed, duration, 20, "1", "1", "1",
                          {"preset": config.get("preset", "calm"), "gates": gates,
                           "stars": stars, "events": events},
                          {"status": "complete", "metrics": {"gates": gate_count}})


def _ambient_game(game_id: str, seed: int, config: dict) -> GameplayReplay:
    rng = random.Random(seed)
    duration = _duration(rng)
    entities = [{"x": round(rng.uniform(-0.9, 0.9), 4), "y": round(rng.uniform(-0.75, 0.75), 4),
                 "speed": round(rng.uniform(0.035, 0.11), 4), "phase": round(rng.random(), 4),
                 "kind": rng.randrange(4)} for _ in range(28 if game_id == "aquarium_ecosystem" else 18)]
    nodes = [{"x": round(rng.uniform(-0.82, 0.82), 4), "y": round(rng.uniform(-0.7, 0.7), 4)}
             for _ in range(12)]
    events = [{"t": 0.0, "type": "start"}]
    for index in range(12):
        events.append({"t": round((index + 1) * duration / 14, 3), "type": "milestone", "index": index})
    events.append({"t": duration, "type": "result", "cycles": 1})
    return GameplayReplay(3, game_id, seed, duration, 20, "1", "1", "1",
                          {"preset": config.get("preset", "calm"), "entities": entities,
                           "nodes": nodes, "events": events},
                          {"status": "complete", "metrics": {"cycles": 1}})


def _ambient(game_id: str) -> Callable[[int, dict], GameplayReplay]:
    return lambda seed, config: _ambient_game(game_id, seed, config)


def _legacy_placeholder(seed: int, config: dict) -> GameplayReplay:
    raise ValueError("battle_royale uses the legacy simulator")


_CROP_ROLES = ("carrot", "wheat", "tomato", "lavender", "pumpkin")
_KIND_ROLES = ("kind_0", "kind_1", "kind_2", "kind_3")
_NODE_ROLES = ("node",)
_ORBIT_ROLES = ("gate", "ship")

_GAMES = {
    game.id: game for game in (
        GameDefinition("garden_cycle", "Garden Cycle", "pixel", "1", "1",
                       "allowed_with_safe_area", "Trồng và thu hoạch một khu vườn yên bình.", _garden_cycle,
                       _CROP_ROLES),
        GameDefinition("orbit_drift", "Orbit Drift", "neon", "1", "1",
                       "default_off", "Trôi qua các cổng sáng trong không gian.", _orbit_drift,
                       _ORBIT_ROLES),
        GameDefinition("aquarium_ecosystem", "Aquarium Ecosystem", "pixel", "1", "1",
                       "default_off", "Đàn cá và cây thủy sinh chuyển động chậm.", _ambient("aquarium_ecosystem"),
                       _KIND_ROLES),
        GameDefinition("marble_flow", "Marble Flow", "neon", "1", "1",
                       "forbidden", "Những viên bi sáng đi qua mạng đường mềm.", _ambient("marble_flow"),
                       _NODE_ROLES),
        GameDefinition("parcel_route", "Parcel Route", "pixel", "1", "1",
                       "allowed_with_safe_area", "Chuyến giao hàng yên bình qua thị trấn.", _ambient("parcel_route"),
                       _KIND_ROLES),
        GameDefinition("territory_bloom", "Territory Bloom", "neon", "1", "1",
                       "default_off", "Các dải màu nở và hòa vào nhau.", _ambient("territory_bloom"),
                       _NODE_ROLES),
        GameDefinition("cloud_runner", "Cloud Runner", "pixel", "1", "1",
                       "allowed_with_safe_area", "Chuyến chạy ngắm cảnh qua mây và đồng cỏ.", _ambient("cloud_runner"),
                       _KIND_ROLES),
        GameDefinition("signal_garden", "Signal Garden", "neon", "1", "1",
                       "default_off", "Mạng nút ánh sáng đồng bộ theo nhịp chậm.", _ambient("signal_garden"),
                       _NODE_ROLES),
        GameDefinition("battle_royale", "Neon Battle Royale", "legacy", "1", "1",
                       "forbidden", "Gameplay Battle Royale tương thích ngược.", _legacy_placeholder),
    )
}


def get_game(game_id: str) -> GameDefinition:
    try:
        return _GAMES[game_id]
    except KeyError as exc:
        raise ValueError(f"unknown gameplay game: {game_id}") from exc


def list_games() -> list[dict]:
    return [{"id": game.id, "name": game.name, "family": game.family,
             "simulation_version": game.simulation_version,
             "renderer_version": game.renderer_version,
             "waveform_policy": game.waveform_policy, "description": game.description,
             "sprite_roles": list(game.sprite_roles),
             "enabled": True} for game in _GAMES.values()]


def simulate_game(game_id: str, seed: int, config: dict | None = None) -> GameplayReplay:
    return get_game(game_id).simulate(seed, config or {})


def resolve_game_id(gameplay: dict, *, book_id: int, patch_id: int, patch_index: int) -> str:
    mode = gameplay.get("selection_mode", "single")
    if mode == "single":
        return str(gameplay.get("game_id") or "garden_cycle")
    game_ids = sorted(dict.fromkeys(str(value) for value in gameplay.get("game_ids", [])))
    if not game_ids:
        raise ValueError("gameplay rotation requires at least one game")
    digest = hashlib.sha256(
        f"gameplay-rotation-v1:{book_id}:{patch_id}:{patch_index}:{','.join(game_ids)}".encode()
    ).digest()
    return game_ids[int.from_bytes(digest[:8], "big") % len(game_ids)]
