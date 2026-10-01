import io
import json
import zipfile
import sys
import types

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.routes.tts_models import router
from app.tts_engine import ZeroTTSEngine, list_tts_models
from app.zerotts_voices import import_voice_pack, list_voices, voice_path


def pack(shape=(1, 10, 768), name="my-speaker/", **arrays):
    latent = io.BytesIO()
    np.savez(latent, voice_emb=np.ones(shape, dtype=np.float32),
             n_voice_queries=np.array(shape[1], dtype=np.int64), **arrays)
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(name + "voice.npz", latent.getvalue())
        archive.writestr(name + "meta.json", json.dumps({"display_name": "Giọng của tôi"}, ensure_ascii=False))
    return payload.getvalue()


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "zerotts_model_dir", "")
    root = tmp_path / "zerotts"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"n_voice_queries": 10, "d_model": 768}))
    return root


def test_import_persists_in_catalog_and_engine_loads_latents(model):
    voice = import_voice_pack(pack(), model)
    assert voice["label"] == "Giọng của tôi"
    assert list_voices() == [voice]
    catalog = next(m for m in list_tts_models() if m["id"] == "zerotts")
    assert voice in catalog["voices"]
    emb = ZeroTTSEngine(voice=voice["id"], model_dir=str(model))._load_voice_emb(10)
    assert emb.shape == (1, 10, 768)
    assert import_voice_pack(pack(), model) == voice
    assert len(list_voices()) == 1
    assert not (model / "voices").exists()


@pytest.mark.parametrize("shape", [(1, 9, 768), (1, 10, 512), (2, 10, 768)])
def test_rejects_wrong_model_latents(model, shape):
    with pytest.raises(ValueError, match="không khớp"):
        import_voice_pack(pack(shape), model)
    assert list_voices() == []


@pytest.mark.parametrize("name", ["../escape/", "/absolute/", "C:/escape/", "..\\escape/"])
def test_rejects_unsafe_archive_paths(model, name):
    with pytest.raises(ValueError, match="Đường dẫn"):
        import_voice_pack(pack(name=name), model)
    assert list_voices() == []


def test_rejects_invalid_zip_and_missing_weights(model):
    with pytest.raises(ValueError):
        import_voice_pack(b"not a zip", model)
    (model / "config.json").unlink()
    with pytest.raises(ValueError, match="tải model"):
        import_voice_pack(pack(), model)


def test_upload_endpoint(model):
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post("/tts-models/zerotts/voices/import", files={"file": ("speaker.zip", pack(), "application/zip")})
        assert response.status_code == 200
        assert voice_path(response.json()["voice"]["id"]).is_file()
        assert client.post("/tts-models/zerotts/voices/import", files={"file": ("clip.wav", b"fake")}).status_code == 400
        assert client.post("/tts-models/zerotts/voices/import", files={"file": ("bad.zip", b"fake")}).status_code == 400


def test_old_weights_report_update_instead_of_unpack_error(model, monkeypatch):
    def load(*args, **kwargs):
        raise ValueError("not enough values to unpack (expected 4, got 3)")

    monkeypatch.setitem(sys.modules, "zerotts", types.SimpleNamespace(
        ZeroTTS=types.SimpleNamespace(from_pretrained=load)))
    engine = ZeroTTSEngine(model_dir=str(model))
    with pytest.raises(RuntimeError, match="Cập nhật model"):
        engine._ensure_loaded()
    assert engine._model is None


def test_unrelated_loading_error_is_preserved(model, monkeypatch):
    def load(*args, **kwargs):
        raise ValueError("invalid model config")

    monkeypatch.setitem(sys.modules, "zerotts", types.SimpleNamespace(
        ZeroTTS=types.SimpleNamespace(from_pretrained=load)))
    with pytest.raises(ValueError, match="invalid model config"):
        ZeroTTSEngine(model_dir=str(model))._ensure_loaded()


def test_downloader_uses_same_revision_as_engine():
    from app.tts_engine import ZEROTTS_HF_REVISION
    from scripts import download_zerotts

    assert download_zerotts.REVISION == ZEROTTS_HF_REVISION
