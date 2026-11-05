"""Style: simple, no external service required."""
from __future__ import annotations


class BackgroundClipNotFound(Exception):
    pass


# Re-export pool helpers for convenient import from the package.
from app.services.background_video.pool import (  # noqa: E402
    ClipCandidate,
    bg_root,
    keyword_dir,
    list_clips,
    mark_used,
    pick_clips,
    pool_stats,
    save_clip,
    write_manifest,
)

__all__ = [
    "BackgroundClipNotFound",
    "ClipCandidate",
    "bg_root",
    "keyword_dir",
    "list_clips",
    "mark_used",
    "pick_clips",
    "pool_stats",
    "save_clip",
    "write_manifest",
]
