"""Import ZeroWeight voice packs without loading ONNX or changing model assets.

Imported voices live under data_root (git-ignored), so each pack is also copied into
a folder inside the repo (``voice_packs/zerotts`` by default). A pack is ~1 MB, small
enough to commit: push it, pull on another machine, and ``sync_repo_packs`` imports it
there on startup or when the model page opens."""
import hashlib
import io
import json
import logging
import re
import zipfile
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

MAX_PACK_BYTES = 20 * 1024 * 1024
MAX_LATENT_BYTES = 1024 * 1024
PACK_NAME = "pack.zip"
# "<label>.<first 12 hex of the voice id>.zip": readable, and the suffix says which
# voice a repo pack already is without opening it.
_REPO_NAME = re.compile(r"\.([0-9a-f]{12})\.zip$")
# Repo packs that failed to import, keyed by (path, mtime), so a bad file is reported
# once instead of on every sync; editing or replacing it retries.
_failed: set[tuple[str, float]] = set()


def voices_dir() -> Path:
    from app.config import settings

    return Path(settings.data_root) / "zerotts_custom_voices"


def repo_packs_dir() -> Path:
    from app.config import _APP_ROOT, settings

    return Path(settings.zerotts_voice_packs_dir or _APP_ROOT / "voice_packs" / "zerotts")


def _short(voice_id: str) -> str:
    return voice_id.removeprefix("custom_")[:12]


def save_repo_copy(voice_id: str) -> Path | None:
    """Copy an imported voice's pack into the repo folder (once); returns its path."""
    folder = repo_packs_dir()
    existing = next(iter(sorted(folder.glob(f"*.{_short(voice_id)}.zip"))), None) if folder.is_dir() else None
    if existing:
        return existing
    try:
        payload, filename = pack_bytes(voice_id)
    except FileNotFoundError:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"{filename.removesuffix('.zip')}.{_short(voice_id)}.zip"
    dest.write_bytes(payload)
    return dest


def sync_repo_packs(model_dir: Path) -> dict:
    """Import repo packs this machine does not have yet, and copy local-only voices
    into the repo folder. Safe to call often: known packs are skipped by file name."""
    folder = repo_packs_dir()
    result = {"imported": [], "exported": [], "failed": []}
    known = {_short(v["id"]) for v in list_voices()}
    for path in sorted(folder.glob("*.zip")) if folder.is_dir() else []:
        match = _REPO_NAME.search(path.name)
        if match and match.group(1) in known:
            continue
        key = (str(path), path.stat().st_mtime)
        if key in _failed:
            continue
        try:
            if path.stat().st_size > MAX_PACK_BYTES:
                raise ValueError("Gói giọng tối đa 20 MB.")
            voice = import_voice_pack(path.read_bytes(), model_dir)
        except ValueError as exc:
            if not (model_dir / "config.json").exists():
                break  # no weights yet: retry once they are downloaded
            _failed.add(key)
            logger.warning("ZeroTTS: bỏ qua gói giọng %s: %s", path.name, exc)
            result["failed"].append({"file": path.name, "error": str(exc)})
            continue
        known.add(_short(voice["id"]))
        result["imported"].append(voice)
        # A pack dropped in by hand gets the canonical name, so the export pass below
        # does not write a second copy of it.
        canonical = path.with_name(f"{path.name.removesuffix('.zip')}.{_short(voice['id'])}.zip")
        if not match and not any(folder.glob(f"*.{_short(voice['id'])}.zip")):
            path.rename(canonical)
    for voice in list_voices():
        if folder.is_dir() and any(folder.glob(f"*.{_short(voice['id'])}.zip")):
            continue
        if save_repo_copy(voice["id"]):
            result["exported"].append(voice["id"])
    return result


def pack_bytes(voice_id: str) -> tuple[bytes, str]:
    """(.zip bytes, download filename) for an imported voice.

    The saved original pack when there is one; voices imported before packs were kept
    get an equivalent rebuilt from voice.npz + meta.json, which imports back to the
    same voice id."""
    npz = voice_path(voice_id)
    if npz is None or not npz.is_file():
        raise FileNotFoundError(voice_id)
    try:
        meta = json.loads((npz.parent / "meta.json").read_text(encoding="utf-8"))
        label = str(meta.get("display_name") or "") if isinstance(meta, dict) else ""
    except (OSError, ValueError):
        meta, label = {}, ""
    safe = "".join(c if c.isprintable() and c not in '\\/:*?"<>|' else "_" for c in label)
    stem = safe.strip(" ._") or voice_id[:20]
    original = npz.parent / PACK_NAME
    if original.is_file():
        return original.read_bytes(), f"{stem}.zip"
    rebuilt = io.BytesIO()
    with zipfile.ZipFile(rebuilt, "w", zipfile.ZIP_DEFLATED) as pack:
        pack.write(npz, f"{stem}/voice.npz")
        pack.writestr(f"{stem}/meta.json", json.dumps(meta if isinstance(meta, dict) else {},
                                                      ensure_ascii=False))
    return rebuilt.getvalue(), f"{stem}.zip"


def voice_path(voice_id: str) -> Path | None:
    if not re.fullmatch(r"custom_[0-9a-f]{64}", voice_id):
        return None
    return voices_dir() / voice_id / "voice.npz"


def list_voices() -> list[dict]:
    result = []
    for path in sorted(voices_dir().glob("custom_*/meta.json")):
        if voice_path(path.parent.name) != path.parent / "voice.npz":
            continue
        if not (path.parent / "voice.npz").is_file():
            continue
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
            result.append({"id": path.parent.name, "label": str(meta["display_name"]),
                           "language": str(meta.get("language") or "vi")})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def import_voice_pack(payload: bytes, model_dir: Path) -> dict:
    if len(payload) > MAX_PACK_BYTES:
        raise ValueError("Gói giọng tối đa 20 MB.")
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as pack:
            members = pack.infolist()
            if len(members) > 32 or sum(m.file_size for m in members) > MAX_PACK_BYTES:
                raise ValueError("Gói giọng quá lớn hoặc chứa quá nhiều file.")
            # Never extract archive paths. Only copy the explicitly validated latents.
            for member in members:
                parts = member.filename.replace("\\", "/").split("/")
                if member.filename.startswith(("/", "\\")) or ".." in parts or ":" in member.filename:
                    raise ValueError("Đường dẫn trong gói giọng không hợp lệ.")
            candidates = [m for m in members if m.filename.endswith("/voice.npz") or m.filename == "voice.npz"]
            if len(candidates) != 1 or candidates[0].file_size > MAX_LATENT_BYTES:
                raise ValueError("Gói phải chứa đúng một voice.npz (tối đa 1 MB).")
            latent = pack.read(candidates[0])
            prefix = candidates[0].filename.removesuffix("voice.npz")
            meta = json.loads(pack.read(prefix + "meta.json").decode("utf-8")) if prefix + "meta.json" in pack.namelist() else {}
            if not isinstance(meta, dict):
                raise ValueError("meta.json phải là một object.")
        # An NPZ is itself a zip: bound its expanded size before NumPy reads arrays.
        with zipfile.ZipFile(io.BytesIO(latent)) as arrays:
            if sum(m.file_size for m in arrays.infolist()) > MAX_LATENT_BYTES:
                raise ValueError("Dữ liệu giọng quá lớn.")
        with np.load(io.BytesIO(latent), allow_pickle=False) as data:
            emb = data["voice_emb"]
            queries = data["n_voice_queries"]
            config = json.loads((model_dir / "config.json").read_text(encoding="utf-8"))
            expected = (1, int(config["n_voice_queries"]), int(config["d_model"]))
            if (emb.dtype != np.float32 or emb.shape != expected or not np.isfinite(emb).all()
                    or queries.shape != () or queries.dtype != np.int64 or int(queries) != expected[1]):
                raise ValueError(f"Voice pack không khớp model ZeroTTS; cần latents {expected} float32.")
            normalized = io.BytesIO()
            np.savez(normalized, voice_emb=emb, n_voice_queries=queries)
    except FileNotFoundError as exc:
        raise ValueError("Cần tải model ZeroTTS trước khi nhập giọng.") from exc
    except (zipfile.BadZipFile, KeyError, TypeError, UnicodeError, RuntimeError, EOFError) as exc:
        raise ValueError("Gói giọng ZeroTTS không hợp lệ.") from exc
    voice_id = "custom_" + hashlib.sha256(emb.tobytes()).hexdigest()
    dest = voices_dir() / voice_id
    label = str(meta.get("display_name") or prefix.strip("/").split("/")[-1] or "Giọng của tôi")[:120]
    voice = {"id": voice_id, "label": label, "language": str(meta.get("language") or "vi")[:20]}
    dest.mkdir(parents=True, exist_ok=True)
    # Immutable identity: importing the same speaker is idempotent and cannot replace presets.
    if not (dest / "voice.npz").exists():
        (dest / "voice.npz").write_bytes(normalized.getvalue())
    # Keep the pack exactly as downloaded so it can be backed up or moved to another
    # machine; the first copy of a speaker wins, like the latents themselves.
    if not (dest / PACK_NAME).exists():
        (dest / PACK_NAME).write_bytes(payload)
    (dest / "meta.json").write_text(json.dumps({"display_name": label, "language": voice["language"]}, ensure_ascii=False), encoding="utf-8")
    return voice
