import io
import json
import sys
import types
import zipfile

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import tts_model_manager, zerotts_runtime
from app.config import settings
from app.routes.tts_models import router
from app.tts_engine import ZeroTTSEngine
from app.zerotts_voices import PACK_NAME, import_voice_pack, pack_bytes, voice_path


def pack(name="my-speaker/", label="Giọng của tôi"):
    latent = io.BytesIO()
    np.savez(latent, voice_emb=np.ones((1, 10, 768), dtype=np.float32),
             n_voice_queries=np.array(10, dtype=np.int64))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(name + "voice.npz", latent.getvalue())
        archive.writestr(name + "meta.json", json.dumps({"display_name": label}, ensure_ascii=False))
        archive.writestr(name + "preview.wav", b"RIFF-not-really")
    return payload.getvalue()


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "zerotts_model_dir", "")
    monkeypatch.setattr(settings, "zerotts_threads", 4)
    root = tmp_path / "zerotts"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"n_voice_queries": 10, "d_model": 768}))
    return root


@pytest.fixture
def client(model):
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client


# --- the imported .zip is kept ------------------------------------------------

def test_import_keeps_original_pack(model):
    original = pack()
    voice = import_voice_pack(original, model)
    assert (voice_path(voice["id"]).parent / PACK_NAME).read_bytes() == original
    payload, filename = pack_bytes(voice["id"])
    assert payload == original and filename == "Giọng của tôi.zip"
    # Re-importing the same speaker never replaces the first saved pack.
    import_voice_pack(pack(name="other/"), model)
    assert pack_bytes(voice["id"])[0] == original


def test_voice_imported_before_packs_were_kept_gets_a_reimportable_zip(model, tmp_path):
    voice = import_voice_pack(pack(label='a/b:c'), model)
    (voice_path(voice["id"]).parent / PACK_NAME).unlink()
    payload, filename = pack_bytes(voice["id"])
    assert filename == "a_b_c.zip"
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert sorted(archive.namelist()) == ["a_b_c/meta.json", "a_b_c/voice.npz"]
    # The rebuilt pack imports back to the very same voice.
    assert import_voice_pack(payload, model)["id"] == voice["id"]


def test_pack_bytes_rejects_unknown_or_unsafe_ids(model):
    for voice_id in ("maichi", "../x", "custom_" + "0" * 64):
        with pytest.raises(FileNotFoundError):
            pack_bytes(voice_id)


def test_pack_download_endpoint(client, model):
    voice = client.post("/tts-models/zerotts/voices/import",
                        files={"file": ("speaker.zip", pack(), "application/zip")}).json()["voice"]
    assert client.get("/tts-models/zerotts/voices").json()["voices"] == [voice]
    response = client.get(f"/tts-models/zerotts/voices/{voice['id']}/pack")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "filename*=UTF-8''Gi%E1%BB%8Dng" in response.headers["content-disposition"]
    assert response.content == pack()
    assert client.get("/tts-models/zerotts/voices/maichi/pack").status_code == 404


# --- CPU / GPU runtime ----------------------------------------------------------

def test_config_defaults_and_clamping(model, monkeypatch):
    monkeypatch.setattr(zerotts_runtime.os, "cpu_count", lambda: 8)
    assert zerotts_runtime.load_config() == {"device": "auto", "threads": 4}
    assert zerotts_runtime.save_config({"device": "gpu", "threads": 99}) == {"device": "gpu", "threads": 8}
    assert zerotts_runtime.load_config() == {"device": "gpu", "threads": 8}
    assert zerotts_runtime.normalize({"device": "tpu", "threads": "x"}) == {"device": "auto", "threads": 4}
    assert zerotts_runtime.normalize({"threads": -3})["threads"] == 1


@pytest.mark.parametrize("device, available, expected", [
    ("auto", ["CUDAExecutionProvider", "CPUExecutionProvider"], ["CUDAExecutionProvider", "CPUExecutionProvider"]),
    ("auto", ["DmlExecutionProvider", "CPUExecutionProvider"], ["DmlExecutionProvider", "CPUExecutionProvider"]),
    ("auto", ["AzureExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"]),
    ("gpu", ["CPUExecutionProvider"], ["CPUExecutionProvider"]),
    ("cpu", ["CUDAExecutionProvider", "CPUExecutionProvider"], ["CPUExecutionProvider"]),
])
def test_resolve_providers(device, available, expected):
    assert zerotts_runtime.resolve_providers(device, available) == expected


@pytest.mark.parametrize("driver_major, spec", [
    (581, zerotts_runtime.ORT_GPU_CUDA13),
    (560, zerotts_runtime.ORT_GPU_CUDA12),
])
def test_gpu_build_follows_driver(driver_major, spec):
    gpu = {"name": "RTX", "driver": f"{driver_major}.1", "driver_major": driver_major}
    assert zerotts_runtime.gpu_package_spec(gpu) == spec
    commands = zerotts_runtime.gpu_install_commands(spec)
    assert commands[0][-3:] == ["uninstall", "-y", "onnxruntime"]
    assert commands[1][-2:] == [spec, "onnx>=1.16"]
    # The final reinstall restores files the CPU wheel's uninstall removed.
    assert "--no-deps" in commands[2] and "[cuda,cudnn]" not in commands[2][-1]


def test_engine_uses_machine_config_and_records_active_provider(model, monkeypatch):
    zerotts_runtime.save_config({"device": "auto", "threads": 3})
    monkeypatch.setattr(zerotts_runtime, "available_providers",
                        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"])
    monkeypatch.setattr(zerotts_runtime, "prepare_providers", lambda providers: None)
    (model / "voices" / "maichi").mkdir(parents=True)
    np.savez(model / "voices" / "maichi" / "voice.npz", voice_emb=np.ones((1, 10, 768), dtype=np.float32))
    seen = {}

    def load(path, **kwargs):
        seen.update(kwargs)
        session = types.SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])
        return types.SimpleNamespace(n_voice_queries=10, prefix_step_sess=session)

    monkeypatch.setitem(sys.modules, "zerotts", types.SimpleNamespace(
        ZeroTTS=types.SimpleNamespace(from_pretrained=load)))
    engine = ZeroTTSEngine(model_dir=str(model))
    engine._ensure_loaded()
    assert seen == {"intra_op_num_threads": 3,
                    "providers": ["CUDAExecutionProvider", "CPUExecutionProvider"]}
    # CUDA was asked for but the session fell back: report where it really ran.
    assert engine.active_providers == ["CPUExecutionProvider"]
    assert zerotts_runtime.status()["last_active_providers"] == ["CPUExecutionProvider"]


def test_runtime_endpoints(client, monkeypatch):
    monkeypatch.setattr(zerotts_runtime, "nvidia_gpu",
                        lambda: {"name": "RTX 3050", "driver": "581.57", "driver_major": 581})
    monkeypatch.setattr(zerotts_runtime, "available_providers", lambda: ["CPUExecutionProvider"])
    status = client.get("/tts-models/zerotts/runtime").json()
    assert status["config"]["device"] == "auto"
    assert status["can_install_gpu"] is True
    assert status["gpu_package"] == zerotts_runtime.ORT_GPU_CUDA13
    assert status["resolved_providers"] == ["CPUExecutionProvider"]
    saved = client.put("/tts-models/zerotts/runtime", json={"device": "cpu", "threads": 2}).json()
    assert saved["config"] == {"device": "cpu", "threads": 2}


def test_gpu_install_job_needs_an_nvidia_card(monkeypatch):
    monkeypatch.setattr(zerotts_runtime, "nvidia_gpu", lambda: None)
    with pytest.raises(ValueError, match="NVIDIA"):
        tts_model_manager._commands("zerotts-gpu", False)
    monkeypatch.setattr(zerotts_runtime, "nvidia_gpu",
                        lambda: {"name": "RTX", "driver": "581.57", "driver_major": 581})
    commands = tts_model_manager._commands("zerotts-gpu", False)
    assert zerotts_runtime.ORT_GPU_CUDA13 in commands[-2]


def test_reinstalling_zerotts_keeps_an_installed_gpu_build(monkeypatch):
    monkeypatch.setattr(zerotts_runtime, "nvidia_gpu", lambda: None)
    monkeypatch.setattr(zerotts_runtime, "onnxruntime_builds", lambda: {"onnxruntime": "1.30.0"})
    plain = tts_model_manager._commands("zerotts", False)
    assert not any("onnxruntime-gpu" in " ".join(c) for c in plain)
    monkeypatch.setattr(zerotts_runtime, "onnxruntime_builds",
                        lambda: {"onnxruntime": "1.30.0", "onnxruntime-gpu": "1.23.2"})
    swapped = tts_model_manager._commands("zerotts", False)
    joined = [" ".join(c) for c in swapped]
    assert any(".[zerotts]" in c for c in joined)
    assert any("uninstall -y onnxruntime" in c for c in joined)
    assert joined.index(next(c for c in joined if ".[zerotts]" in c)) < joined.index(
        next(c for c in joined if "uninstall" in c))
    assert "download_zerotts.py" in joined[-1]


# --- packs mirrored into the repo (committed to git) --------------------------

def test_import_endpoint_copies_pack_into_repo_folder(client, model):
    from app.zerotts_voices import repo_packs_dir

    body = client.post("/tts-models/zerotts/voices/import",
                       files={"file": ("speaker.zip", pack(), "application/zip")}).json()
    short = body["voice"]["id"].removeprefix("custom_")[:12]
    copies = list(repo_packs_dir().glob("*.zip"))
    assert [p.name for p in copies] == [f"Giọng của tôi.{short}.zip"]
    assert copies[0].read_bytes() == pack()
    assert body["repo_pack"] == str(copies[0])


def test_sync_imports_pulled_packs_and_exports_local_ones(model, tmp_path, monkeypatch):
    from app.zerotts_voices import list_voices, repo_packs_dir, sync_repo_packs

    folder = repo_packs_dir()
    folder.mkdir(parents=True, exist_ok=True)
    # Another machine pushed a pack; it was dropped in without the canonical name.
    (folder / "from-git.zip").write_bytes(pack(label="Từ máy khác"))
    (folder / "broken.zip").write_bytes(b"not a zip")
    result = sync_repo_packs(model)
    assert [v["label"] for v in result["imported"]] == ["Từ máy khác"]
    assert [f["file"] for f in result["failed"]] == ["broken.zip"]
    short = result["imported"][0]["id"].removeprefix("custom_")[:12]
    assert sorted(p.name for p in folder.glob("*.zip")) == ["broken.zip", f"from-git.{short}.zip"]
    # Second run: nothing new, the bad pack is not retried until it changes.
    assert sync_repo_packs(model) == {"imported": [], "exported": [], "failed": []}
    assert len(list_voices()) == 1

    # A voice imported before repo copies existed is exported on the next sync.
    for p in folder.glob("from-git.*.zip"):
        p.unlink()
    assert sync_repo_packs(model)["exported"] == [result["imported"][0]["id"]]
    assert len(list(folder.glob(f"*.{short}.zip"))) == 1


def test_sync_waits_for_weights(model):
    from app.zerotts_voices import list_voices, repo_packs_dir, sync_repo_packs

    repo_packs_dir().mkdir(parents=True, exist_ok=True)
    (repo_packs_dir() / "a.zip").write_bytes(pack())
    (model / "config.json").unlink()
    assert sync_repo_packs(model)["failed"] == []
    (model / "config.json").write_text(json.dumps({"n_voice_queries": 10, "d_model": 768}))
    assert len(sync_repo_packs(model)["imported"]) == 1
    assert len(list_voices()) == 1
