"""Safe, small-scope download manager for local TTS model assets.

Downloads are deliberately separate from synthesis workers: a model update must
never replace files while an audiobook job has the model open.  Only a fixed
catalogue of commands can be started; the API never accepts a shell command or
path from the browser.
"""
from __future__ import annotations

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


def _dir_size(path: Path) -> int:
    try:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


def _zero_status() -> dict:
    root = zerotts_model_dir()
    ready = (root / "config.json").is_file() and any((root / "voices").glob("*/voice.npz"))
    return {"managed": True, "ready": ready, "path": str(root), "size_bytes": _dir_size(root),
            "detail": "Weights ONNX và voice pack cục bộ."}


def _f5_status() -> dict:
    files = ("model_last.pt", "config.json")
    try:
        from huggingface_hub import try_to_load_from_cache
        cached = [try_to_load_from_cache("hynt/F5-TTS-Vietnamese-ViVoice", name) for name in files]
        ready = all(isinstance(path, str) and Path(path).is_file() for path in cached)
        paths = [Path(path) for path in cached if isinstance(path, str)]
        return {"managed": True, "ready": ready, "path": str(paths[0].parent) if paths else "",
                "size_bytes": sum(path.stat().st_size for path in paths if path.is_file()),
                "detail": "Checkpoint và vocabulary từ Hugging Face cache."}
    except ImportError:
        return {"managed": True, "ready": False, "path": "", "size_bytes": 0,
                "detail": "Cần cài extra f5-vivoice trước khi tải weights."}


def _confucius_status() -> dict:
    root = Path(settings.confucius4_repo_dir) if settings.confucius4_repo_dir else None
    ready = bool(root and (root / "confuciustts").is_dir())
    return {"managed": False, "ready": ready, "path": str(root) if root else "",
            "size_bytes": _dir_size(root) if root else 0,
            "detail": "Cần checkout upstream và đặt CONFUCIUS4_REPO_DIR; upstream có thêm dependencies/checkpoints."}


def _package_status(module: str, extra: str, detail: str) -> dict:
    ready = find_spec(module) is not None
    return {"managed": True, "ready": ready, "path": "", "size_bytes": 0,
            "detail": detail if ready else f"Chưa cài package {extra}. Chọn Tải model để cài đặt."}


def list_models() -> list[dict]:
    from app.tts_engine import list_tts_models
    statuses = {
        "voxcpm2": _package_status("voxcpm", "tts", "Package VoxCPM2; weights được tải bởi thư viện khi chạy lần đầu."),
        "omnivoice": _package_status("omnivoice", "omnivoice", "Package OmniVoice; weights được tải bởi thư viện khi chạy lần đầu."),
        "vieneu-fast": _package_status("vieneu", "vieneu-fast", "Package VieNeu và voice presets."),
        "edge-tts": _package_status("edge_tts", "light-tts", "Dịch vụ Edge TTS trực tuyến."),
        "gtts": _package_status("gtts", "light-tts", "Dịch vụ Google Translate TTS trực tuyến."),
        "zerotts": _zero_status(),
        "f5-vivoice": _f5_status(),
        "confucius4": _confucius_status(),
    }
    result = []
    for model in list_tts_models():
        status = statuses.get(model["id"], {"managed": False, "ready": None, "path": "", "size_bytes": 0,
                                                "detail": "Weights được package/model tải theo cơ chế riêng khi chạy."})
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
