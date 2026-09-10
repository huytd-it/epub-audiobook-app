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
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf


BUILTIN_API_ENGINES = frozenset({"edge-tts", "gtts"})
SUPPORTED_ADAPTERS = frozenset({"openai", "custom", "gemini", "elevenlabs", "vbee", "google"})
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


def _custom_file() -> Path:
    from app.config import settings

    return Path(settings.data_root) / "tts_custom_providers.json"


def _read_custom_providers() -> list[dict[str, Any]]:
    path = _custom_file()
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _write_custom_providers(items: list[dict[str, Any]]) -> None:
    path = _custom_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_config(item: dict[str, Any]) -> dict[str, Any]:
    provider_id = str(item.get("id") or "").strip()
    adapter = str(item.get("adapter") or "openai").strip().lower()
    if not _ID_RE.match(provider_id) or provider_id in BUILTIN_API_ENGINES:
        raise ValueError(f"TTS API provider id không hợp lệ hoặc trùng: {provider_id!r} (chỉ a-z, 0-9, -, _)")
    if adapter not in SUPPORTED_ADAPTERS:
        raise ValueError(f"Adapter {adapter!r} chưa được hỗ trợ; chọn: {', '.join(sorted(SUPPORTED_ADAPTERS))}")
    return {**item, "id": provider_id, "adapter": adapter}


def _env_configs() -> list[dict[str, Any]]:
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
        normalized = _normalize_config(item)
        if normalized["id"] in seen:
            raise ValueError(f"TTS API provider id trùng: {normalized['id']!r}")
        seen.add(normalized["id"])
        result.append(normalized)
    return result


def _configs() -> list[dict[str, Any]]:
    """Env providers (read-only) merged with UI-managed custom providers from disk."""
    merged = _env_configs()
    seen = {item["id"] for item in merged}
    for item in _read_custom_providers():
        if not isinstance(item, dict):
            continue
        try:
            normalized = _normalize_config(item)
        except ValueError:
            continue
        if normalized["id"] in seen:
            continue
        seen.add(normalized["id"])
        merged.append({**normalized, "custom": True})
    return merged


def _model_slug(model_id: str) -> str:
    """Engine ids travel through URLs and job payloads, so a model id like
    ``google-tts/vi`` becomes ``google-tts-vi``."""
    return re.sub(r"[^a-z0-9_-]+", "-", model_id.strip().lower()).strip("-")


def _model_entries(cfg: dict[str, Any]) -> list[dict[str, str]]:
    """The models one provider serves. Empty for a legacy single-model config,
    which keeps using its bare ``model`` field and the provider id as engine id."""
    raw = cfg.get("models")
    if not isinstance(raw, list):
        return []
    entries: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            item = {"id": item}
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "").strip()
        slug = _model_slug(model_id)
        if not model_id or not slug or slug in seen:
            continue
        seen.add(slug)
        entries.append({"id": model_id, "label": str(item.get("label") or model_id).strip(), "slug": slug})
    return entries


def list_custom_providers(*, include_secret: bool = False) -> list[dict[str, Any]]:
    """Raw custom providers from disk, sanitized for the UI unless include_secret."""
    result = []
    for item in _read_custom_providers():
        if not isinstance(item, dict):
            continue
        try:
            normalized = _normalize_config(item)
        except ValueError:
            continue
        sanitized = dict(normalized)
        secret = str(sanitized.pop("api_key", "") or "")
        sanitized["custom"] = True
        sanitized["has_api_key"] = bool(secret)
        if include_secret and secret:
            sanitized["api_key"] = secret
        result.append(sanitized)
    return result


def validate_provider_payload(data: dict[str, Any], *, is_update: bool = False) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("Payload provider phải là một object")
    payload = dict(data)
    if is_update:
        # Payload chưa lưu (test trước khi lưu): id không dùng tới, nhưng _normalize_config
        # vẫn bắt buộc id hợp lệ nên phải mượn một slug placeholder rồi bỏ đi.
        payload.pop("id", None)
        payload["id"] = "provider-preview"
    normalized = _normalize_config(payload)
    if is_update:
        normalized.pop("id", None)
    adapter = normalized["adapter"]
    if adapter == "custom" and not str(normalized.get("base_url") or "").strip():
        raise ValueError("Adapter 'custom' cần base_url; bỏ trống sẽ gọi nhầm https://api.openai.com/v1")
    if adapter == "vbee" and not is_update:
        if not str(normalized.get("app_id") or "").strip() or not str(normalized.get("callback_url") or "").strip():
            raise ValueError("Vbee cần app_id và callback_url")
    voices = normalized.get("voices")
    if voices is not None and not isinstance(voices, list):
        raise ValueError("voices phải là một array")
    if isinstance(voices, list):
        cleaned = []
        for voice in voices:
            if isinstance(voice, str) and voice.strip():
                cleaned.append({"id": voice.strip(), "label": voice.strip(), "language": ""})
            elif isinstance(voice, dict) and str(voice.get("id") or "").strip():
                cleaned.append({
                    "id": str(voice["id"]).strip(),
                    "label": str(voice.get("label") or voice["id"]).strip(),
                    "language": str(voice.get("language") or ""),
                })
        normalized["voices"] = cleaned
    models = normalized.get("models")
    if models is not None and not isinstance(models, list):
        raise ValueError("models phải là một array")
    if isinstance(models, list):
        normalized["models"] = [{"id": entry["id"], "label": entry["label"]}
                                for entry in _model_entries(normalized)]
    api_key = str(normalized.pop("api_key", "") or "")
    if api_key:
        if len(api_key) > 4096:
            raise ValueError("API key quá dài")
        normalized["api_key"] = api_key
    return normalized


def save_custom_provider(data: dict[str, Any], *, provider_id: str | None = None) -> dict[str, Any]:
    items = [item for item in _read_custom_providers() if isinstance(item, dict)]
    if provider_id is not None:
        target = next((item for item in items
                       if isinstance(item, dict) and str(item.get("id") or "") == provider_id), None)
        if target is None:
            raise KeyError(provider_id)
        merged = {**target, **data, "id": provider_id}
        if not str(data.get("api_key") or "") and target.get("api_key"):
            merged["api_key"] = target["api_key"]
        normalized = validate_provider_payload(merged)
        if normalized["id"] in {str(item.get("id") or "") for item in _env_configs()}:
            raise ValueError(f"Id {normalized['id']!r} trùng với provider cấu hình trong TTS_API_PROVIDERS")
        items = [normalized if item is target else item for item in items]
        _write_custom_providers(items)
        return normalized
    normalized = validate_provider_payload(data)
    existing = {str(item.get("id") or "") for item in items} | {item["id"] for item in _env_configs()}
    if normalized["id"] in existing:
        raise ValueError(f"TTS API provider id trùng: {normalized['id']!r}")
    items.append(normalized)
    _write_custom_providers(items)
    return normalized


def delete_custom_provider(provider_id: str) -> None:
    items = [item for item in _read_custom_providers() if isinstance(item, dict)]
    kept = [item for item in items if str(item.get("id") or "") != provider_id]
    if len(kept) == len(items):
        raise KeyError(provider_id)
    _write_custom_providers(kept)


def provider_config(engine_id: str) -> dict[str, Any] | None:
    configs = _configs()
    direct = next((item for item in configs if item["id"] == engine_id), None)
    if direct is not None:
        return direct
    # Multi-model provider: engine id is "<provider>:<model slug>".
    base_id, separator, slug = str(engine_id or "").partition(":")
    if not separator:
        return None
    cfg = next((item for item in configs if item["id"] == base_id), None)
    if cfg is None:
        return None
    entry = next((item for item in _model_entries(cfg) if item["slug"] == slug), None)
    if entry is None:
        return None
    return {**cfg, "id": engine_id, "model": entry["id"], "provider_id": base_id,
            "name": f"{cfg.get('name') or base_id} · {entry['label']}"}


def _resolve_api_key(cfg: dict[str, Any]) -> str:
    direct = str(cfg.get("api_key") or "").strip()
    if direct:
        return direct
    key_env = str(cfg.get("api_key_env") or _default_key_env(str(cfg.get("adapter") or "openai")))
    return os.getenv(key_env, "")


def is_api_engine(engine_id: str | None) -> bool:
    return bool(engine_id) and (engine_id in BUILTIN_API_ENGINES or provider_config(str(engine_id)) is not None)


def list_api_models() -> list[dict[str, Any]]:
    models = []
    for provider in _configs():
        # One catalog entry per model the provider serves; a provider without a
        # ``models`` list stays a single entry keyed by its own id.
        variants = [
            {**provider, "id": f"{provider['id']}:{entry['slug']}", "model": entry["id"],
             "provider_id": provider["id"],
             "name": f"{provider.get('name') or provider['id']} · {entry['label']}"}
            for entry in _model_entries(provider)
        ] or [provider]
        for cfg in variants:
            key_env = str(cfg.get("api_key_env") or _default_key_env(str(cfg["adapter"])))
            configured = bool(_resolve_api_key(cfg))
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
                "custom": bool(cfg.get("custom")),
                "has_api_key": bool(str(cfg.get("api_key") or "")),
                "config_hint": f"Đặt secret trong biến môi trường {key_env}",
            })
    return models


def _default_key_env(adapter: str) -> str:
    return {
        "gemini": "GEMINI_API_KEY",
        "elevenlabs": "ELEVENLABS_API_KEY",
        "vbee": "VBEE_API_KEY",
        "google": "GOOGLE_TTS_API_KEY",
    }.get(adapter, "OPENAI_API_KEY")


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
        api_key = _resolve_api_key(self.config)
        if not api_key:
            key_env = str(self.config.get("api_key_env") or _default_key_env(adapter))
            raise RuntimeError(f"Thiếu API key: nhập key trong UI hoặc đặt biến môi trường {key_env}")
        timeout = float(self.config.get("timeout_seconds") or 120)
        if adapter == "gemini":
            return self._gemini(text, api_key, timeout)
        if adapter == "vbee":
            return self._vbee(text, api_key, timeout)
        if adapter == "elevenlabs":
            return self._elevenlabs(text, api_key, timeout)
        if adapter == "google":
            return self._google(text, api_key, timeout)
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

    def _google(self, text: str, api_key: str, timeout: float) -> bytes:
        """Google Cloud Text-to-Speech: https://cloud.google.com/text-to-speech/docs."""
        base = str(self.config.get("base_url") or "https://texttospeech.googleapis.com/v1").rstrip("/")
        url = f"{base}/text:synthesize?key={urllib.parse.quote(api_key, safe='')}"
        voice_name = str(self.voice or self.config.get("voice") or "vi-VN-Standard-A")
        language_code = str(self.config.get("language_code") or "-".join(voice_name.split("-")[:2]) or "vi-VN")
        sample_rate = int(self.config.get("sample_rate") or 24000)
        speaking_rate = float(self.config.get("speaking_rate") or 1.0)
        body, _ = _request(url, payload={
            "input": {"text": text},
            "voice": {"languageCode": language_code, "name": voice_name},
            "audioConfig": {"audioEncoding": "LINEAR16", "sampleRateHertz": sample_rate,
                            "speakingRate": speaking_rate},
        }, headers={"Content-Type": "application/json"}, timeout=timeout)
        response = json.loads(body)
        encoded = response.get("audioContent")
        if not encoded:
            raise RuntimeError(f"Google TTS không trả về audioContent: {str(response)[:300]}")
        self._sample_rate = sample_rate
        return _pcm_wav(base64.b64decode(encoded), sample_rate)

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