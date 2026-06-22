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

    def synthesize_chunk(
        self,
        text: str,
        reference_wav_path: str | None = None,
        prompt_text: str | None = None,
    ) -> np.ndarray:
        self._ensure_loaded()
        kwargs = {}
        if reference_wav_path:
            kwargs["reference_wav_path"] = reference_wav_path
            if prompt_text:
                # "Ultimate cloning" mode: passing the transcript alongside the same clip as
                # both prompt and reference yields closer timbre/prosody matching than
                # reference_wav_path alone.
                kwargs["prompt_wav_path"] = reference_wav_path
                kwargs["prompt_text"] = prompt_text
        return self._model.generate(
            text=text,
            cfg_value=self.cfg_value,
            inference_timesteps=self.inference_timesteps,
            **kwargs,
        )

    def synthesize_patch(
        self,
        text: str,
        max_chars: int = 400,
        reference_wav_path: str | None = None,
        prompt_text: str | None = None,
    ) -> list[np.ndarray]:
        """Chunk patch text and synthesize each chunk; returns the list of wav arrays so the
        caller (audio_merge) can decide how to write them without holding extra copies.

        Passing the same reference_wav_path/prompt_text for every chunk in every patch of a
        book keeps the cloned voice (timbre, pitch, pacing) consistent end-to-end - without it,
        VoxCPM samples a fresh random voice per call, which is why narration used to shift
        between chunks/patches."""
        chunks = split_into_tts_chunks(text, max_chars=max_chars)
        return [
            self.synthesize_chunk(chunk, reference_wav_path=reference_wav_path, prompt_text=prompt_text)
            for chunk in chunks
        ]
