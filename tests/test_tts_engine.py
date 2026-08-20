import ast
import importlib.util
import sys
import types
from pathlib import Path
from unittest import mock

import numpy as np
import pytest

from app.tts_engine import (
    EdgeTTSEngine,
    GTTSEngine,
    OmniVoiceEngine,
    VieNeuFastEngine,
    VoxCPMEngine,
    ZeroTTSEngine,
    create_tts_engine,
    list_tts_models,
    normalize_tt_payload,
)


class FakeModel:
    def __init__(self):
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return np.array([1.0])


def _installed_generate_parameters() -> set[str]:
    """Parameter names of the installed VoxCPM._generate, read straight off its source.

    Parsed with ast rather than imported because importing voxcpm pulls in torch and
    costs ~30s. FakeModel.generate(**kwargs) swallows anything, so without checking the
    real signature a kwarg the library rejects (this happened with 'seed') passes every
    test here and only blows up on the GPU box mid-synthesis."""
    spec = importlib.util.find_spec("voxcpm")  # does not execute voxcpm/__init__.py
    if spec is None or not spec.submodule_search_locations:
        pytest.skip("voxcpm is not installed")
    source = Path(spec.submodule_search_locations[0], "core.py")
    if not source.exists():
        pytest.skip(f"voxcpm layout changed: {source} is missing")
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for klass in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "VoxCPM"):
        for func in (n for n in klass.body if isinstance(n, ast.FunctionDef) and n.name == "_generate"):
            args = func.args
            return {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs} - {"self"}
    pytest.skip("voxcpm.core.VoxCPM._generate not found")


def test_synthesize_chunk_only_sends_kwargs_the_installed_voxcpm_accepts(monkeypatch):
    monkeypatch.setattr("app.tts_engine._seed_rng", lambda seed: None)  # torch not needed here
    model = FakeModel()
    engine = VoxCPMEngine()
    engine._model = model

    engine.synthesize_chunk("hello", reference_wav_path="voice.wav", prompt_text="hello")

    unsupported = set(model.calls[0]) - _installed_generate_parameters()
    assert unsupported == set()


def test_synthesize_chunk_passes_generation_defaults(monkeypatch):
    monkeypatch.setattr("app.tts_engine._seed_rng", lambda seed: None)
    model = FakeModel()
    engine = VoxCPMEngine()
    engine._model = model

    result = engine.synthesize_chunk("hello")

    assert result.tolist() == [1.0]
    assert model.calls == [
        {
            "text": "hello",
            "cfg_value": 2.0,
            "inference_timesteps": 10,
        }
    ]


def test_synthesize_chunk_seeds_the_rng_before_each_generate(monkeypatch):
    """VoxCPM 2.x dropped the per-call seed argument, so reproducibility now depends on
    seeding torch's global RNG ourselves - and on doing it before sampling starts."""
    events = []
    monkeypatch.setattr("app.tts_engine._seed_rng", lambda seed: events.append(("seed", seed)))

    class RecordingModel(FakeModel):
        def generate(self, **kwargs):
            events.append(("generate", kwargs["text"]))
            return super().generate(**kwargs)

    engine = VoxCPMEngine(seed=7)
    engine._model = RecordingModel()

    engine.synthesize_chunk("một")
    engine.synthesize_chunk("hai")

    assert events == [("seed", 7), ("generate", "một"), ("seed", 7), ("generate", "hai")]


def test_synthesize_chunk_passes_ultimate_cloning_prompt_arguments(monkeypatch):
    monkeypatch.setattr("app.tts_engine._seed_rng", lambda seed: None)
    model = FakeModel()
    engine = VoxCPMEngine()
    engine._model = model

    engine.synthesize_chunk("hello", reference_wav_path="voice.wav", prompt_text="hello")

    assert model.calls[0] == {
        "text": "hello",
        "cfg_value": 2.0,
        "inference_timesteps": 10,
        "reference_wav_path": "voice.wav",
        "prompt_wav_path": "voice.wav",
        "prompt_text": "hello",
    }


def test_synthesize_chunk_without_prompt_text_omits_prompt_arguments(monkeypatch):
    monkeypatch.setattr("app.tts_engine._seed_rng", lambda seed: None)
    model = FakeModel()
    engine = VoxCPMEngine()
    engine._model = model

    engine.synthesize_chunk("hello", reference_wav_path="voice.wav")

    assert "prompt_wav_path" not in model.calls[0]
    assert "prompt_text" not in model.calls[0]


def test_model_catalog_and_factory_are_unified():
    models = list_tts_models()
    assert [model["id"] for model in models] == [
        "voxcpm2", "omnivoice", "vieneu-fast", "zerotts", "edge-tts", "gtts",
    ]
    assert isinstance(create_tts_engine("voxcpm2"), VoxCPMEngine)
    assert isinstance(create_tts_engine("omnivoice"), OmniVoiceEngine)
    assert isinstance(create_tts_engine("vieneu-fast"), VieNeuFastEngine)
    assert isinstance(create_tts_engine("zerotts"), ZeroTTSEngine)
    assert isinstance(create_tts_engine("edge-tts"), EdgeTTSEngine)
    assert isinstance(create_tts_engine("gtts"), GTTSEngine)


def test_catalog_carries_capability_metadata():
    models = {m["id"]: m for m in list_tts_models()}
    assert models["voxcpm2"]["capabilities"] == {
        "kind": "local", "reference_audio": True, "voice_selection": False,
        "offline": True, "online": False,
    }
    for cloud in ("edge-tts", "gtts"):
        assert models[cloud]["capabilities"] == {
            "kind": "cloud", "reference_audio": False, "voice_selection": True,
            "offline": False, "online": True,
        }
        assert models[cloud]["supports_reference"] is False
    assert models["edge-tts"]["default_voice"] == "vi-VN-HoaiMyNeural"
    assert models["gtts"]["default_voice"] == "vi"
    # ZeroTTS is the one local engine that picks from a fixed cast instead of cloning:
    # offline like the other local models, voice_selection like the cloud ones.
    assert models["zerotts"]["capabilities"] == {
        "kind": "local", "reference_audio": False, "voice_selection": True,
        "offline": True, "online": False,
    }
    assert models["zerotts"]["supports_reference"] is False
    assert models["zerotts"]["default_voice"] == "maichi"
    # VieNeu could clone, but the app drives it from the same fixed cast instead.
    assert models["vieneu-fast"]["capabilities"] == models["zerotts"]["capabilities"]
    assert models["vieneu-fast"]["supports_reference"] is False
    assert models["vieneu-fast"]["default_voice"] == "Adam"


def test_normalize_payload_accepts_legacy_shapes_and_canonicalises():
    assert normalize_tt_payload({"patch_id": 1})["tts_engine"] == "voxcpm2"
    canonical = normalize_tt_payload({"patch_id": 1, "backend": "gtts", "voice": "vi"})
    assert canonical["tts_engine"] == "gtts"
    assert canonical["max_chars"] == 0 and canonical["with_effects"] is False
    assert normalize_tt_payload({"patch_id": 1, "tts_engine": "edge-tts"})["tts_engine"] == "edge-tts"
    with pytest.raises(ValueError, match="Unknown TTS engine"):
        normalize_tt_payload({"patch_id": 1, "tts_engine": "nope"})


def test_cloud_engines_default_their_voice_from_the_catalog(monkeypatch):
    assert EdgeTTSEngine().voice == "vi-VN-HoaiMyNeural"
    assert GTTSEngine().voice == "vi"
    assert create_tts_engine("edge-tts", voice="vi-VN-NamMinhNeural").voice == "vi-VN-NamMinhNeural"


def test_cloud_engines_decode_to_arrays_and_learn_the_sample_rate(monkeypatch):
    import io

    import soundfile as sf

    def fake_wav_bytes(text):
        buf = io.BytesIO()
        sf.write(buf, np.zeros(2400, dtype="float32"), 24000, format="WAV")
        return buf.getvalue()

    engine = EdgeTTSEngine()
    monkeypatch.setattr(engine, "_synth_wav_bytes", fake_wav_bytes)
    result = engine.synthesize_chunk("xin chào", "voice.wav", "unused")
    assert result.dtype == np.float32 and result.shape == (2400,)
    assert engine.sample_rate == 24000
    assert engine.config_fingerprint() == "edge-tts:vi-VN-HoaiMyNeural"


def test_local_engines_report_a_stable_config_fingerprint():
    assert "voxcpm2" in VoxCPMEngine().config_fingerprint()
    assert "omnivoice" in OmniVoiceEngine().config_fingerprint()
    assert "vieneu-fast" in VieNeuFastEngine().config_fingerprint()
    assert VoxCPMEngine(seed=1).config_fingerprint() != VoxCPMEngine(seed=2).config_fingerprint()


def test_omnivoice_normalizes_list_output_and_clone_arguments(monkeypatch):
    monkeypatch.setattr("app.tts_engine._seed_rng", lambda seed: None)
    model = FakeModel()
    model.generate = lambda **kwargs: (model.calls.append(kwargs) or [np.array([0.25])])
    engine = OmniVoiceEngine()
    engine._model = model

    result = engine.synthesize_chunk("xin chào", "voice.wav", "giọng mẫu")

    assert result.dtype == np.float32
    assert model.calls == [{"text": "xin chào", "ref_audio": "voice.wav", "ref_text": "giọng mẫu"}]


def test_vieneu_fast_speaks_a_preset_and_ignores_any_reference_clip():
    """infer() resolves ref_audio before voice, so sending the book's clip would
    silently override the preset the user chose. The clip must not reach infer()."""
    class FakeVieNeu:
        def infer(self, text, **kwargs):
            # v3 Turbo ignores style=, so the engine must not send it at all.
            assert (text, kwargs) == ("xin chào", {"voice": "Ngọc Linh"})
            return [0.5]

    engine = VieNeuFastEngine(voice="Ngọc Linh")
    engine._model = FakeVieNeu()
    result = engine.synthesize_chunk("xin chào", "voice.wav", "unused transcript")

    assert result.dtype == np.float32
    assert result.tolist() == [0.5]


def test_vieneu_fast_falls_back_to_the_catalog_default_voice():
    assert VieNeuFastEngine().voice == "Adam"
    assert "Adam" in VieNeuFastEngine().config_fingerprint()
    # The voice changes the audio, so it has to change the chunk cache key too.
    assert (VieNeuFastEngine(voice="Mai Anh").config_fingerprint()
            != VieNeuFastEngine(voice="Thái Sơn").config_fingerprint())


def test_vieneu_fast_loads_v3turbo_weights_explicitly():
    seen = {}

    def fake_vieneu(**kwargs):
        seen.update(kwargs)
        return object()

    module = types.ModuleType("vieneu")
    module.Vieneu = fake_vieneu
    with mock.patch.dict(sys.modules, {"vieneu": module}):
        VieNeuFastEngine()._ensure_loaded()

    assert seen == {
        "mode": "v3turbo",
        "backbone_repo": "pnnbao-ump/VieNeu-TTS-v3-Turbo",
        "precision": "int8",
    }
