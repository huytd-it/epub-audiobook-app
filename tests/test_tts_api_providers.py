import json

import pytest

from app.config import settings
from app.tts_api_providers import (
    ApiTTSEngine,
    is_api_engine,
    list_api_models,
    provider_config,
    validate_provider_payload,
)
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


def test_unsaved_payload_validates_without_an_id():
    """Nút "Test trước khi lưu" gửi payload chưa có id thật."""
    normalized = validate_provider_payload(
        {"id": "", "adapter": "custom", "base_url": "http://localhost:20128/v1", "model": "google-tts/vi"},
        is_update=True,
    )

    assert "id" not in normalized
    assert normalized["base_url"] == "http://localhost:20128/v1"


def test_custom_adapter_requires_base_url():
    """Base URL rỗng từng lặng lẽ gọi api.openai.com và trả 401 khó hiểu."""
    for is_update in (False, True):
        with pytest.raises(ValueError, match="base_url"):
            validate_provider_payload(
                {"id": "gateway", "adapter": "custom", "model": "google-tts/vi"},
                is_update=is_update,
            )


def _multi_model_config():
    return [{
        "id": "gateway",
        "name": "Gateway",
        "adapter": "custom",
        "base_url": "http://localhost:20128/v1",
        "api_key_env": "GATEWAY_TTS_KEY",
        "models": [
            {"id": "google-tts/vi", "label": "Google VI"},
            {"id": "google-tts/en", "label": "Google EN"},
        ],
    }]


def test_one_provider_publishes_one_catalog_entry_per_model(monkeypatch):
    monkeypatch.setattr(settings, "tts_api_providers", json.dumps(_multi_model_config()))
    monkeypatch.setenv("GATEWAY_TTS_KEY", "secret")

    entries = {model["id"]: model for model in list_api_models() if model["id"].startswith("gateway")}
    assert set(entries) == {"gateway:google-tts-vi", "gateway:google-tts-en"}
    assert entries["gateway:google-tts-vi"]["model_id"] == "google-tts/vi"
    assert entries["gateway:google-tts-en"]["name"] == "Gateway · Google EN"
    assert entries["gateway:google-tts-en"]["configured"] is True


def test_engine_id_carries_the_model_through_the_pipeline(monkeypatch):
    monkeypatch.setattr(settings, "tts_api_providers", json.dumps(_multi_model_config()))

    assert resolve_engine_id("gateway:google-tts-en") == "gateway:google-tts-en"
    assert is_api_engine("gateway:google-tts-en") is True
    engine = create_tts_engine("gateway:google-tts-en")
    assert isinstance(engine, ApiTTSEngine)
    assert engine.config["model"] == "google-tts/en"
    assert engine.config["base_url"] == "http://localhost:20128/v1"
    # Cache key phải phân biệt hai model của cùng một provider.
    assert engine.config_fingerprint() != create_tts_engine("gateway:google-tts-vi").config_fingerprint()


def test_unknown_model_slug_is_not_an_engine(monkeypatch):
    monkeypatch.setattr(settings, "tts_api_providers", json.dumps(_multi_model_config()))

    assert provider_config("gateway:khong-co") is None
    assert is_api_engine("gateway:khong-co") is False


def test_single_model_provider_keeps_its_bare_id(monkeypatch):
    """Provider cũ (chỉ có `model`) không được đổi engine id, tránh vỡ audio settings đã lưu."""
    monkeypatch.setattr(settings, "tts_api_providers", json.dumps(_provider_config()))

    ids = [model["id"] for model in list_api_models()]
    assert "studio-gateway" in ids
    assert not any(item.startswith("studio-gateway:") for item in ids)
