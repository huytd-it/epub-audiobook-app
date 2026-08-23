"""API for the local TTS model manager page."""
from fastapi import APIRouter, HTTPException

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
