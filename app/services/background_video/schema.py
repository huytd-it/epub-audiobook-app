"""Style: simple, no external service required."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FetchResult:
    keyword: str
    platform: str
    clips_saved: int
    message: str = ""
