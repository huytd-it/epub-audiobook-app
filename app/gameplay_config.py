"""Stable gameplay profile and product constants."""
from __future__ import annotations

import hashlib
import json

from app.config import settings

FIGHTERS_PER_MATCH = 24
MAX_MATCH_SECONDS = 300.0
MIN_MATCH_SECONDS = 180.0
RENDERER_VERSION = settings.gameplay_renderer_version
BUILTIN_THEME_ID = "neon-geometry"
BUILTIN_THEME_VERSION = 1


def profile_key(width: int, height: int, fps: int, themes: list[dict], *, renderer_version: str = RENDERER_VERSION,
                game_id: str | None = None, simulation_version: str = "1",
                ruleset_version: str = "1", quality: int = 23) -> str:
    catalog = sorted((str(t["id"]), int(t["version"]), str(t.get("content_sha256") or "")) for t in themes)
    material = {"schema_version": 2, "game_id": game_id, "width": width, "height": height,
                "fps": fps, "renderer_version": renderer_version,
                "simulation_version": simulation_version, "ruleset_version": ruleset_version,
                "quality": quality, "codec": "libx264", "pixel_format": "yuv420p",
                "themes": catalog}
    length = 32
    raw = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:length]
