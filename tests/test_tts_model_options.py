"""Per-model TTS actions: version info, advanced options, sample download, offline move."""
import json
import time

import numpy as np
import pytest

from app import tts_model_manager
from app.config import settings


def _seed_zerotts_weights(root, voices=("maichi", "baotrang")):
    voices_dir = root / "zerotts" / "voices"
    entries = []
    for name in voices:
        voice_dir = voices_dir / name
        voice_dir.mkdir(parents=True, exist_ok=True)
        (voice_dir / "preview.wav").write_bytes(b"RIFFfake" + name.encode())
        entries.append({"name": name, "display_name": name.title(), "description": "giong mau"})
    (voices_dir / "index.json").write_text(
        json.dumps({"voices": entries}, ensure_ascii=False), encoding="utf-8")


def test_install_status_carries_package_version_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    for model in tts_model_manager.list_models():
        assert "package" in model["install"]
        assert "package_version" in model["install"]


def test_model_options_roundtrip_and_clamp(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    assert tts_model_manager.get_model_options("f5-vivoice")["speed"] == 1.0
    saved = tts_model_manager.save_model_options(
        "f5-vivoice", {"speed": 99, "device": "cuda", "bogus": 1})
    assert saved == {"speed": 2.0, "nfe_step": 32, "cfg_strength": 2.0,
                     "sway_sampling_coef": -1.0, "device": "cuda"}
    assert tts_model_manager.get_model_options("f5-vivoice") == saved


def test_model_options_empty_schema_and_unknown_model(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    assert tts_model_manager.get_model_options("zerotts") == {}
    assert tts_model_manager.save_model_options("zerotts", {"speed": 5}) == {}
    with pytest.raises(KeyError):
        tts_model_manager.get_model_options("nope")
    with pytest.raises(KeyError):
        tts_model_manager.save_model_options("nope", {})


def test_per_model_sample_download_rejects_models_without_cast(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    with pytest.raises(ValueError, match="không có giọng mẫu"):
        tts_model_manager.download_model_sample_voices("voxcpm2")


def test_vieneu_sample_download_reports_presets(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr("app.tts_engine._vieneu_voices",
                        lambda: [{"id": "Adam", "label": "Adam", "language": "vi"}])
    result = tts_model_manager.download_model_sample_voices("vieneu-fast")
    assert result["requested"] == 1 and result["failed"] == 0
    assert result["items"] == [{"id": "Adam", "status": "preset"}]


def test_vieneu_sample_download_needs_package(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr("app.tts_engine._vieneu_voices", lambda: [])
    with pytest.raises(RuntimeError, match="package vieneu"):
        tts_model_manager.download_model_sample_voices("vieneu-fast")


def test_zerotts_offline_move_copies_then_reports_exists(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    _seed_zerotts_weights(tmp_path)
    first = tts_model_manager.move_sample_voices_offline("zerotts")
    assert first["moved_or_exists"] == 2 and first["failed"] == 0
    assert all(i["status"] == "moved" for i in first["items"])
    assert (tmp_path / "voices" / "zerotts__maichi.wav").is_file()
    second = tts_model_manager.move_sample_voices_offline("zerotts")
    assert all(i["status"] == "exists" for i in second["items"])


def test_zerotts_offline_move_needs_cast_first(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr("app.tts_engine.fetch_zerotts_voice_index", lambda **kwargs: [])
    with pytest.raises(RuntimeError, match="Tải giọng mẫu"):
        tts_model_manager.move_sample_voices_offline("zerotts")


def test_vieneu_offline_job_renders_each_preset(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr("app.tts_engine._vieneu_voices",
                        lambda: [{"id": "Adam", "label": "Adam", "language": "vi"},
                                 {"id": "My Duyen", "label": "My Duyen", "language": "vi"}])

    class FakeEngine:
        sample_rate = 48000

        def __init__(self, *args, **kwargs):
            self.voice = None

        def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None):
            assert text and self.voice
            return np.zeros(480, dtype=np.float32)

    monkeypatch.setattr("app.tts_engine.create_tts_engine", lambda *a, **k: FakeEngine())
    started = tts_model_manager.move_sample_voices_offline("vieneu-fast")
    assert started["job"]["state"] == "running"
    deadline = time.time() + 15
    while time.time() < deadline:
        status = tts_model_manager.offline_voices_status("vieneu-fast")
        if status and status["state"] != "running":
            break
        time.sleep(0.1)
    final = tts_model_manager.offline_voices_status("vieneu-fast")
    assert final["state"] == "done"
    assert (tmp_path / "voices" / "vieneu-fast__Adam.wav").is_file()
    assert (tmp_path / "voices" / "vieneu-fast__My_Duyen.wav").is_file()


def test_offline_move_rejects_models_without_cast(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    with pytest.raises(ValueError, match="không có giọng mẫu"):
        tts_model_manager.move_sample_voices_offline("voxcpm2")
    assert tts_model_manager.offline_voices_status("zerotts") is None
