"""Machine-level runtime settings for local ZeroTTS: CPU or GPU, and thread count.

These describe the hardware, not a book, so they live in one file under data_root
and every ZeroTTS engine (Playground, book jobs, patches) reads them, instead of
riding the per-book ``tts_options`` that travel with a book to other machines.

ZeroTTS is plain ONNX, so "GPU" means an onnxruntime build that has a GPU execution
provider: ``onnxruntime-gpu`` (CUDA, NVIDIA) or ``onnxruntime-directml``. The CPU
``onnxruntime`` the ``zerotts`` extra pulls in has neither, which is why the model
page offers a one-click swap when an NVIDIA card is present.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from importlib import metadata
from pathlib import Path

logger = logging.getLogger(__name__)

DEVICES = ("auto", "cpu", "gpu")
GPU_PROVIDERS = ("CUDAExecutionProvider", "DmlExecutionProvider")
MAX_THREADS = 64
# onnxruntime-gpu 1.24+ is built for CUDA 13, which needs an R580+ driver; older
# drivers get the last CUDA 12 build. Same pin as the Kaggle notebook.
CUDA13_MIN_DRIVER = 580
ORT_GPU_CUDA12 = "onnxruntime-gpu[cuda,cudnn]==1.23.2"
ORT_GPU_CUDA13 = "onnxruntime-gpu[cuda,cudnn]>=1.24"

# Providers the most recently loaded ZeroTTS session actually got. onnxruntime
# falls back to CPU with only a warning when a CUDA library is missing, so the
# requested device alone does not say where synthesis ran.
_last_active: list[str] = []
_last_accelerated = [False]


def _config_file() -> Path:
    from app.config import settings

    return Path(settings.data_root) / "zerotts_runtime.json"


def _max_threads() -> int:
    return max(1, min(MAX_THREADS, os.cpu_count() or 4))


def normalize(raw: object) -> dict:
    from app.config import settings

    supplied = raw if isinstance(raw, dict) else {}
    device = str(supplied.get("device") or "auto")
    if device not in DEVICES:
        device = "auto"
    try:
        threads = int(supplied.get("threads") or settings.zerotts_threads)
    except (TypeError, ValueError):
        threads = settings.zerotts_threads
    return {"device": device, "threads": max(1, min(_max_threads(), threads))}


def load_config() -> dict:
    path = _config_file()
    try:
        stored = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        stored = {}
    return normalize(stored)


def save_config(raw: object) -> dict:
    config = normalize(raw)
    path = _config_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def available_providers() -> list[str]:
    try:
        import onnxruntime as ort
    except ImportError:
        return []
    try:
        return list(ort.get_available_providers())
    except Exception:  # a half-swapped install can import but fail here
        return []


def onnxruntime_builds() -> dict[str, str]:
    """Installed onnxruntime distributions, e.g. {"onnxruntime-gpu": "1.31.1"}."""
    found = {}
    for dist in ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml"):
        try:
            found[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            continue
    return found


def nvidia_gpu() -> dict | None:
    """{"name", "driver", "driver_major"} of the first NVIDIA card, None without one."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=name,driver_version", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    line = out.strip().splitlines()[0] if out.strip() else ""
    if "," not in line:
        return None
    name, driver = (part.strip() for part in line.split(",", 1))
    match = re.match(r"(\d+)", driver)
    return {"name": name, "driver": driver, "driver_major": int(match.group(1)) if match else 0}


def gpu_package_spec(gpu: dict | None) -> str:
    if gpu and gpu.get("driver_major", 0) >= CUDA13_MIN_DRIVER:
        return ORT_GPU_CUDA13
    return ORT_GPU_CUDA12


def resolve_providers(device: str, available: list[str] | None = None) -> list[str]:
    """Execution providers for a ZeroTTS session, best first, CPU always last."""
    available = available_providers() if available is None else available
    gpu = next((p for p in GPU_PROVIDERS if p in available), None)
    if device == "cpu" or gpu is None:
        if device == "gpu":
            logger.warning("ZeroTTS: chọn GPU nhưng onnxruntime không có GPU provider "
                           "(có: %s) — chạy CPU", ", ".join(available) or "không có")
        return ["CPUExecutionProvider"]
    return [gpu, "CPUExecutionProvider"]


def prepare_providers(providers: list[str]) -> None:
    """Point onnxruntime at the CUDA/cuDNN DLLs the nvidia-* wheels installed."""
    if "CUDAExecutionProvider" not in providers:
        return
    try:
        import onnxruntime as ort

        if hasattr(ort, "preload_dlls"):
            ort.preload_dlls()
    except Exception as exc:  # the session itself reports what is really missing
        logger.warning("ZeroTTS: preload_dlls lỗi: %s", exc)


def record_active(providers: list[str], *, accelerated: bool = False) -> None:
    _last_active[:] = list(providers)
    _last_accelerated[0] = accelerated


def status() -> dict:
    available = available_providers()
    gpu = nvidia_gpu()
    builds = onnxruntime_builds()
    config = load_config()
    has_gpu_provider = any(p in available for p in GPU_PROVIDERS)
    return {
        "config": config,
        "max_threads": _max_threads(),
        "cpu_count": os.cpu_count() or 0,
        "available_providers": available,
        "gpu_provider_available": has_gpu_provider,
        "resolved_providers": resolve_providers(config["device"], available),
        "last_active_providers": list(_last_active),
        "last_accelerated": _last_accelerated[0],
        "gpu_graph_built": _gpu_graph_built(),
        "onnxruntime": builds,
        "nvidia_gpu": gpu,
        # Only offer the swap when it can help: an NVIDIA card without a CUDA build.
        "can_install_gpu": bool(gpu) and "CUDAExecutionProvider" not in available,
        "gpu_package": gpu_package_spec(gpu) if gpu else None,
        # Swapping the package does not replace an onnxruntime already imported here.
        "restart_required": "onnxruntime-gpu" in builds and "onnxruntime" in sys.modules
                            and "CUDAExecutionProvider" not in available,
    }


def _gpu_graph_built() -> bool:
    from app.tts_engine import zerotts_model_dir
    from app.zerotts_gpu import built_graph

    try:
        return built_graph(zerotts_model_dir()) is not None
    except OSError:
        return False


def gpu_install_commands(spec: str | None = None) -> list[list[str]]:
    """Replace the CPU onnxruntime with the CUDA build plus its CUDA/cuDNN wheels.

    Both wheels own the same ``onnxruntime/`` package directory, so the CPU one has to
    go first, and the final --no-deps reinstall restores whatever its uninstall deleted
    when onnxruntime-gpu was already present."""
    spec = spec or gpu_package_spec(nvidia_gpu())
    bare = spec.replace("[cuda,cudnn]", "")
    pip = [sys.executable, "-m", "pip"]
    return [
        [*pip, "uninstall", "-y", "onnxruntime"],
        [*pip, "install", "--upgrade", spec, "onnx>=1.16"],
        [*pip, "install", "--force-reinstall", "--no-deps", bare],
    ]
