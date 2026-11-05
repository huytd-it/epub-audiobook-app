"""Style: simple, no external service required."""
from __future__ import annotations


class DtkError(RuntimeError):
    pass


class DtkNotReady(DtkError):
    pass
