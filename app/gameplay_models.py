"""Serializable gameplay domain models."""
from __future__ import annotations

from dataclasses import asdict, dataclass


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
