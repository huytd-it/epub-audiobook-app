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


def profile_key(width: int, height: int, fps: int, themes: list[dict], *, renderer_version: str = RENDERER_VERSION) -> str:
    catalog = sorted((str(t["id"]), int(t["version"])) for t in themes)
    raw = json.dumps([width, height, fps, renderer_version, catalog], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:24]
