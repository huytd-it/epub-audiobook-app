"""Shared bookkeeping for the media library (data/backgrounds).

Books and patches reference background files by absolute path - in
book.background_image_path, patch.image_path and the shared video config's
`backgrounds` list. Renaming or deleting a file therefore has to repoint (or
clear) every one of those references, or a later render points at a file that
no longer exists.

Both entry points into the library - the /photos manager and the video config
modal on a book page - go through here so the cleanup can't drift apart.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def referenced_media_paths(conn) -> set[str]:
    """Every background path currently reachable from a book or patch.

    Used by material_cache.gc() to decide what is safe to delete: a cache
    entry that made it into one of these three places - a direct book/patch
    override, or a shared video config's backgrounds list - must survive
    regardless of age or budget, the same way replace_background_reference()
    above has to sweep all three when a manually-managed photo is renamed.
    """
    paths: set[str] = set()
    for row in conn.execute(
        "SELECT background_image_path FROM book WHERE background_image_path IS NOT NULL"
    ).fetchall():
        paths.add(row["background_image_path"])
    for row in conn.execute(
        "SELECT image_path FROM patch WHERE image_path IS NOT NULL"
    ).fetchall():
        paths.add(row["image_path"])
    for row in conn.execute(
        "SELECT automation_config FROM book WHERE automation_config IS NOT NULL"
    ).fetchall():
        try:
            config = json.loads(row["automation_config"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        backgrounds = config.get("video", {}).get("backgrounds")
        if isinstance(backgrounds, list):
            paths.update(path for path in backgrounds if isinstance(path, str))
    return paths


def replace_background_reference(conn, old_path: str, new_path: str | None) -> None:
    """Repoint every reference to old_path at new_path, or drop it when None.

    Does not commit; the caller decides the transaction boundary (renaming
    also has to move the file inside the same lock).
    """
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE book SET background_image_path = ?, updated_at = ? WHERE background_image_path = ?",
        (new_path, now, old_path),
    )
    conn.execute("UPDATE patch SET image_path = ? WHERE image_path = ?", (new_path, old_path))
    for row in conn.execute(
        "SELECT id, automation_config FROM book WHERE automation_config IS NOT NULL"
    ).fetchall():
        try:
            config = json.loads(row["automation_config"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        backgrounds = config.get("video", {}).get("backgrounds")
        if not isinstance(backgrounds, list) or old_path not in backgrounds:
            continue
        if new_path:
            updated = [new_path if path == old_path else path for path in backgrounds]
        else:
            updated = [path for path in backgrounds if path != old_path]
        config["video"]["backgrounds"] = updated
        conn.execute(
            "UPDATE book SET automation_config = ? WHERE id = ?", (json.dumps(config), row["id"])
        )


def delete_background(conn, path: Path) -> None:
    """Clear every reference to a background file, then remove it from disk."""
    replace_background_reference(conn, str(path), None)
    conn.commit()
    path.unlink(missing_ok=True)
