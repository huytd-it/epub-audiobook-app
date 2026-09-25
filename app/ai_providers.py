"""Extensible generative-AI provider registry (content + thumbnail).

Design: the rest of the app only talks to the small functions in this module
(:func:`generate_text`, :func:`generate_image`, :func:`is_configured`) plus the
task builders in ``app/ai_content.py``. Adding a new backend (Gemini,
Anthropic, a local server, ...) means:

1. subclass :class:`AIProvider` (implement ``generate_text``/``generate_image``),
2. call :func:`register_ai_provider` with its adapter name,
3. optionally expose its knobs in ``app/config.py`` / ``AI_API_PROVIDERS``.

The built-in ``openai`` adapter also serves any OpenAI-compatible server via
``base_url`` (registered under the ``custom`` id), so self-hosted gateways
work without code changes. Secrets always come from environment variables
(``OPENAI_API_KEY`` / ``AI_API_KEY`` / per-provider ``api_key_env``) or the
UI-managed key file — never from the DB or logs.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

_DEFAULT_OPENAI_BASE = "https://api.openai.com/v1"


# ---------------------------------------------------------------------------
# Provider configs
# ---------------------------------------------------------------------------

def _custom_key_file() -> Path:
    from app.config import settings

    return Path(settings.data_root) / "ai_custom_providers.json"


def _read_custom_providers() -> list[dict[str, Any]]:
    path = _custom_key_file()
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _env_providers() -> list[dict[str, Any]]:
    from app.config import settings

    raw = (settings.ai_api_providers or "").strip()
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"AI_API_PROVIDERS không phải JSON hợp lệ: {exc}") from exc
    if not isinstance(value, list):
        raise ValueError("AI_API_PROVIDERS phải là một JSON array")
    return [item for item in value if isinstance(item, dict)]


@dataclass
class ResolvedAIProvider:
    """One provider with all defaults applied — what adapters receive."""

    id: str = "openai"
    adapter: str = "openai"  # openai | custom (+ future: gemini, anthropic, ...)
    api_key: str = ""
    base_url: str = _DEFAULT_OPENAI_BASE
    text_model: str = "gpt-4o-mini"
    image_model: str = "gpt-image-1"
    extra: dict[str, Any] = field(default_factory=dict)


def _default_key_env(adapter: str) -> str:
    return {"custom": "AI_API_KEY"}.get(adapter, "OPENAI_API_KEY")


def resolve_provider(provider_id: str | None = None) -> ResolvedAIProvider:
    """Resolve a provider id to concrete settings (env defaults + overrides)."""
    from app.config import settings

    wanted = (provider_id or settings.ai_provider or "openai").strip().lower() or "openai"
    merged: dict[str, Any] = {}
    for item in (*_env_providers(), *_read_custom_providers()):
        if str(item.get("id") or "").strip().lower() == wanted:
            merged = {**merged, **item}
    adapter = str(merged.get("adapter") or wanted if wanted in {"openai", "custom"} else merged.get("adapter") or "openai").strip().lower()
    if wanted in {"openai", "custom"} and "adapter" not in merged:
        adapter = wanted
    api_key = str(merged.get("api_key") or "").strip()
    if not api_key:
        api_key = os.getenv(str(merged.get("api_key_env") or _default_key_env(adapter)), "")
    if not api_key and adapter in {"openai", "custom"}:
        # Fall back to the well-known env var so OPENAI_API_KEY alone just works.
        api_key = os.getenv("OPENAI_API_KEY" if adapter == "openai" else "AI_API_KEY", "")
    base_url = str(merged.get("base_url") or settings.ai_base_url or _DEFAULT_OPENAI_BASE).rstrip("/") or _DEFAULT_OPENAI_BASE
    return ResolvedAIProvider(
        id=wanted,
        adapter=adapter,
        api_key=api_key,
        base_url=base_url,
        text_model=str(merged.get("text_model") or settings.ai_text_model or "gpt-4o-mini"),
        image_model=str(merged.get("image_model") or settings.ai_image_model or "gpt-image-1"),
        extra={k: v for k, v in merged.items() if k not in {"id", "adapter", "api_key", "api_key_env", "base_url", "text_model", "image_model"}},
    )


def is_configured(provider_id: str | None = None) -> bool:
    return bool(resolve_provider(provider_id).api_key)


def list_providers() -> list[dict[str, Any]]:
    """Provider catalog for the UI (keys redacted to a boolean)."""
    from app.config import settings

    seen: dict[str, dict[str, Any]] = {
        "openai": {"id": "openai", "adapter": "openai", "text_model": settings.ai_text_model,
                   "image_model": settings.ai_image_model, "key_env": "OPENAI_API_KEY"},
        "custom": {"id": "custom", "adapter": "custom", "text_model": settings.ai_text_model,
                   "image_model": settings.ai_image_model, "key_env": "AI_API_KEY"},
    }
    for item in (*_env_providers(), *_read_custom_providers()):
        pid = str(item.get("id") or "").strip()
        if pid and pid not in seen:
            seen[pid] = {"id": pid, "adapter": str(item.get("adapter") or "openai"),
                         "text_model": str(item.get("text_model") or settings.ai_text_model),
                         "image_model": str(item.get("image_model") or settings.ai_image_model),
                         "key_env": str(item.get("api_key_env") or _default_key_env(str(item.get("adapter") or "openai")))}
    out = []
    for pid, info in seen.items():
        try:
            configured = bool(resolve_provider(pid).api_key)
        except ValueError:
            configured = False
        out.append({**info, "configured": configured, "active": pid == (settings.ai_provider or "openai")})
    return out


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — no new dependencies)
# ---------------------------------------------------------------------------

def _request_json(url: str, *, api_key: str, payload: dict[str, Any], timeout: float,
                  key_header: str = "Authorization") -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key_header == "Authorization":
        headers["Authorization"] = f"Bearer {api_key}"
    else:
        headers[key_header] = api_key
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(2048).decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise RuntimeError(f"AI API lỗi HTTP {exc.code}: {detail[:500]}") from exc


# ---------------------------------------------------------------------------
# Provider interface + registry
# ---------------------------------------------------------------------------

class AIProvider:
    """Interface every backend implements. Register subclasses via
    :func:`register_ai_provider` — callers never import them directly."""

    def generate_text(self, cfg: ResolvedAIProvider, *, system: str, user: str,
                      timeout: float, max_tokens: int = 1500,
                      response_format: str = "text") -> str:
        raise NotImplementedError

    def generate_image(self, cfg: ResolvedAIProvider, *, prompt: str, timeout: float,
                       size: str = "1024x1024") -> bytes:
        raise NotImplementedError


_REGISTRY: dict[str, Callable[[], AIProvider]] = {}


def register_ai_provider(adapter: str, factory: Callable[[], AIProvider]) -> None:
    """Extension point: plug a new backend under an adapter name."""
    _REGISTRY[adapter.strip().lower()] = factory


def _provider_for(cfg: ResolvedAIProvider) -> AIProvider:
    factory = _REGISTRY.get(cfg.adapter)
    if factory is None:
        raise ValueError(
            f"AI adapter {cfg.adapter!r} chưa được hỗ trợ "
            f"(đã có: {', '.join(sorted(_REGISTRY)) or '—'})"
        )
    return factory()


class OpenAICompatibleProvider(AIProvider):
    """Chat-Completions + Images backends (OpenAI + any compatible gateway)."""

    def generate_text(self, cfg, *, system, user, timeout, max_tokens=1500, response_format="text") -> str:
        if not cfg.api_key:
            raise RuntimeError("Thiếu API key: đặt OPENAI_API_KEY (hoặc AI_API_KEY cho custom) trong .env")
        payload: dict[str, Any] = {
            "model": cfg.text_model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.7,
        }
        if response_format == "json":
            payload["response_format"] = {"type": "json_object"}
        body = _request_json(f"{cfg.base_url}/chat/completions", api_key=cfg.api_key,
                             payload=payload, timeout=timeout)
        try:
            return str(body["choices"][0]["message"]["content"] or "").strip()
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"AI trả về nội dung không mong đợi: {str(body)[:300]}") from exc

    def generate_image(self, cfg, *, prompt, timeout, size="1024x1024") -> bytes:
        if not cfg.api_key:
            raise RuntimeError("Thiếu API key: đặt OPENAI_API_KEY (hoặc AI_API_KEY cho custom) trong .env")
        payload: dict[str, Any] = {"model": cfg.image_model, "prompt": prompt, "size": size}
        body = _request_json(f"{cfg.base_url}/images/generations", api_key=cfg.api_key,
                             payload=payload, timeout=timeout)
        try:
            data = body["data"][0]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"AI trả về ảnh không mong đợi: {str(body)[:300]}") from exc
        if isinstance(data, dict) and data.get("b64_json"):
            return base64.b64decode(data["b64_json"])
        url = data.get("url") if isinstance(data, dict) else None
        if not url:
            raise RuntimeError("AI không trả về URL/b64 ảnh")
        req = urllib.request.Request(str(url), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            raise RuntimeError(f"Không tải được ảnh AI: {exc}") from exc


register_ai_provider("openai", OpenAICompatibleProvider)
register_ai_provider("custom", OpenAICompatibleProvider)


# ---------------------------------------------------------------------------
# Facade used by the rest of the app
# ---------------------------------------------------------------------------

def generate_text(*, system: str, user: str, provider_id: str | None = None,
                  model: str | None = None, timeout: float | None = None,
                  max_tokens: int = 1500, response_format: str = "text") -> str:
    from app.config import settings

    cfg = resolve_provider(provider_id)
    if model:
        cfg.text_model = model
    return _provider_for(cfg).generate_text(
        cfg, system=system, user=user,
        timeout=timeout or settings.ai_timeout_seconds,
        max_tokens=max_tokens, response_format=response_format,
    )


def generate_image_bytes(*, prompt: str, provider_id: str | None = None,
                         model: str | None = None, timeout: float | None = None,
                         size: str = "1024x1024") -> bytes:
    from app.config import settings

    cfg = resolve_provider(provider_id)
    if model:
        cfg.image_model = model
    return _provider_for(cfg).generate_image(
        cfg, prompt=prompt, timeout=timeout or settings.ai_timeout_seconds, size=size,
    )
