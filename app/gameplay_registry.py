"""Registry and deterministic simulations for versioned gameplay backgrounds.

Two asset-free families ship today: ``retro`` (the handheld console games — rắn săn mồi,
xếp gạch, xe tăng and friends) and ``procedural`` (analytic colour fields). Neither needs a
theme pack; ``legacy`` keeps Battle Royale renderable for clips produced before the catalog.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable

from app.gameplay_models import GameplayReplay
from app.gameplay_procedural import PROCEDURAL_GAMES, simulate_procedural
from app.gameplay_retro import RETRO_GAMES, simulate_retro

DEFAULT_GAME_ID = "snake_arena"

# The sprite-hungry pixel/neon families were retired when the retro catalog landed. Books
# configured before that keep rendering: their saved id maps to the closest live game rather
# than failing validation months after the fact.
RETIRED_GAMES = {
    "garden_cycle": "snake_arena",
    "aquarium_ecosystem": "brick_breaker",
    "parcel_route": "pixel_dash",
    "cloud_runner": "pixel_dash",
    "orbit_drift": "star_defender",
    "marble_flow": "brick_breaker",
    "territory_bloom": "snake_arena",
    "signal_garden": "brick_stack",
}


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


def _retro(game_id: str) -> Callable[[int, dict], GameplayReplay]:
    return lambda seed, config: simulate_retro(game_id, seed, config)


def _procedural(game_id: str) -> Callable[[int, dict], GameplayReplay]:
    return lambda seed, config: simulate_procedural(game_id, seed, config)


def _legacy_placeholder(seed: int, config: dict) -> GameplayReplay:
    raise ValueError("battle_royale uses the legacy simulator")


# Neither family declares sprite_roles: every pixel is painted from code, so a theme pack has
# nothing to override and the catalog can never promise art the renderer cannot produce.
_GAMES = {
    game.id: game for game in (
        *(GameDefinition(spec.id, spec.name, "retro", "1", "1", spec.waveform_policy,
                         spec.description, _retro(spec.id)) for spec in RETRO_GAMES),
        *(GameDefinition(spec.id, spec.name, "procedural", "1", "1", spec.waveform_policy,
                         spec.description, _procedural(spec.id)) for spec in PROCEDURAL_GAMES),
        GameDefinition("battle_royale", "Neon Battle Royale", "legacy", "1", "1",
                       "forbidden", "Gameplay Battle Royale tương thích ngược.", _legacy_placeholder),
    )
}


def migrate_game_id(game_id: str) -> str:
    """Resolve a stored game id to one this build can still render."""
    return RETIRED_GAMES.get(game_id, game_id)


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
        return migrate_game_id(str(gameplay.get("game_id") or DEFAULT_GAME_ID))
    game_ids = sorted(dict.fromkeys(migrate_game_id(str(value))
                                    for value in gameplay.get("game_ids", [])))
    if not game_ids:
        raise ValueError("gameplay rotation requires at least one game")
    digest = hashlib.sha256(
        f"gameplay-rotation-v1:{book_id}:{patch_id}:{patch_index}:{','.join(game_ids)}".encode()
    ).digest()
    return game_ids[int.from_bytes(digest[:8], "big") % len(game_ids)]
