"""Serializable gameplay domain models."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class GameplayReplay:
    schema_version: int
    game_id: str
    seed: int
    duration_seconds: float
    tick_rate: int
    simulation_version: str
    ruleset_version: str
    renderer_version: str
    payload: dict
    result: dict

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Fighter:
    key: str
    name: str
    class_name: str


@dataclass(frozen=True)
class Replay:
    seed: int
    duration_seconds: float
    roster: list[dict]
    themes: list[dict]
    map: dict
    events: list[dict]
    top3: list[str]
    winner_key: str

    def to_dict(self) -> dict:
        return asdict(self)
