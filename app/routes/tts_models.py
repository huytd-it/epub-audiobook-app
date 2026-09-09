"""API for the local TTS model manager page."""
import base64
import io
import time

import soundfile as sf
from fastapi import APIRouter, HTTPException, Request

from app import tts_model_manager

router = APIRouter(prefix="/tts-models", tags=["tts-models"])


@router.get("/providers")
def list_custom_providers():
    from app import tts_api_providers

    return {"providers": tts_api_providers.list_custom_providers()}


@router.post("/providers")
async def create_custom_provider(request: Request):
    from app import tts_api_providers

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "payload must be an object")
    try:
        saved = tts_api_providers.save_custom_provider(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    sanitized = {key: value for key, value in saved.items() if key != "api_key"}
    sanitized["has_api_key"] = bool(saved.get("api_key"))
    sanitized["custom"] = True
    return {"provider": sanitized}


@router.post("/providers/test")
async def test_custom_provider(request: Request):
    """Synthesize a short sample with an unsaved provider payload (test before save)."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "payload must be an object")
    config = body.get("config") if isinstance(body.get("config"), dict) else body
    text = str(body.get("text") or "Xin chào, đây là bản nghe thử.").strip()[:300]
    voice = str(body.get("voice") or config.get("voice") or "").strip() or None
    if not text:
        raise HTTPException(400, "Cần nội dung text để test")
    from app import tts_api_providers

    try:
        normalized = tts_api_providers.validate_provider_payload(dict(config), is_update=True)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if voice:
        normalized["voice"] = voice
    if body.get("api_key"):
        normalized["api_key"] = str(body["api_key"])
    engine = tts_api_providers.ApiTTSEngine.__new__(tts_api_providers.ApiTTSEngine)
    engine.engine_id = normalized.get("id") or "test-provider"
    engine.config = normalized
    engine.voice = normalized.get("voice")
    engine._sample_rate = int(normalized.get("sample_rate") or 24000)
    try:
        started = time.perf_counter()
        audio = engine.synthesize_chunk(text)
        elapsed = time.perf_counter() - started
        sample_rate = int(engine.sample_rate)
        output = io.BytesIO()
        sf.write(output, audio, sample_rate, format="WAV")
        duration = len(audio) / sample_rate if sample_rate else 0
        return {
            "audio_base64": base64.b64encode(output.getvalue()).decode("ascii"),
            "mime_type": "audio/wav",
            "sample_rate": sample_rate,
            "latency_seconds": round(elapsed, 3),
            "duration_seconds": round(duration, 3),
            "realtime_factor": round(elapsed / duration, 3) if duration else None,
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Test provider thất bại: {exc}") from exc


@router.put("/providers/{provider_id}")
async def update_custom_provider(provider_id: str, request: Request):
    from app import tts_api_providers

    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "payload must be an object")
    try:
        saved = tts_api_providers.save_custom_provider(body, provider_id=provider_id)
    except KeyError as exc:
        raise HTTPException(404, f"Không tìm thấy provider {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    sanitized = {key: value for key, value in saved.items() if key != "api_key"}
    sanitized["has_api_key"] = bool(saved.get("api_key"))
    sanitized["custom"] = True
    return {"provider": sanitized}


@router.delete("/providers/{provider_id}")
def delete_custom_provider(provider_id: str):
    from app import tts_api_providers

    try:
        tts_api_providers.delete_custom_provider(provider_id)
    except KeyError as exc:
        raise HTTPException(404, f"Không tìm thấy provider {exc}") from exc
    return {"status": "ok", "id": provider_id}


@router.get("")
def list_tts_models():
    return {"models": tts_model_manager.list_models()}


@router.post("/{model_id}/download")
def download_tts_model(model_id: str, update: bool = False):
    try:
        return tts_model_manager.start_download(model_id, update=update)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/playground")
async def tts_playground(request: Request):
    """Run a short interactive synthesis outside the production queue."""
    body = await request.json()
    text = str(body.get("text") or "").strip() if isinstance(body, dict) else ""
    engine_id = str(body.get("model_id") or "").strip() if isinstance(body, dict) else ""
    voice = (str(body.get("voice") or "").strip() or None) if isinstance(body, dict) else None
    if not text or len(text) > 1200:
        raise HTTPException(400, "Nội dung nghe thử phải có từ 1 đến 1200 ký tự")
    try:
        from app.tts_engine import create_tts_engine, resolve_engine_id

        engine_id = resolve_engine_id(engine_id)
        started = time.perf_counter()
        engine = create_tts_engine(engine_id, voice=voice)
        audio = engine.synthesize_chunk(text)
        elapsed = time.perf_counter() - started
        sample_rate = int(engine.sample_rate)
        output = io.BytesIO()
        sf.write(output, audio, sample_rate, format="WAV")
        duration = len(audio) / sample_rate if sample_rate else 0
        return {
            "audio_base64": base64.b64encode(output.getvalue()).decode("ascii"),
            "mime_type": "audio/wav",
            "model_id": engine_id,
            "sample_rate": sample_rate,
            "latency_seconds": round(elapsed, 3),
            "duration_seconds": round(duration, 3),
            "realtime_factor": round(elapsed / duration, 3) if duration else None,
            "characters_per_second": round(len(text) / elapsed, 1) if elapsed else None,
        }
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, f"Không thể tạo audio nghe thử: {exc}") from exc