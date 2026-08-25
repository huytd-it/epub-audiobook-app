"""Lightweight media browser API: browse folders, preview files, serve thumbnails.

Whitelisted roots keep every request inside the project data tree.  Path
traversal is rejected at two layers: the normalised path must stay inside one
of the allowed directories, and individual name components may not contain
path separators or ``..`` segments.

Root keys are unique identifiers prefixed with ``_`` to avoid collisions when
two physical directories share the same name (e.g. ``uploads`` and the
``uploads`` sub-directory under each book).  All ``path`` values in requests
and responses are qualified as ``<root_key>/<relative>`` so the frontend can
always resolve them unambiguously.
"""
from __future__ import annotations

import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse

from app.config import settings

router = APIRouter(prefix="/api/ui/media-browser", tags=["media-browser"])

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg"}
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".avi", ".mkv"}
AUDIO_EXTS = {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}
_ALL_MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS

# ----------------------------------------------------------------------- #
#  Root registry
# ----------------------------------------------------------------------- #

class _RootEntry:
    __slots__ = ("key", "label", "categories", "exts", "disk_path")

    def __init__(
        self,
        key: str,
        label: str,
        categories: list[str],
        exts: set[str],
        disk_path: Path,
    ) -> None:
        self.key = key
        self.label = label
        self.categories = categories
        self.exts = exts
        self.disk_path = disk_path


def _build_root_entries() -> list[_RootEntry]:
    """Resolved at call time so tests can monkeypatch ``settings.data_root``."""
    data = Path(settings.data_root).resolve()
    app_root = Path(__file__).resolve().parent.parent.parent

    return [
        _RootEntry(
            key="_Nền",
            label="Ảnh nền",
            categories=["backgrounds"],
            exts=IMAGE_EXTS | VIDEO_EXTS,
            disk_path=data / "backgrounds",
        ),
        _RootEntry(
            key="_Sách",
            label="Sách",
            categories=["thumbnails", "videos", "audio"],
            exts=IMAGE_EXTS | VIDEO_EXTS | AUDIO_EXTS,
            disk_path=data / "books",
        ),
        _RootEntry(
            key="_Video",
            label="Video",
            categories=["videos"],
            exts=VIDEO_EXTS,
            disk_path=data / "videos",
        ),
        _RootEntry(
            key="_Nhạc",
            label="Nhạc nền",
            categories=["music"],
            exts=AUDIO_EXTS,
            disk_path=data / "music",
        ),
        _RootEntry(
            key="_Giọng",
            label="Giọng mẫu",
            categories=["voices"],
            exts=AUDIO_EXTS,
            disk_path=data / "voices",
        ),
        _RootEntry(
            key="_Tải lên",
            label="Tải lên",
            categories=["uploads"],
            exts=_ALL_MEDIA_EXTS,
            disk_path=data / "uploads",
        ),
        _RootEntry(
            key="_Hiệu ứng",
            label="Hiệu ứng",
            categories=["effects"],
            exts=AUDIO_EXTS,
            disk_path=data / "effects",
        ),
        _RootEntry(
            key="_Logo",
            label="Logo",
            categories=["logos"],
            exts=IMAGE_EXTS | {".svg"},
            disk_path=app_root / "assets",
        ),
    ]


# ----------------------------------------------------------------------- #
#  Category → allowed extensions
# ----------------------------------------------------------------------- #

_CATEGORY_MAP: dict[str, set[str]] = {
    "thumbnails": IMAGE_EXTS,
    "videos": VIDEO_EXTS,
    "audio": AUDIO_EXTS,
    "backgrounds": IMAGE_EXTS | VIDEO_EXTS,
    "logos": IMAGE_EXTS | {".svg"},
    "voices": AUDIO_EXTS,
    "music": AUDIO_EXTS,
    "uploads": _ALL_MEDIA_EXTS,
    "effects": AUDIO_EXTS,
}


# ----------------------------------------------------------------------- #
#  Path helpers
# ----------------------------------------------------------------------- #


def _resolve_under_root(rel: str, root_disk: Path) -> Path:
    """Resolve *rel* under *root_disk*, raising if it escapes."""
    target = (root_disk / rel).resolve()
    if root_disk.resolve() not in target.parents and target != root_disk.resolve():
        raise HTTPException(status_code=403, detail="Đường dẫn ngoài vùng cho phép")
    return target


def _safe_rel_path(full: Path, roots: list[_RootEntry]) -> tuple[str, str] | None:
    """Return ``(root_key, relative_path)`` or ``None``."""
    full_resolved = full.resolve()
    for root in roots:
        rr = root.disk_path.resolve()
        try:
            rel = full_resolved.relative_to(rr).as_posix()
            return root.key, rel
        except ValueError:
            continue
    return None


# ----------------------------------------------------------------------- #
#  Entry builders
# ----------------------------------------------------------------------- #


def _file_entry(p: Path, roots: list[_RootEntry]) -> dict:
    stat = p.stat()
    ext = p.suffix.lower()
    mime, _ = mimetypes.guess_type(p.name)
    rp = _safe_rel_path(p, roots)
    if rp:
        root_key, rel = rp
        path = f"{root_key}/{rel}"
    else:
        path = p.name
    kind: str
    if ext in IMAGE_EXTS:
        kind = "image"
    elif ext in VIDEO_EXTS:
        kind = "video"
    elif ext in AUDIO_EXTS:
        kind = "audio"
    else:
        kind = "file"
    return {
        "name": p.name,
        "path": path,
        "is_dir": False,
        "size": stat.st_size,
        "modified": stat.st_mtime,
        "ext": ext,
        "mime": mime or "application/octet-stream",
        "kind": kind,
    }


def _dir_entry(p: Path, roots: list[_RootEntry]) -> dict:
    rp = _safe_rel_path(p, roots)
    if rp:
        root_key, rel = rp
        path = f"{root_key}/{rel}"
    else:
        path = p.name
    return {
        "name": p.name,
        "path": path,
        "is_dir": True,
        "size": 0,
        "modified": 0,
        "ext": "",
        "mime": "",
        "kind": "directory",
    }


# ----------------------------------------------------------------------- #
#  API
# ----------------------------------------------------------------------- #


@router.get("/browse")
def browse(
    request: Request,
    path: str = Query(default="", description="Qualified root-relative path (e.g. '_Sách/1/patch_overlays')"),
    category: str = Query(default="", description="Optional category filter"),
    show_hidden: bool = Query(default=False),
):
    """List directories and files under *path*.

    *path* is ``<root_key>/<optional subpath>``.  When empty the top-level
    directories from every existing root are returned.
    """
    roots = _build_root_entries()
    existing_roots = [r for r in roots if r.disk_path.exists()]

    # Empty path → aggregate root entries
    if not path.strip():
        entries: list[dict] = []
        for root in existing_roots:
            entries.append({
                "name": root.label,
                "path": root.key,
                "is_dir": True,
                "size": 0,
                "modified": 0,
                "ext": "",
                "mime": "",
                "kind": "directory",
            })
        return {
            "path": "",
            "entries": sorted(entries, key=lambda e: e["name"]),
            "roots": [r.key for r in existing_roots],
        }

    # Parse qualified path → root_key + sub
    rel = path.strip().strip("/")
    parts = rel.split("/", 1)
    root_key = parts[0]

    target_root = None
    for r in existing_roots:
        if r.key == root_key:
            target_root = r
            break
    if target_root is None:
        raise HTTPException(status_code=404, detail=f"Thư mục '{root_key}' không tồn tại")

    sub = "/".join(parts[1:]) if len(parts) > 1 else ""
    target = _resolve_under_root(sub, target_root.disk_path) if sub else target_root.disk_path.resolve()

    if not target.exists():
        raise HTTPException(status_code=404, detail="Đường dẫn không tồn tại")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Đường dẫn không phải thư mục")

    ext_filter = _CATEGORY_MAP.get(category.lower()) if category else None

    entries = []
    try:
        for item in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if not show_hidden and item.name.startswith("."):
                continue
            if item.is_dir():
                entries.append(_dir_entry(item, roots))
            elif item.is_file():
                if ext_filter and item.suffix.lower() not in ext_filter:
                    continue
                entries.append(_file_entry(item, roots))
    except PermissionError:
        raise HTTPException(status_code=403, detail="Không có quyền truy cập thư mục")

    return {
        "path": f"{root_key}/{sub}" if sub else root_key,
        "entries": entries,
        "roots": [r.key for r in existing_roots],
    }


@router.get("/preview")
def preview(
    request: Request,
    path: str = Query(..., description="Qualified path to file (e.g. '_Nền/sunset.jpg')"),
):
    """Serve a file for in-browser preview (images, video, audio)."""
    roots = _build_root_entries()
    existing_roots = [r for r in roots if r.disk_path.exists()]
    rel = path.strip().strip("/")

    parts = rel.split("/", 1)
    root_key = parts[0]
    target_root = None
    for r in existing_roots:
        if r.key == root_key:
            target_root = r
            break
    if target_root is None:
        raise HTTPException(status_code=404, detail="Thư mục không tồn tại")

    sub = "/".join(parts[1:]) if len(parts) > 1 else ""
    if not sub:
        raise HTTPException(status_code=400, detail="Cần chỉ định file")

    target = _resolve_under_root(sub, target_root.disk_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File không tồn tại")

    mime, _ = mimetypes.guess_type(target.name)
    return FileResponse(str(target), media_type=mime or "application/octet-stream")


@router.get("/info")
def file_info(
    request: Request,
    path: str = Query(..., description="Qualified path to file"),
):
    """Return metadata for a single file (size, mime, kind)."""
    roots = _build_root_entries()
    existing_roots = [r for r in roots if r.disk_path.exists()]
    rel = path.strip().strip("/")
    parts = rel.split("/", 1)
    root_key = parts[0]
    target_root = None
    for r in existing_roots:
        if r.key == root_key:
            target_root = r
            break
    if target_root is None:
        raise HTTPException(status_code=404, detail="Thư mục không tồn tại")

    sub = "/".join(parts[1:]) if len(parts) > 1 else ""
    target = _resolve_under_root(sub, target_root.disk_path) if sub else target_root.disk_path.resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail="Không tìm thấy")
    if target.is_dir():
        raise HTTPException(status_code=400, detail="Đường dẫn là thư mục")

    return _file_entry(target, roots)


@router.get("/resolve")
def resolve_path(
    request: Request,
    path: str = Query(..., description="Relative path to resolve to an absolute filesystem path"),
):
    """Resolve a virtual relative path (as returned by browse) to an absolute
    filesystem path. Used by the branding logo picker so Pillow can load the
    image file. Returns 404 if the file doesn't exist or escapes the roots."""
    existing_roots = _build_root_entries()
    rel = path.strip().strip("/")
    parts = rel.split("/", 1)
    root_key = parts[0]

    target_root = None
    for r in existing_roots:
        if r.key == root_key:
            target_root = r
            break
    if target_root is None:
        raise HTTPException(status_code=404, detail=f"Thư mục '{root_key}' không tồn tại")

    sub = "/".join(parts[1:]) if len(parts) > 1 else ""
    if not sub:
        raise HTTPException(status_code=400, detail="Cần chỉ định file")

    target = _resolve_under_root(sub, target_root.disk_path)
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File không tồn tại")

    return {"path": str(target), "name": target.name, "exists": True}
