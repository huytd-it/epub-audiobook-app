"""Pluggable network TTS providers.

Providers are configured with ``TTS_API_PROVIDERS`` as a JSON array.  The
audiobook pipeline only knows the small TTSEngine protocol, so adding an API
provider never requires a new queue handler.
"""
from __future__ import annotations

import base64
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass
from typing import Any

import numpy as np
import soundfile as sf


BUILTIN_API_ENGINES = frozenset({"edge-tts", "gtts"})
SUPPORTED_ADAPTERS = frozenset({"openai", "gemini", "elevenlabs", "vbee"})


def _configs() -> list[dict[str, Any]]:
    from app.config import settings

    raw = settings.tts_api_providers.strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"TTS_API_PROVIDERS không phải JSON hợp lệ: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("TTS_API_PROVIDERS phải là một JSON array")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Mỗi TTS API provider phải là một object")
        provider_id = str(item.get("id") or "").strip()
        adapter = str(item.get("adapter") or "openai").strip().lower()
        if not provider_id or provider_id in seen or provider_id in BUILTIN_API_ENGINES:
            raise ValueError(f"TTS API provider id không hợp lệ hoặc trùng: {provider_id!r}")
        if adapter not in SUPPORTED_ADAPTERS:
            raise ValueError(f"Adapter {adapter!r} chưa được hỗ trợ; chọn: {', '.join(sorted(SUPPORTED_ADAPTERS))}")
        seen.add(provider_id)
        result.append({**item, "id": provider_id, "adapter": adapter})
    return result


def provider_config(engine_id: str) -> dict[str, Any] | None:
    return next((item for item in _configs() if item["id"] == engine_id), None)


def is_api_engine(engine_id: str | None) -> bool:
    return bool(engine_id) and (engine_id in BUILTIN_API_ENGINES or provider_config(str(engine_id)) is not None)


def list_api_models() -> list[dict[str, Any]]:
    models = []
    for cfg in _configs():
        key_env = str(cfg.get("api_key_env") or _default_key_env(cfg["adapter"]))
        configured = bool(os.getenv(key_env))
        voices = cfg.get("voices") if isinstance(cfg.get("voices"), list) else []
        normalized_voices = [
            {"id": str(v["id"]), "label": str(v.get("label") or v["id"]), "language": str(v.get("language") or "")}
            for v in voices if isinstance(v, dict) and v.get("id")
        ]
        default_voice = str(cfg.get("voice") or (normalized_voices[0]["id"] if normalized_voices else "")) or None
        models.append({
            "id": cfg["id"],
            "name": str(cfg.get("name") or cfg["id"]),
            "model_id": str(cfg.get("model") or ""),
            "package": "api",
            "sample_rate": int(cfg.get("sample_rate") or 24000),
            "supports_reference": False,
            "capabilities": {
                "kind": "api", "runtime": "api", "provider": cfg["adapter"],
                "reference_audio": False, "voice_selection": True,
                "offline": False, "online": True,
            },
            "default_voice": default_voice,
            "voices": normalized_voices,
            "options_schema": [],
            "configured": configured,
            "config_hint": f"Đặt secret trong biến môi trường {key_env}",
        })
    return models


def _default_key_env(adapter: str) -> str:
    return {"gemini": "GEMINI_API_KEY", "elevenlabs": "ELEVENLABS_API_KEY", "vbee": "VBEE_API_KEY"}.get(adapter, "OPENAI_API_KEY")


def _request(url: str, *, payload: dict[str, Any] | None, headers: dict[str, str], timeout: float) -> tuple[bytes, str]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(), response.headers.get("Content-Type", "")
    except urllib.error.HTTPError as exc:
        detail = exc.read(2048).decode("utf-8", errors="replace")
        raise RuntimeError(f"TTS API lỗi HTTP {exc.code}: {detail}") from exc


def _pcm_wav(pcm: bytes, sample_rate: int) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return out.getvalue()


@dataclass
class ApiTTSEngine:
    engine_id: str
    voice: str | None = None

    def __post_init__(self) -> None:
        cfg = provider_config(self.engine_id)
        if cfg is None:
            raise ValueError(f"Không tìm thấy TTS API provider {self.engine_id!r}")
        self.config = cfg
        self.voice = self.voice or cfg.get("voice")
        self._sample_rate = int(cfg.get("sample_rate") or 24000)

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def config_fingerprint(self) -> str:
        return f"api:{self.engine_id}:{self.config.get('model', '')}:{self.voice or ''}"

    def synthesize_chunk(self, text: str, reference_wav_path: str | None = None,
                         prompt_text: str | None = None) -> np.ndarray:
        audio = self._synthesize(text)
        data, sample_rate = sf.read(io.BytesIO(audio))
        self._sample_rate = int(sample_rate)
        data = np.asarray(data, dtype=np.float32)
        return data.mean(axis=1) if data.ndim > 1 else data

    def _synthesize(self, text: str) -> bytes:
        adapter = self.config["adapter"]
        key_env = str(self.config.get("api_key_env") or _default_key_env(adapter))
        api_key = os.getenv(key_env, "")
        if not api_key:
            raise RuntimeError(f"Thiếu API key: đặt biến môi trường {key_env}")
        timeout = float(self.config.get("timeout_seconds") or 120)
        if adapter == "gemini":
            return self._gemini(text, api_key, timeout)
        if adapter == "vbee":
            return self._vbee(text, api_key, timeout)
        if adapter == "elevenlabs":
            return self._elevenlabs(text, api_key, timeout)
        return self._openai(text, api_key, timeout)

    def _openai(self, text: str, api_key: str, timeout: float) -> bytes:
        base = str(self.config.get("base_url") or "https://api.openai.com/v1").rstrip("/")
        url = base if base.endswith("/audio/speech") else f"{base}/audio/speech"
        payload = {
            "model": self.config.get("model") or "gpt-4o-mini-tts",
            "input": text,
            "voice": self.voice or "alloy",
            "response_format": "wav",
        }
        if self.config.get("instructions"):
            payload["instructions"] = self.config["instructions"]
        audio, _ = _request(url, payload=payload, headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
        }, timeout=timeout)
        return audio

    def _elevenlabs(self, text: str, api_key: str, timeout: float) -> bytes:
        voice = urllib.parse.quote(str(self.voice or self.config.get("voice") or ""), safe="")
        if not voice:
            raise RuntimeError("ElevenLabs cần voice hoặc voice mặc định")
        base = str(self.config.get("base_url") or "https://api.elevenlabs.io/v1").rstrip("/")
        url = f"{base}/text-to-speech/{voice}?output_format=pcm_24000"
        pcm, _ = _request(url, payload={
            "text": text, "model_id": self.config.get("model") or "eleven_multilingual_v2",
        }, headers={"xi-api-key": api_key, "Content-Type": "application/json"}, timeout=timeout)
        self._sample_rate = 24000
        return _pcm_wav(pcm, self._sample_rate)

    def _gemini(self, text: str, api_key: str, timeout: float) -> bytes:
        url = str(self.config.get("base_url") or "https://generativelanguage.googleapis.com/v1beta/interactions")
        body, _ = _request(url, payload={
            "model": self.config.get("model") or "gemini-3.1-flash-tts-preview",
            "input": text,
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": self.voice or "Kore"}]},
        }, headers={"x-goog-api-key": api_key, "Content-Type": "application/json", "Api-Revision": "2026-05-20"}, timeout=timeout)
        response = json.loads(body)
        block = response.get("output_audio") or response.get("audio") or {}
        encoded = block.get("data") if isinstance(block, dict) else None
        if not encoded:
            raise RuntimeError("Gemini không trả về output_audio.data")
        self._sample_rate = int(self.config.get("sample_rate") or 24000)
        return _pcm_wav(base64.b64decode(encoded), self._sample_rate)

    def _vbee(self, text: str, api_key: str, timeout: float) -> bytes:
        """Submit Vbee's asynchronous request, poll it, then fetch the WAV result."""
        base = str(self.config.get("base_url") or "https://vbee.vn/api/v1/tts").rstrip("/")
        app_id = str(self.config.get("app_id") or "")
        callback_url = str(self.config.get("callback_url") or "")
        if not app_id or not callback_url:
            raise RuntimeError("Vbee cần app_id và callback_url trong TTS_API_PROVIDERS")
        body, _ = _request(base, payload={
            "app_id": app_id,
            "input_text": text,
            "voice_code": self.voice or self.config.get("voice"),
            "audio_type": "wav",
            "speed_rate": float(self.config.get("speed_rate") or 1),
            "response_type": "indirect",
            "callback_url": callback_url,
        }, headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, timeout=timeout)
        created = json.loads(body)
        result = created.get("result") if isinstance(created, dict) else None
        request_id = result.get("request_id") if isinstance(result, dict) else None
        if not request_id:
            raise RuntimeError(f"Vbee không trả về request_id: {created}")
        deadline = time.monotonic() + timeout
        headers = {"Authorization": f"Bearer {api_key}"}
        while time.monotonic() < deadline:
            raw, _ = _request(f"{base}/{urllib.parse.quote(str(request_id), safe='')}", payload=None,
                              headers=headers, timeout=min(30, timeout))
            status_body = json.loads(raw)
            status_result = status_body.get("result") if isinstance(status_body, dict) else None
            if isinstance(status_result, dict):
                status = str(status_result.get("status") or "").upper()
                audio_link = status_result.get("audio_link")
                if status == "SUCCESS" and audio_link:
                    audio, _ = _request(str(audio_link), payload=None, headers={}, timeout=timeout)
                    return audio
                if status in {"FAILURE", "FAILED", "ERROR"}:
                    raise RuntimeError(f"Vbee tổng hợp thất bại: {status_result}")
            time.sleep(float(self.config.get("poll_interval_seconds") or 1.5))
        raise RuntimeError(f"Vbee chưa hoàn tất sau {timeout:g} giây")