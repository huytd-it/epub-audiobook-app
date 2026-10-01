"""Import ZeroWeight voice packs without loading ONNX or changing model assets."""
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

import numpy as np

MAX_PACK_BYTES = 20 * 1024 * 1024
MAX_LATENT_BYTES = 1024 * 1024


def voices_dir() -> Path:
    from app.config import settings

    return Path(settings.data_root) / "zerotts_custom_voices"


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
    (dest / "meta.json").write_text(json.dumps({"display_name": label, "language": voice["language"]}, ensure_ascii=False), encoding="utf-8")
    return voice
