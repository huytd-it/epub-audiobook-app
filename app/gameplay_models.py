"""Serializable gameplay domain models."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field


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
    themes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
