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
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings

MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".mp4", ".webm", ".mov"}
# Subdirectories of data/backgrounds owned by the app itself (material_cache,
# background_gen) - never offered as, or usable as, a user folder.
RESERVED_FOLDERS = {"cache", "patch_bg"}


def backgrounds_root() -> Path:
    """Resolved at call time (not import time) so tests can repoint data_root."""
    return Path(settings.data_root) / "backgrounds"


def clean_folder_name(name: str) -> str:
    """Sanitize a user folder name to one safe path component; raises ValueError."""
    cleaned = re.sub(r"[^\w\- ]", "", (name or "").strip()).strip()
    if not cleaned or cleaned.lower() in RESERVED_FOLDERS:
        raise ValueError("invalid folder name")
    return cleaned


def folder_path(name: str) -> Path | None:
    """Directory of an existing user folder, or None for anything else
    (traversal, reserved names, missing)."""
    if not name or name != Path(name).name or name.startswith(".") or name.lower() in RESERVED_FOLDERS:
        return None
    path = backgrounds_root() / name
    return path if path.is_dir() else None


def list_folders() -> list[str]:
    root = backgrounds_root()
    if not root.is_dir():
        return []
    return sorted(
        (p.name for p in root.iterdir() if p.is_dir() and folder_path(p.name)), key=str.lower)


def folder_files(name: str) -> list[Path]:
    """Media files directly inside a user folder, in name order."""
    path = folder_path(name)
    if path is None:
        return []
    return sorted(
        (p for p in path.iterdir() if p.is_file() and p.suffix.lower() in MEDIA_EXTENSIONS),
        key=lambda p: p.name.lower())


def expand_backgrounds(config: dict | None) -> list[str]:
    """The video config's background files: the explicit `backgrounds` list
    followed by everything inside each selected `background_folders` entry.

    Folders are read at call time, so files added to a folder later are picked
    up by the next render without touching the config."""
    config = config or {}
    paths = [p for p in config.get("backgrounds") or [] if isinstance(p, str)]
    for name in config.get("background_folders") or []:
        if isinstance(name, str):
            paths.extend(str(p) for p in folder_files(name))
    return list(dict.fromkeys(paths))


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


def delete_folder(conn, name: str) -> None:
    """Remove a user folder with its files, clearing every reference to them
    and dropping the folder from the video configs that selected it."""
    path = folder_path(name)
    if path is None:
        return
    for file in path.iterdir():
        if file.is_file():
            replace_background_reference(conn, str(file), None)
    for row in conn.execute(
        "SELECT id, automation_config FROM book WHERE automation_config IS NOT NULL"
    ).fetchall():
        try:
            config = json.loads(row["automation_config"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        folders = config.get("video", {}).get("background_folders")
        if not isinstance(folders, list) or name not in folders:
            continue
        config["video"]["background_folders"] = [f for f in folders if f != name]
        conn.execute(
            "UPDATE book SET automation_config = ? WHERE id = ?", (json.dumps(config), row["id"])
        )
    conn.commit()
    shutil.rmtree(path, ignore_errors=True)


def delete_background(conn, path: Path) -> None:
    """Clear every reference to a background file, then remove it from disk."""
    replace_background_reference(conn, str(path), None)
    conn.commit()
    path.unlink(missing_ok=True)
