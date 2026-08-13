"""Content-addressed cache for generated/fetched background material.

Sourcing a background from a remote API (Pollinations, Pexels, ...) is slow
and, for paid or rate-limited providers, not free. Keying the cache off the
request that produced a file (source + model + prompt + size) rather than a
book or patch lets identical requests - the common case when re-rendering a
patch after a text edit, or reusing a prompt across chapters - skip the
network entirely.

Files live under data/backgrounds/cache/<key[:2]>/<key><ext>, sharded by the
first two hex characters so the directory never has to hold tens of thousands
of entries flat. That path is a subdirectory of data/backgrounds, but every
listing endpoint over that folder (routes/video.py's /video/backgrounds,
routes/ui_api.py's /media) iterates it non-recursively and filters to files
with an allowed suffix, so a nested "cache" directory is invisible to them
without any extra exclusion. routes/photos.py's rename/delete reject any name
containing a path separator, so those can't reach into it either.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.media_library import referenced_media_paths

logger = logging.getLogger(__name__)


def make_key(source: str, prompt: str, width: int, height: int, model: str = "") -> str:
    """Stable identity for a cached request.

    Same truncated-sha256 convention as app.text_analysis.text_hash, just
    longer (24 hex chars) since collisions here would silently serve the wrong
    image for a different prompt, not just waste a cache slot.
    """
    raw = f"{source}|{model}|{width}x{height}|{prompt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _cache_root() -> Path:
    root = Path(settings.data_root) / "backgrounds" / "cache"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _cache_path(cache_key: str, ext: str) -> Path:
    shard = _cache_root() / cache_key[:2]
    shard.mkdir(parents=True, exist_ok=True)
    return shard / f"{cache_key}{ext}"


def get(conn: sqlite3.Connection, cache_key: str) -> Path | None:
    """Return the cached file, or None on a miss.

    A row whose file is gone (deleted by hand, or a GC run that raced a
    concurrent renderer) is treated as a miss and the stale row is dropped, so
    it doesn't keep shadowing future put() calls for the same key.
    """
    row = conn.execute(
        "SELECT file_path FROM material_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    if row is None:
        return None
    path = Path(row["file_path"])
    if not path.is_file():
        conn.execute("DELETE FROM material_cache WHERE cache_key = ?", (cache_key,))
        conn.commit()
        return None
    conn.execute(
        "UPDATE material_cache SET last_used_at = ?, use_count = use_count + 1 WHERE cache_key = ?",
        (datetime.now(timezone.utc).isoformat(), cache_key),
    )
    conn.commit()
    return path


def put(
    conn: sqlite3.Connection, cache_key: str, data: bytes, *,
    source: str, prompt: str, width: int, height: int, ext: str = ".png",
) -> Path:
    """Write data to the cache and record it, atomically.

    The write lands in a temp file in the same shard directory and is moved
    into place with os.replace(), which is atomic on both POSIX and Windows.
    A concurrent get() for the same key therefore either misses cleanly (the
    temp file isn't visible under the real name yet) or reads a complete
    file - never a partial one. render jobs commonly run at concurrency > 1
    (see video_config.py's "concurrency" setting), so this isn't a corner case.
    """
    dest = _cache_path(cache_key, ext)
    tmp = dest.parent / f"{dest.stem}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}{ext}"
    tmp.write_bytes(data)
    os.replace(tmp, dest)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO material_cache
               (cache_key, source, prompt, file_path, file_size, width, height,
                created_at, last_used_at, use_count)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
           ON CONFLICT(cache_key) DO UPDATE SET
               file_path = excluded.file_path,
               file_size = excluded.file_size,
               last_used_at = excluded.last_used_at""",
        (cache_key, source, prompt, str(dest), len(data), width, height, now, now),
    )
    conn.commit()
    return dest


def gc(conn: sqlite3.Connection, *, max_age_days: int | None = None, max_bytes: int | None = None) -> int:
    """Remove cache entries that are safe to remove, oldest-used first.

    An entry currently referenced by any book's background list, background
    image, or patch image - see referenced_media_paths - is never removed
    even if it is old or the budget is exceeded: it doesn't matter whether a
    background reached that list via a manual upload or a cache write, once a
    book depends on it, deleting the file would break that book's next
    render.

    max_age_days and max_bytes are independent triggers; either can remove an
    entry, and both are best-effort - if every removable entry is younger than
    max_age_days, max_bytes may still leave the cache over budget rather than
    touch a referenced file.
    """
    referenced = referenced_media_paths(conn)
    rows = conn.execute(
        "SELECT cache_key, file_path, file_size, last_used_at FROM material_cache ORDER BY last_used_at ASC"
    ).fetchall()
    total_bytes = sum(row["file_size"] or 0 for row in rows)
    now = datetime.now(timezone.utc)
    removed = 0
    for row in rows:
        if row["file_path"] in referenced:
            continue
        stale = False
        if max_age_days is not None:
            try:
                last_used = datetime.fromisoformat(row["last_used_at"])
            except ValueError:
                last_used = now
            stale = (now - last_used).days >= max_age_days
        over_budget = max_bytes is not None and total_bytes > max_bytes
        if not (stale or over_budget):
            continue
        Path(row["file_path"]).unlink(missing_ok=True)
        conn.execute("DELETE FROM material_cache WHERE cache_key = ?", (row["cache_key"],))
        total_bytes -= row["file_size"] or 0
        removed += 1
    conn.commit()
    return removed
