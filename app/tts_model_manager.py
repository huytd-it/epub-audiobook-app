"""Safe, small-scope download manager for local TTS model assets.

Downloads are deliberately separate from synthesis workers: a model update must
never replace files while an audiobook job has the model open.  Only a fixed
catalogue of commands can be started; the API never accepts a shell command or
path from the browser.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from importlib.util import find_spec
from pathlib import Path

from app.config import settings
from app.tts_engine import zerotts_model_dir

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_lock = threading.Lock()
_jobs: dict[str, dict] = {}
# Tiến trình render giọng mẫu sang thư viện offline (riêng với _jobs tải model).
_offline_jobs: dict[str, dict] = {}

# Pip distribution đứng sau mỗi model local. None = checkout repo ngoài pip
# (Confucius4), không có version để hiển thị/cập nhật.
_MODEL_PACKAGE_DISTS: dict[str, str | None] = {
    "voxcpm2": "voxcpm",
    "omnivoice": "omnivoice",
    "confucius4": None,
    "f5-vivoice": "f5-tts",
    "vieneu-fast": "vieneu",
    "zerotts": "zerotts",
    "edge-tts": "edge-tts",
    "gtts": "gtts",
}


def _package_version(dist_name: str | None) -> str | None:
    """Version pip đã cài của model, None khi chưa cài hoặc không qua pip.

    Không bao giờ raise: trang quản lý model phải render được dù môi trường thiếu."""
    if not dist_name:
        return None
    try:
        from importlib.metadata import version

        return version(dist_name)
    except Exception:
        return None


def _dir_size(path: Path) -> int:
    try:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


def _zero_status() -> dict:
    root = zerotts_model_dir()
    ready = (root / "config.json").is_file() and any((root / "voices").glob("*/voice.npz"))
    return {"managed": True, "ready": ready, "path": str(root), "size_bytes": _dir_size(root),
            "detail": "Weights ONNX và voice pack cục bộ.",
            "package": "zerotts", "package_version": _package_version("zerotts")}


def _f5_status() -> dict:
    files = ("model_last.pt", "config.json")
    try:
        from huggingface_hub import try_to_load_from_cache
        cached = [try_to_load_from_cache("hynt/F5-TTS-Vietnamese-ViVoice", name) for name in files]
        ready = all(isinstance(path, str) and Path(path).is_file() for path in cached)
        paths = [Path(path) for path in cached if isinstance(path, str)]
        return {"managed": True, "ready": ready, "path": str(paths[0].parent) if paths else "",
                "size_bytes": sum(path.stat().st_size for path in paths if path.is_file()),
                "detail": "Checkpoint và vocabulary từ Hugging Face cache.",
                "package": "f5-tts", "package_version": _package_version("f5-tts")}
    except ImportError:
        return {"managed": True, "ready": False, "path": "", "size_bytes": 0,
                "detail": "Cần cài extra f5-vivoice trước khi tải weights.",
                "package": "f5-tts", "package_version": _package_version("f5-tts")}


def _confucius_status() -> dict:
    root = Path(settings.confucius4_repo_dir) if settings.confucius4_repo_dir else None
    ready = bool(root and (root / "confuciustts").is_dir())
    return {"managed": False, "ready": ready, "path": str(root) if root else "",
            "size_bytes": _dir_size(root) if root else 0,
            "detail": "Cần checkout upstream và đặt CONFUCIUS4_REPO_DIR; upstream có thêm dependencies/checkpoints.",
            "package": None, "package_version": None}


def _package_status(module: str, extra: str, detail: str, dist: str | None = None) -> dict:
    ready = find_spec(module) is not None
    return {"managed": True, "ready": ready, "path": "", "size_bytes": 0,
            "detail": detail if ready else f"Chưa cài package {extra}. Chọn Tải model để cài đặt.",
            "package": dist, "package_version": _package_version(dist)}


def list_models() -> list[dict]:
    from app.tts_engine import list_tts_models
    statuses = {
        "voxcpm2": _package_status("voxcpm", "tts", "Package VoxCPM2; weights được tải bởi thư viện khi chạy lần đầu.", "voxcpm"),
        "omnivoice": _package_status("omnivoice", "omnivoice", "Package OmniVoice; weights được tải bởi thư viện khi chạy lần đầu.", "omnivoice"),
        "vieneu-fast": _package_status("vieneu", "vieneu-fast", "Package VieNeu và voice presets.", "vieneu"),
        "edge-tts": _package_status("edge_tts", "light-tts", "Dịch vụ Edge TTS trực tuyến.", "edge-tts"),
        "gtts": _package_status("gtts", "light-tts", "Dịch vụ Google Translate TTS trực tuyến.", "gtts"),
        "zerotts": _zero_status(),
        "f5-vivoice": _f5_status(),
        "confucius4": _confucius_status(),
    }
    result = []
    for model in list_tts_models():
        status = statuses.get(model["id"], {"managed": False, "ready": None, "path": "", "size_bytes": 0,
                                                "detail": "Weights được package/model tải theo cơ chế riêng khi chạy.",
                                                "package": model.get("package"),
                                                "package_version": _package_version(model.get("package"))})
        if model.get("capabilities", {}).get("kind") == "api" and model["id"] not in statuses:
            status = {"managed": False, "ready": bool(model.get("configured")), "path": "",
                      "size_bytes": 0, "detail": model.get("config_hint") or "TTS chạy qua API."}
        with _lock:
            job = dict(_jobs.get(model["id"], {}))
        result.append({**model, "install": status, "job": job or None})
    return result


def _commands(model_id: str, update: bool) -> list[list[str]]:
    ensure_pip = [sys.executable, "-m", "ensurepip", "--upgrade"]
    extras = {
        "voxcpm2": "tts",
        "omnivoice": "omnivoice",
        "vieneu-fast": "vieneu-fast",
        "edge-tts": "light-tts",
        "gtts": "light-tts",
    }
    if model_id in extras:
        return [
            ensure_pip,
            [sys.executable, "-m", "pip", "install", *( ["--upgrade"] if update else []), f".[{extras[model_id]}]"],
        ]
    if model_id == "zerotts":
        return [
            ensure_pip,
            [sys.executable, "-m", "pip", "install", *( ["--upgrade"] if update else []), ".[zerotts]"],
            [sys.executable, str(_PROJECT_ROOT / "scripts" / "download_zerotts.py")],
        ]
    if model_id == "f5-vivoice":
        code = (
            "from huggingface_hub import hf_hub_download; "
            "repo='hynt/F5-TTS-Vietnamese-ViVoice'; "
            f"force={update!r}; "
            "[print(hf_hub_download(repo, name, force_download=force)) "
            "for name in ('model_last.pt','config.json')]"
        )
        return [
            ensure_pip,
            [sys.executable, "-m", "pip", "install", *( ["--upgrade"] if update else []), ".[f5-vivoice]"],
            [sys.executable, "-c", code],
        ]
    raise ValueError("Model này không có quy trình tải tự động trong ứng dụng")


def start_download(model_id: str, *, update: bool = False) -> dict:
    commands = _commands(model_id, update)
    with _lock:
        existing = _jobs.get(model_id)
        if existing and existing.get("state") == "running":
            return dict(existing)
        job = {"state": "running", "action": "update" if update else "download", "log": "Đang chuẩn bị...", "returncode": None}
        _jobs[model_id] = job

    def run() -> None:
        try:
            lines: list[str] = []
            code = 0
            for command in commands:
                process = subprocess.Popen(command, cwd=str(_PROJECT_ROOT), stdout=subprocess.PIPE,
                                           stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
                assert process.stdout is not None
                for line in process.stdout:
                    lines.append(line.rstrip())
                    with _lock:
                        job["log"] = "\n".join(lines[-20:])
                code = process.wait()
                if code:
                    break
            with _lock:
                job.update({"state": "done" if code == 0 else "failed", "returncode": code,
                            "log": "\n".join(lines[-20:]) or ("Hoàn tất." if code == 0 else "Tải model thất bại.")})
        except Exception as exc:
            with _lock:
                job.update({"state": "failed", "returncode": -1, "log": str(exc)})

    threading.Thread(target=run, name=f"tts-model-{model_id}", daemon=True).start()
    return dict(job)


# --- Voice-mẫu preview clips -------------------------------------------------
# "Tải danh sách voice mẫu": ZeroTTS publishes one preview.wav per voice on HF,
# so the UI can download just those small clips (KB, not the ~900 MB weights)
# into <data_root>/voice_samples and offer instant listening. VieNeu presets
# ship inside the wheel, so its samples are the catalog entries themselves
# (synthesized on demand via Playground once the package is installed).

SAMPLE_VOICE_MODELS = ("zerotts", "vieneu-fast")


def _sample_voice_dir(model_id: str) -> Path:
    from app.tts_engine import sample_voices_dir

    dest = sample_voices_dir() / model_id
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def list_sample_voices() -> dict:
    """Cached voice-mẫu catalog for instant listening.

    ZeroTTS entries carry ``audio_url`` when their preview.wav is already cached
    (or ships with the weights); VieNeu entries have no pre-rendered clip and
    are auditioned through Playground after install."""
    from app import tts_engine

    groups: dict[str, dict] = {}
    # ZeroTTS: prefer the weights' own cast, else the HF index fetched on demand.
    zerotts_cast = tts_engine._zerotts_voices(tts_engine.zerotts_model_dir())
    if not zerotts_cast:
        zerotts_cast = tts_engine.fetch_zerotts_voice_index()
    zdir = _sample_voice_dir("zerotts")
    zvoices = []
    for voice in zerotts_cast:
        vid = str(voice.get("id") or "").strip()
        if not vid:
            continue
        cached = zdir / f"{vid}.wav"
        shipped = tts_engine.zerotts_model_dir() / "voices" / vid / "preview.wav"
        local = cached if cached.is_file() else (shipped if shipped.is_file() else None)
        zvoices.append({
            "id": vid,
            "label": str(voice.get("label") or vid),
            "language": str(voice.get("language") or ""),
            "cached": local is not None,
            "audio_url": f"/tts-models/sample-voices/file/zerotts/{vid}" if local is not None else None,
        })
    groups["zerotts"] = {
        "model_id": "zerotts",
        "name": "ZeroTTS",
        "count": len(zvoices),
        "cached": sum(1 for v in zvoices if v["cached"]),
        "voices": zvoices,
    }
    # VieNeu V3 Turbo: presets from the installed wheel (no download needed).
    vvoices = [
        {"id": v["id"], "label": v.get("label") or v["id"],
         "language": v.get("language") or "vi", "cached": False, "audio_url": None}
        for v in tts_engine._vieneu_voices()
    ]
    groups["vieneu-fast"] = {
        "model_id": "vieneu-fast",
        "name": "VieNeu V3 Turbo",
        "count": len(vvoices),
        "cached": 0,
        "voices": vvoices,
    }
    return {"models": groups,
            "total": sum(g["count"] for g in groups.values()),
            "cached": sum(g["cached"] for g in groups.values())}


def download_sample_voices(*, timeout: float = 60.0) -> dict:
    """Fetch every ZeroTTS preview.wav (+ index.json) into the sample cache.

    Small files only: voices/index.json (KB) and one preview.wav per voice
    (~100-300 KB each). Returns per-voice status so the UI can show progress."""
    import urllib.request

    from app import tts_engine

    cast = tts_engine._zerotts_voices(tts_engine.zerotts_model_dir())
    if not cast:
        cast = tts_engine.fetch_zerotts_voice_index()
    if not cast:
        raise RuntimeError("Không lấy được danh sách voice ZeroTTS từ máy lẫn Hugging Face")
    dest = _sample_voice_dir("zerotts")
    # Keep a local copy of the index so the list works offline afterwards.
    try:
        url = (f"https://huggingface.co/{tts_engine.ZEROTTS_HF_REPO}/resolve/"
               f"{tts_engine.ZEROTTS_HF_REVISION}/voices/index.json")
        with urllib.request.urlopen(url, timeout=timeout) as response:
            (dest / "index.json").write_bytes(response.read())
    except Exception:
        pass
    items: list[dict] = []
    for voice in cast:
        vid = str(voice.get("id") or "").strip()
        if not vid:
            continue
        target = dest / f"{vid}.wav"
        if target.is_file() and target.stat().st_size > 0:
            items.append({"id": vid, "status": "cached"})
            continue
        shipped = tts_engine.zerotts_model_dir() / "voices" / vid / "preview.wav"
        if shipped.is_file():
            try:
                target.write_bytes(shipped.read_bytes())
                items.append({"id": vid, "status": "cached"})
                continue
            except OSError as exc:
                items.append({"id": vid, "status": "failed", "message": str(exc)})
                continue
        url = (f"https://huggingface.co/{tts_engine.ZEROTTS_HF_REPO}/resolve/"
               f"{tts_engine.ZEROTTS_HF_REVISION}/voices/{vid}/preview.wav")
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:
                target.write_bytes(response.read())
            items.append({"id": vid, "status": "downloaded"})
        except Exception as exc:
            items.append({"id": vid, "status": "failed", "message": str(exc)[:200]})
    ok = sum(1 for i in items if i["status"] in {"cached", "downloaded"})
    return {"model_id": "zerotts", "requested": len(items),
            "cached_or_downloaded": ok, "failed": len(items) - ok, "items": items}


def sample_voice_file(model_id: str, voice_id: str) -> Path:
    """Resolve a cached preview clip, refusing path traversal."""
    if model_id != "zerotts":
        raise ValueError("Chỉ ZeroTTS có file voice mẫu tải sẵn (VieNeu nghe thử qua Playground)")
    safe = "".join(c for c in voice_id if c.isalnum() or c in ("-", "_")).strip()
    if not safe or safe != voice_id:
        raise ValueError("Voice id không hợp lệ")
    from app import tts_engine

    cached = _sample_voice_dir("zerotts") / f"{safe}.wav"
    if cached.is_file():
        return cached
    shipped = tts_engine.zerotts_model_dir() / "voices" / safe / "preview.wav"
    if shipped.is_file():
        return shipped
    raise FileNotFoundError(f"Chưa tải voice mẫu {safe!r}; bấm 'Tải danh sách voice mẫu' trước")


# --- Cấu hình nâng cao theo từng model ---------------------------------------
# Tùy chọn suy luận của từng model local (đúng options_schema của nó) được lưu
# riêng ở <data_root>/tts_model_options.json. TRANG Model TTS dùng chúng khi
# nghe thử ở Playground; pipeline sách/production giữ nguyên tts_options của
# riêng từng sách nên cấu hình ở đây không làm đổi giọng sách đang chạy.

_MODEL_OPTION_MODELS = ("voxcpm2", "omnivoice", "confucius4", "f5-vivoice", "vieneu-fast", "zerotts")


def _model_options_file() -> Path:
    return Path(settings.data_root) / "tts_model_options.json"


def _read_model_options() -> dict:
    path = _model_options_file()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _require_local_model(model_id: str) -> None:
    from app import tts_engine

    if model_id not in tts_engine._MODELS:
        raise KeyError(model_id)


def get_model_options(model_id: str) -> dict:
    """Tùy chọn đã lưu của model, hợp nhất trên schema defaults và đã validate.

    Model không có options_schema trả về {} — dialog khi đó chỉ hiển thị thông tin."""
    from app import tts_engine

    _require_local_model(model_id)
    stored = _read_model_options().get(model_id)
    supplied = stored if isinstance(stored, dict) else {}
    return tts_engine.normalize_tts_options(model_id, supplied)


def save_model_options(model_id: str, raw: object) -> dict:
    """Validate theo options_schema rồi lưu theo model; trả về bản đã chuẩn hóa."""
    from app import tts_engine

    _require_local_model(model_id)
    normalized = tts_engine.normalize_tts_options(model_id, raw)
    options = _read_model_options()
    options[model_id] = normalized
    path = _model_options_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(options, ensure_ascii=False, indent=2), encoding="utf-8")
    return normalized


def download_model_sample_voices(model_id: str = "zerotts", *, timeout: float = 60.0) -> dict:
    """Tải giọng mẫu của RIÊNG một model (nút trên từng card).

    - zerotts: tải preview.wav từng giọng (nhẹ, vài MB).
    - vieneu-fast: presets nằm sẵn trong wheel nên chỉ kiểm tra package và trả
      về danh sách; chưa cài package thì báo lỗi kèm hướng dẫn bấm Tải model."""
    if model_id == "vieneu-fast":
        from app import tts_engine

        presets = tts_engine._vieneu_voices()
        if not presets:
            raise RuntimeError("Chưa cài package vieneu nên chưa có presets. Bấm “Tải model” ở card VieNeu V3 Turbo trước.")
        items = [{"id": v["id"], "status": "preset"} for v in presets]
        return {"model_id": "vieneu-fast", "requested": len(items),
                "cached_or_downloaded": len(items), "failed": 0, "items": items,
                "note": "Presets nằm trong package; nghe thử từng giọng qua Playground."}
    if model_id != "zerotts":
        raise ValueError(f"Model {model_id!r} không có giọng mẫu tải sẵn (chỉ zerotts và vieneu-fast)")
    return download_sample_voices(timeout=timeout)


def _voices_library_dir() -> Path:
    """Thư viện voice offline <data_root>/voices — nơi các model clone lấy clip mẫu."""
    dest = Path(settings.data_root) / "voices"
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _offline_clip_name(model_id: str, voice_id: str) -> str:
    import re

    safe = re.sub(r"[^\w.-]+", "_", voice_id, flags=re.UNICODE).strip("_") or "voice"
    return f"{model_id}__{safe}.wav"


def move_sample_voices_offline(model_id: str) -> dict:
    """Chuyển giọng mẫu của model sang thư viện offline để các model clone lấy so sánh.

    - zerotts: chép preview.wav đã tải (hoặc kèm weights) vào thư viện — xong ngay.
    - vieneu-fast: presets không có file sẵn nên render từng giọng bằng chính
      model rồi lưu vào thư viện — chạy job nền, poll tiến trình qua
      offline_voices_status()."""
    if model_id == "zerotts":
        return _move_zerotts_offline_sync()
    if model_id == "vieneu-fast":
        return {"model_id": model_id, "job": _start_vieneu_offline_job()}
    raise ValueError(f"Model {model_id!r} không có giọng mẫu để chuyển offline (chỉ zerotts và vieneu-fast)")


def offline_voices_status(model_id: str) -> dict | None:
    """Tiến trình job chuyển offline gần nhất của model (None khi chưa từng chạy)."""
    with _lock:
        job = _offline_jobs.get(model_id)
        return dict(job) if job else None


def _move_zerotts_offline_sync() -> dict:
    import shutil

    from app import tts_engine

    cast = tts_engine._zerotts_voices(tts_engine.zerotts_model_dir())
    if not cast:
        cast = tts_engine.fetch_zerotts_voice_index()
    if not cast:
        raise RuntimeError("Chưa có danh sách giọng ZeroTTS. Bấm “Tải giọng mẫu” trước.")
    dest_dir = _voices_library_dir()
    items: list[dict] = []
    for voice in cast:
        vid = str(voice.get("id") or "").strip()
        if not vid:
            continue
        try:
            src = sample_voice_file("zerotts", vid)
        except (ValueError, FileNotFoundError) as exc:
            items.append({"id": vid, "status": "failed", "message": str(exc)[:200]})
            continue
        dest = dest_dir / _offline_clip_name("zerotts", vid)
        if dest.is_file() and dest.stat().st_size > 0:
            items.append({"id": vid, "status": "exists", "file": dest.name})
            continue
        try:
            shutil.copyfile(src, dest)
            items.append({"id": vid, "status": "moved", "file": dest.name})
        except OSError as exc:
            items.append({"id": vid, "status": "failed", "message": str(exc)[:200]})
    ok = sum(1 for i in items if i["status"] in {"moved", "exists"})
    return {"model_id": "zerotts", "requested": len(items), "moved_or_exists": ok,
            "failed": len(items) - ok, "items": items,
            "note": "Các file zerotts__*.wav đã vào thư viện voices; model clone chọn chúng làm clip mẫu."}


def _start_vieneu_offline_job() -> dict:
    with _lock:
        existing = _offline_jobs.get("vieneu-fast")
        if existing and existing.get("state") == "running":
            return dict(existing)
        job: dict = {"state": "running", "model_id": "vieneu-fast", "done": 0, "total": 0,
                     "moved": 0, "exists": 0, "failed": 0, "log": "Đang chuẩn bị..."}
        _offline_jobs["vieneu-fast"] = job

    def run() -> None:
        import soundfile as sf

        from app import tts_engine

        try:
            presets = tts_engine._vieneu_voices()
            if not presets:
                raise RuntimeError("Chưa cài package vieneu. Bấm “Tải model” ở card VieNeu V3 Turbo trước.")
            engine = tts_engine.create_tts_engine("vieneu-fast")
            dest_dir = _voices_library_dir()
            items: list[dict] = []
            with _lock:
                job.update({"total": len(presets), "log": f"Đã tải model, bắt đầu render {len(presets)} giọng..."})
            for index, preset in enumerate(presets, start=1):
                vid = str(preset.get("id") or "").strip()
                if not vid:
                    continue
                dest = dest_dir / _offline_clip_name("vieneu-fast", vid)
                if dest.is_file() and dest.stat().st_size > 0:
                    items.append({"id": vid, "status": "exists", "file": dest.name})
                    with _lock:
                        job.update({"done": index, "exists": job["exists"] + 1,
                                    "log": f"[{index}/{len(presets)}] {vid}: đã có, bỏ qua"})
                    continue
                try:
                    import numpy as _np

                    engine.voice = vid
                    clip = _np.asarray(engine.synthesize_chunk(tts_engine.PRESET_REFERENCE_TEXT),
                                       dtype=_np.float32).reshape(-1)
                    staging = dest.with_name(f"{dest.name}.part")
                    sf.write(str(staging), clip, int(engine.sample_rate), format="WAV")
                    staging.replace(dest)
                    items.append({"id": vid, "status": "moved", "file": dest.name})
                    with _lock:
                        job.update({"done": index, "moved": job["moved"] + 1,
                                    "log": f"[{index}/{len(presets)}] {vid}: đã lưu {dest.name}"})
                except Exception as exc:
                    items.append({"id": vid, "status": "failed", "message": str(exc)[:200]})
                    with _lock:
                        job.update({"done": index, "failed": job["failed"] + 1,
                                    "log": f"[{index}/{len(presets)}] {vid}: lỗi {exc}"})
            failed = sum(1 for i in items if i["status"] == "failed")
            with _lock:
                job.update({"state": "done" if not failed else "failed",
                            "items": items,
                            "log": f"Hoàn tất: {len(items) - failed}/{len(items)} giọng vào thư viện voices."})
        except Exception as exc:
            with _lock:
                job.update({"state": "failed", "log": str(exc)[:500]})

    threading.Thread(target=run, name="tts-offline-voices-vieneu-fast", daemon=True).start()
    return dict(job)
