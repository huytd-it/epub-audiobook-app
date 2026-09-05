"""API for the local TTS model manager page."""
import base64
import io
import time

import soundfile as sf
from fastapi import APIRouter, HTTPException, Request

from app import tts_model_manager

router = APIRouter(prefix="/tts-models", tags=["tts-models"])


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