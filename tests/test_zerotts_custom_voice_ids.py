"""Short slug ids for new ZeroTTS imports, legacy custom_<sha256> compatibility,
custom voices in the sample list, and the once-only cached preview clip."""
import hashlib
import io
import json
import shutil
import time
import zipfile

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import tts_model_manager
from app.config import settings
from app.routes.tts_models import router
from app.tts_engine import ZeroTTSEngine
from app.zerotts_voices import (delete_voice, import_voice_pack, list_voices, pack_bytes, save_repo_copy,
                                slugify_voice_name, sync_repo_packs, unique_voice_id, update_voice,
                                voice_path, voices_dir)


def make_pack(display_name="Mai Chi", fill=1.0, folder="speaker/"):
    latent = io.BytesIO()
    np.savez(latent, voice_emb=np.full((1, 10, 768), fill, dtype=np.float32),
             n_voice_queries=np.array(10, dtype=np.int64))
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(folder + "voice.npz", latent.getvalue())
        archive.writestr(folder + "meta.json", json.dumps({"display_name": display_name}, ensure_ascii=False))
    return payload.getvalue()


@pytest.fixture
def model(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "zerotts_model_dir", "")
    monkeypatch.setattr(settings, "zerotts_voice_packs_dir", str(tmp_path / "repo_packs"))
    root = tmp_path / "zerotts"
    root.mkdir()
    (root / "config.json").write_text(json.dumps({"n_voice_queries": 10, "d_model": 768}))
    with tts_model_manager._preview_lock:
        tts_model_manager._preview_state.clear()
    return root


def make_legacy(fill=7.0, label="Giọng cũ"):
    emb = np.full((1, 10, 768), fill, dtype=np.float32)
    voice_id = "custom_" + hashlib.sha256(emb.tobytes()).hexdigest()
    folder = voices_dir() / voice_id
    folder.mkdir(parents=True)
    np.savez(folder / "voice.npz", voice_emb=emb, n_voice_queries=np.array(10, dtype=np.int64))
    (folder / "meta.json").write_text(json.dumps({"display_name": label, "language": "vi"}, ensure_ascii=False),
                                      encoding="utf-8")
    return voice_id


# --- R1: slug ids -------------------------------------------------------------

@pytest.mark.parametrize("name, slug", [
    ("Mai Chi", "maichi"), ("Kim Oanh", "kimoanh"), ("Đức Anh 2", "ducanh2"),
    ("../../etc", "etc"), ("", "giong"), ("   ", "giong"), ("中文", "giong"),
])
def test_slugify(name, slug):
    assert slugify_voice_name(name) == slug


def test_new_import_gets_slug_id_and_collisions_get_suffix(model):
    first = import_voice_pack(make_pack("Mai Chi", 1.0), model)
    second = import_voice_pack(make_pack("Mai Chi", 2.0), model)
    third = import_voice_pack(make_pack("mai-chi", 3.0), model)
    assert [first["id"], second["id"], third["id"]] == ["maichi", "maichi-2", "maichi-3"]
    assert {v["id"] for v in list_voices()} == {"maichi", "maichi-2", "maichi-3"}
    # The engine resolves a slug id to the imported latents.
    emb = ZeroTTSEngine(voice="maichi-2", model_dir=str(model))._load_voice_emb(10)
    assert float(emb[0, 0, 0]) == 2.0


def test_slug_never_shadows_a_bundled_voice(model):
    (model / "voices" / "maichi").mkdir(parents=True)
    assert unique_voice_id("Mai Chi", model) == "maichi-2"
    assert import_voice_pack(make_pack("Mai Chi"), model)["id"] == "maichi-2"
    # A bundled-only name is not an imported voice, so the engine keeps using the weights.
    assert voice_path("maichi") is None


@pytest.mark.parametrize("bad", ["../x", "a/b", "A", "", "..", "custom_xyz", "x" * 80, "c:evil"])
def test_voice_path_rejects_unsafe_or_unknown_ids(model, bad):
    assert voice_path(bad) is None


def test_reimport_same_latent_is_idempotent(model):
    voice = import_voice_pack(make_pack("Mai Chi", 1.0), model)
    original_pack = (voices_dir() / voice["id"] / "pack.zip").read_bytes()
    again = import_voice_pack(make_pack("Tên khác", 1.0, folder="other/"), model)
    assert again["id"] == voice["id"]
    assert [v["id"] for v in list_voices()] == [voice["id"]]
    # First copy wins for the pack itself.
    assert (voices_dir() / voice["id"] / "pack.zip").read_bytes() == original_pack


def test_reimport_of_a_legacy_voice_reuses_the_legacy_id(model):
    legacy = make_legacy(fill=1.0)
    voice = import_voice_pack(make_pack("Mai Chi", 1.0), model)
    assert voice["id"] == legacy
    assert [v["id"] for v in list_voices()] == [legacy]


# --- R1: legacy custom_<64hex> compatibility -----------------------------------

def test_legacy_ids_round_trip_alongside_slugs(model):
    legacy = make_legacy()
    slug = import_voice_pack(make_pack("Kim Oanh", 3.0), model)["id"]
    assert voice_path(legacy) == voices_dir() / legacy / "voice.npz"
    assert {v["id"] for v in list_voices()} == {legacy, slug}

    assert update_voice(legacy, label="Đổi tên", language="vi")["label"] == "Đổi tên"
    assert next(v for v in list_voices() if v["id"] == legacy)["label"] == "Đổi tên"
    payload, filename = pack_bytes(legacy)
    assert filename == "Đổi tên.zip" and payload

    emb = ZeroTTSEngine(voice=legacy, model_dir=str(model))._load_voice_emb(10)
    assert float(emb[0, 0, 0]) == 7.0

    delete_voice(legacy)
    assert [v["id"] for v in list_voices()] == [slug]
    with pytest.raises(FileNotFoundError):
        delete_voice(legacy)


def test_repo_packs_keyed_by_latent_hash_for_both_id_shapes(model, tmp_path):
    legacy = make_legacy()
    slug = import_voice_pack(make_pack("Kim Oanh", 3.0), model)["id"]
    result = sync_repo_packs(model)
    assert sorted(result["exported"]) == sorted([legacy, slug])
    repo = tmp_path / "repo_packs"
    digest = hashlib.sha256(np.full((1, 10, 768), 3.0, dtype=np.float32).tobytes()).hexdigest()
    assert (repo / f"Kim Oanh.{digest[:12]}.zip").is_file()
    assert (repo / f"Giọng cũ.{legacy[7:19]}.zip").is_file()
    # Idempotent: nothing new on a second sync, and save_repo_copy finds the file.
    assert sync_repo_packs(model) == {"imported": [], "exported": [], "failed": []}
    assert save_repo_copy(slug) == repo / f"Kim Oanh.{digest[:12]}.zip"

    # Another machine: the data dir is empty, the repo packs import back.
    shutil.rmtree(voices_dir())
    imported = sync_repo_packs(model)["imported"]
    assert sorted(v["id"] for v in imported) == ["giongcu", "kimoanh"]
    assert len(list(repo.glob("*.zip"))) == 2

    delete_voice("kimoanh")
    assert not (repo / f"Kim Oanh.{digest[:12]}.zip").exists()


# --- R2 / R3: sample list + cached preview -------------------------------------

def test_sample_list_includes_custom_voices_without_synthesizing(model, monkeypatch):
    calls = []
    monkeypatch.setattr(tts_model_manager, "_synthesize_preview", lambda vid: calls.append(vid))
    monkeypatch.setattr("app.tts_engine.fetch_zerotts_voice_index", lambda *a, **k: [])
    legacy = make_legacy()
    slug = import_voice_pack(make_pack("Mai Chi"), model)["id"]
    catalog = tts_model_manager.list_sample_voices()
    custom = {v["id"]: v for v in catalog["models"]["zerotts"]["voices"] if v.get("custom")}
    assert set(custom) == {legacy, slug}
    assert custom[slug]["audio_url"] is None and custom[slug]["preview_status"] == "missing"
    assert calls == []


def test_preview_is_synthesized_once_and_then_served_from_cache(model, monkeypatch):
    calls = []

    def fake_synth(voice_id):
        calls.append(voice_id)
        return np.zeros(4800, dtype=np.float32), 48000

    monkeypatch.setattr(tts_model_manager, "_synthesize_preview", fake_synth)
    monkeypatch.setattr("app.tts_engine.fetch_zerotts_voice_index", lambda *a, **k: [])
    slug = import_voice_pack(make_pack("Mai Chi"), model)["id"]

    path = tts_model_manager.build_custom_voice_preview(slug)
    assert path == tts_model_manager.custom_preview_path(slug) and path.stat().st_size > 0
    assert tts_model_manager.build_custom_voice_preview(slug) == path
    assert tts_model_manager.queue_custom_voice_previews([slug])["queued"] == []
    assert calls == [slug]

    assert tts_model_manager.sample_voice_file("zerotts", slug) == path
    entry = next(v for v in tts_model_manager.list_sample_voices()["models"]["zerotts"]["voices"] if v["id"] == slug)
    assert entry["audio_url"] == f"/tts-models/sample-voices/file/zerotts/{slug}"
    assert entry["preview_status"] == "ready"
    assert calls == [slug]


def test_background_queue_builds_missing_previews_and_reports_failures(model, monkeypatch):
    calls = []

    def fake_synth(voice_id):
        calls.append(voice_id)
        if voice_id == "kimoanh":
            raise RuntimeError("boom")
        return np.zeros(480, dtype=np.float32), 48000

    monkeypatch.setattr(tts_model_manager, "_synthesize_preview", fake_synth)
    monkeypatch.setattr("app.tts_engine.fetch_zerotts_voice_index", lambda *a, **k: [])
    import_voice_pack(make_pack("Mai Chi", 1.0), model)
    import_voice_pack(make_pack("Kim Oanh", 2.0), model)

    queued = tts_model_manager.queue_custom_voice_previews(None)["queued"]
    assert queued == ["kimoanh", "maichi"]
    deadline = time.time() + 10
    while tts_model_manager._preview_worker is not None and time.time() < deadline:
        time.sleep(0.02)
    entries = {v["id"]: v for v in tts_model_manager.list_sample_voices()["models"]["zerotts"]["voices"]}
    assert entries["maichi"]["preview_status"] == "ready"
    assert entries["kimoanh"]["preview_status"] == "failed" and "boom" in entries["kimoanh"]["preview_error"]
    assert sorted(calls) == ["kimoanh", "maichi"]


def test_preview_without_weights_fails_cleanly_and_voice_still_listed(model, monkeypatch):
    monkeypatch.setattr(tts_model_manager, "_synthesize_preview",
                        lambda vid: pytest.fail("must not synthesize without weights"))
    monkeypatch.setattr("app.tts_engine.fetch_zerotts_voice_index", lambda *a, **k: [])
    slug = import_voice_pack(make_pack("Mai Chi"), model)["id"]
    (model / "config.json").unlink()
    with pytest.raises(RuntimeError, match="tải model"):
        tts_model_manager.build_custom_voice_preview(slug)
    entry = next(v for v in tts_model_manager.list_sample_voices()["models"]["zerotts"]["voices"] if v["id"] == slug)
    assert entry["audio_url"] is None


def test_routes_trigger_previews_and_delete_drops_cache(model, monkeypatch):
    monkeypatch.setattr(tts_model_manager, "_synthesize_preview",
                        lambda vid: (np.zeros(480, dtype=np.float32), 48000))
    monkeypatch.setattr("app.tts_engine.fetch_zerotts_voice_index", lambda *a, **k: [])
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        voice = client.post("/tts-models/zerotts/voices/import",
                            files={"file": ("mai.zip", make_pack("Mai Chi"), "application/zip")}).json()["voice"]
        assert voice["id"] == "maichi"
        assert client.post("/tts-models/sample-voices/zerotts/custom-previews",
                           json={"voice_ids": "maichi"}).status_code == 400
        assert client.post("/tts-models/sample-voices/zerotts/custom-previews",
                           json={"voice_ids": ["maichi", "nope"]}).json()["queued"] == ["maichi"]
        deadline = time.time() + 10
        while tts_model_manager._preview_worker is not None and time.time() < deadline:
            time.sleep(0.02)
        audio = client.get("/tts-models/sample-voices/file/zerotts/maichi")
        assert audio.status_code == 200 and audio.content[:4] == b"RIFF"
        assert client.delete("/tts-models/zerotts/voices/maichi").status_code == 200
        assert not tts_model_manager.custom_preview_path("maichi").exists()
        assert client.get("/tts-models/sample-voices/file/zerotts/maichi").status_code == 404
