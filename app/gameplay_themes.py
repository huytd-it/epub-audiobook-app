"""Secure Army Theme Pack import and prompt generation."""
from __future__ import annotations

import io
import json
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

from PIL import Image

ASSETS = ("tank.png", "assassin.png", "ranger.png", "heal.png", "speed.png",
          "damage.png", "projectile.png", "elimination.png")
ALLOWED = {"theme.json", *ASSETS}
TARGET_SIZE = (256, 256)


def theme_prompt(army_theme: str, unit_class: str = "tank, assassin, ranger",
                 colors: str = "cyan, magenta, electric lime") -> str:
    return (f"Army theme: {army_theme}. Unit class: {unit_class}. Colors: {colors}. "
            "Top-down 2D game sprite, transparent alpha, centered, no text, no watermark, "
            "no background, clear silhouette, consistent lighting and proportions. "
            "Create matching tank, assassin, ranger, heal, speed, damage, projectile, and elimination assets.")


def _safe_members(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members = {}
    for info in archive.infolist():
        path = PurePosixPath(info.filename.replace("\\", "/"))
        if info.is_dir():
            continue
        if path.is_absolute() or ".." in path.parts or len(path.parts) != 1 or path.name not in ALLOWED:
            raise ValueError(f"invalid theme pack path: {info.filename}")
        if info.file_size > 8 * 1024 * 1024:
            raise ValueError(f"theme asset too large: {path.name}")
        if path.name in members:
            raise ValueError(f"duplicate theme asset: {path.name}")
        members[path.name] = info
    missing = ALLOWED - set(members)
    if missing:
        raise ValueError(f"theme pack missing: {', '.join(sorted(missing))}")
    return members


def install_theme_zip(data: bytes, root: str | Path) -> dict:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid ZIP") from exc
    with archive:
        members = _safe_members(archive)
        try:
            manifest = json.loads(archive.read(members["theme.json"]))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid theme.json") from exc
        theme_id = str(manifest.get("id", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,47}", theme_id):
            raise ValueError("theme id must be a short lowercase slug")
        version = manifest.get("version")
        if type(version) is not int or version < 1:
            raise ValueError("theme version must be a positive integer")
        target = Path(root) / theme_id / str(version)
        temp = target.with_name(target.name + ".installing")
        if temp.exists():
            shutil.rmtree(temp)
        temp.mkdir(parents=True)
        try:
            for filename in ASSETS:
                with Image.open(io.BytesIO(archive.read(members[filename]))) as image:
                    image.load()
                    if image.format != "PNG" or "A" not in image.getbands():
                        raise ValueError(f"{filename} must be a PNG with alpha")
                    image.thumbnail(TARGET_SIZE, Image.Resampling.LANCZOS)
                    canvas = Image.new("RGBA", TARGET_SIZE)
                    canvas.alpha_composite(image.convert("RGBA"), ((256-image.width)//2, (256-image.height)//2))
                    canvas.save(temp / filename, "PNG")
            (temp / "theme.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            temp.replace(target)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
    return {"id": theme_id, "version": version, "name": str(manifest.get("name") or theme_id),
            "manifest": manifest, "asset_dir": str(target)}
