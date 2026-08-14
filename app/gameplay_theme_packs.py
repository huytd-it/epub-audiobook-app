"""Secure, immutable Pixel/Neon gameplay theme-pack importer."""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from PIL import Image

MAX_ENTRIES = 128
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_PIXELS = 16_000_000


def _slug(value: object) -> str:
    result = str(value or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,47}", result):
        raise ValueError("theme id must be a short lowercase slug")
    return result


def install_theme_pack_zip(data: bytes, root: str | Path) -> dict:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ValueError("invalid ZIP") from exc
    with archive:
        infos = [info for info in archive.infolist() if not info.is_dir()]
        if len(infos) > MAX_ENTRIES:
            raise ValueError("theme pack has too many files")
        members: dict[str, zipfile.ZipInfo] = {}
        total = 0
        for info in infos:
            path = PurePosixPath(info.filename.replace("\\", "/"))
            mode = info.external_attr >> 16
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"invalid theme pack path: {info.filename}")
            if stat.S_ISLNK(mode):
                raise ValueError(f"theme pack symlink is not allowed: {info.filename}")
            normalized = path.as_posix()
            key = normalized.casefold()
            if key in members:
                raise ValueError(f"duplicate theme asset: {normalized}")
            if path.suffix.lower() not in {".json", ".png"}:
                raise ValueError(f"unsupported theme asset: {normalized}")
            if info.file_size > MAX_FILE_BYTES:
                raise ValueError(f"theme asset too large: {normalized}")
            total += info.file_size
            if total > MAX_TOTAL_BYTES:
                raise ValueError("theme pack expands beyond the size limit")
            if info.compress_size and info.file_size / info.compress_size > 200:
                raise ValueError(f"theme asset compression ratio is unsafe: {normalized}")
            members[key] = info
        manifest_info = members.get("theme.json")
        if manifest_info is None:
            raise ValueError("theme pack missing theme.json")
        try:
            manifest = json.loads(archive.read(manifest_info))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("invalid theme.json") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 2:
            raise ValueError("theme.json must use schema_version 2")
        theme_id = _slug(manifest.get("id"))
        version = manifest.get("version")
        if type(version) is not int or version < 1:
            raise ValueError("theme version must be a positive integer")
        family = manifest.get("family")
        if family not in {"pixel", "neon"}:
            raise ValueError("theme family must be pixel or neon")
        supported = manifest.get("supported_games")
        if not isinstance(supported, dict) or not supported:
            raise ValueError("theme pack must declare supported_games")
        referenced: set[str] = set()
        for game_id, contract in supported.items():
            if not isinstance(game_id, str) or not isinstance(contract, dict):
                raise ValueError("invalid supported game contract")
            assets = contract.get("assets", {})
            if not isinstance(assets, dict):
                raise ValueError("theme assets must be an object")
            for role, filename in assets.items():
                if not isinstance(role, str) or not isinstance(filename, str):
                    raise ValueError("invalid theme asset role")
                normalized = PurePosixPath(filename.replace("\\", "/")).as_posix()
                if normalized.casefold() not in members or not normalized.lower().endswith(".png"):
                    raise ValueError(f"missing referenced theme asset: {filename}")
                referenced.add(normalized.casefold())
        digest = hashlib.sha256()
        for key in sorted(members):
            content = archive.read(members[key])
            digest.update(key.encode()); digest.update(b"\0"); digest.update(content)
            if key.endswith(".png") and key in referenced:
                with Image.open(io.BytesIO(content)) as image:
                    if image.format != "PNG" or image.width * image.height > MAX_PIXELS:
                        raise ValueError(f"invalid or oversized PNG: {members[key].filename}")
                    image.verify()
        content_sha256 = digest.hexdigest()
        target = Path(root) / theme_id / str(version)
        if target.exists():
            existing_manifest = target / "theme.json"
            if existing_manifest.is_file():
                try:
                    existing = json.loads(existing_manifest.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    existing = {}
                if existing.get("content_sha256") != content_sha256:
                    raise ValueError("theme id/version is immutable; upload a new version")
            return {"id": theme_id, "version": version, "name": str(manifest.get("name") or theme_id),
                    "family": family, "manifest": existing, "content_sha256": content_sha256,
                    "asset_dir": str(target)}
        temp = target.with_name(target.name + ".installing")
        shutil.rmtree(temp, ignore_errors=True)
        temp.mkdir(parents=True)
        try:
            stored_manifest = {**manifest, "content_sha256": content_sha256}
            for key, info in members.items():
                if key == "theme.json":
                    continue
                destination = temp / PurePosixPath(info.filename.replace("\\", "/")).as_posix()
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(info))
            (temp / "theme.json").write_text(json.dumps(stored_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            temp.replace(target)
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise
    return {"id": theme_id, "version": version, "name": str(manifest.get("name") or theme_id),
            "family": family, "manifest": stored_manifest, "content_sha256": content_sha256,
            "asset_dir": str(target)}
