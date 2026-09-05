import json

from app.config import settings
from app.tts_api_providers import ApiTTSEngine, is_api_engine, list_api_models
from app.tts_engine import create_tts_engine, list_tts_models, resolve_engine_id


def _provider_config():
    return [{
        "id": "studio-gateway",
        "name": "Studio gateway",
        "adapter": "openai",
        "base_url": "http://localhost:9000/v1",
        "model": "vi-narrator",
        "voice": "hanoi-female",
        "api_key_env": "STUDIO_TTS_KEY",
        "voices": [{"id": "hanoi-female", "label": "Hà Nội nữ", "language": "vi"}],
    }]


def test_configured_api_provider_joins_catalog(monkeypatch):
    monkeypatch.setattr(settings, "tts_api_providers", json.dumps(_provider_config()))
    monkeypatch.setenv("STUDIO_TTS_KEY", "secret")

    api_model = list_api_models()[0]
    assert api_model["id"] == "studio-gateway"
    assert api_model["configured"] is True
    assert api_model["capabilities"]["runtime"] == "api"
    assert any(model["id"] == "studio-gateway" for model in list_tts_models())


def test_factory_and_queue_classification_accept_dynamic_provider(monkeypatch):
    monkeypatch.setattr(settings, "tts_api_providers", json.dumps(_provider_config()))

    assert resolve_engine_id("studio-gateway") == "studio-gateway"
    assert is_api_engine("studio-gateway") is True
    engine = create_tts_engine("studio-gateway")
    assert isinstance(engine, ApiTTSEngine)
    assert engine.voice == "hanoi-female"


def test_builtin_network_engines_are_api_runtime(monkeypatch):
    monkeypatch.setattr(settings, "tts_api_providers", "")
    assert is_api_engine("edge-tts") is True
    assert is_api_engine("gtts") is True
    assert is_api_engine("voxcpm2") is False
