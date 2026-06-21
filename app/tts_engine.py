"""VoxCPM2 wrapper: lazy-loaded singleton model, used exclusively by the worker so GPU usage
stays strictly sequential (one synthesis call in flight at a time)."""
from __future__ import annotations

import numpy as np

from app.chunker import split_into_tts_chunks


class VoxCPMEngine:
    def __init__(
        self,
        model_id: str = "openbmb/VoxCPM2",
        load_denoiser: bool = False,
        cfg_value: float = 2.0,
        inference_timesteps: int = 10,
    ):
        self.model_id = model_id
        self.load_denoiser = load_denoiser
        self.cfg_value = cfg_value
        self.inference_timesteps = inference_timesteps
        self._model = None

    def _ensure_loaded(self) -> None:
        if self._model is None:
            from voxcpm import VoxCPM  # heavy import, deferred until first real use

            self._model = VoxCPM.from_pretrained(self.model_id, load_denoiser=self.load_denoiser)

    @property
    def sample_rate(self) -> int:
        self._ensure_loaded()
        return self._model.tts_model.sample_rate

    def synthesize_chunk(self, text: str) -> np.ndarray:
        self._ensure_loaded()
        return self._model.generate(
            text=text,
            cfg_value=self.cfg_value,
            inference_timesteps=self.inference_timesteps,
        )

    def synthesize_patch(self, text: str, max_chars: int = 400) -> list[np.ndarray]:
        """Chunk patch text and synthesize each chunk; returns the list of wav arrays so the
        caller (audio_merge) can decide how to write them without holding extra copies."""
        chunks = split_into_tts_chunks(text, max_chars=max_chars)
        return [self.synthesize_chunk(chunk) for chunk in chunks]
