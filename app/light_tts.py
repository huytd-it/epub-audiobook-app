"""Lightweight TTS engine for preview: no GPU, fast response, pluggable backends."""
from __future__ import annotations

import asyncio
import io
import time
from typing import Any

from app.config import settings

_BACKENDS: dict[str, dict[str, Any]] = {
    "edge-tts": {
        "description": "Microsoft Edge TTS (online, high quality)",
        "default_voice": "vi-VN-HoaiMyNeural",
    },
    "gtts": {
        "description": "Google Translate TTS (online, simple)",
        "default_voice": "vi",
    },
}

_EDGE_VOICES_CACHE: list[dict[str, Any]] | None = None


def _check_backend(name: str) -> None:
    """Import-check a backend lazily; raise RuntimeError if missing."""
    if name == "edge-tts":
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            raise RuntimeError("edge-tts is not installed. pip install edge-tts")
    elif name == "gtts":
        try:
            from gtts import gTTS  # noqa: F401
        except ImportError:
            raise RuntimeError("gTTS is not installed. pip install gTTS")
    else:
        raise RuntimeError(f"Unknown TTS backend: {name}")


_EDGE_RETRIES = 5


def _edge_tts_synthesize(text: str, voice: str) -> tuple[bytes, int]:
    """Synthesize text via edge-tts, return (wav_bytes, sample_rate).

    The Edge endpoint intermittently closes a stream without sending any audio --
    it shows up as NoAudioReceived on perfectly valid text, typically a few hundred
    requests into an audiobook run. Retrying the same chunk after a short backoff
    succeeds, so absorb it here instead of failing the whole patch job."""
    import edge_tts

    async def _run() -> bytes:
        communicate = edge_tts.Communicate(text, voice)
        buf = io.BytesIO()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    for attempt in range(_EDGE_RETRIES):
        try:
            mp3_bytes = asyncio.run(_run())
        except Exception:
            if attempt == _EDGE_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)
            continue
        if mp3_bytes:
            return _mp3_to_wav_bytes(mp3_bytes)
        if attempt == _EDGE_RETRIES - 1:
            raise RuntimeError(f"edge-tts trả về audio rỗng sau {_EDGE_RETRIES} lần thử")
        time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def _gtts_synthesize(text: str, voice: str) -> tuple[bytes, int]:
    """Synthesize text via gTTS, return (wav_bytes, sample_rate)."""
    from gtts import gTTS

    tts = gTTS(text=text, lang=voice)
    buf = io.BytesIO()
    tts.write_to_fp(buf)
    mp3_bytes = buf.getvalue()
    return _mp3_to_wav_bytes(mp3_bytes)


def _mp3_to_wav_bytes(mp3_bytes: bytes) -> tuple[bytes, int]:
    """Convert MP3 bytes to WAV bytes using soundfile."""
    import soundfile as sf

    audio, sr = sf.read(io.BytesIO(mp3_bytes))
    wav_buf = io.BytesIO()
    sf.write(wav_buf, audio, sr, format="WAV")
    return wav_buf.getvalue(), sr


_BACKEND_SYNTH: dict[str, Any] = {
    "edge-tts": _edge_tts_synthesize,
    "gtts": _gtts_synthesize,
}


def _edge_list_voices_raw() -> list[dict[str, Any]]:
    """Fetch the raw edge-tts voice list (network call)."""
    import edge_tts

    async def _run() -> list[dict[str, Any]]:
        return await edge_tts.list_voices()

    return asyncio.run(_run())


def _gtts_langs() -> dict[str, str]:
    from gtts.lang import tts_langs

    return tts_langs()


def _fallback_voice(backend: str) -> list[dict[str, Any]]:
    dv = _BACKENDS[backend]["default_voice"]
    return [{"id": dv, "label": dv, "language": ""}]


def _edge_voices() -> list[dict[str, Any]]:
    global _EDGE_VOICES_CACHE
    if _EDGE_VOICES_CACHE is None:
        raw = _edge_list_voices_raw()
        voices = [
            {
                "id": v["ShortName"],
                "label": f"{v['ShortName']} ({v.get('Gender', '')})",
                "language": v.get("Locale", ""),
            }
            for v in raw
        ]
        # Vietnamese first, then by locale, then by id.
        voices.sort(key=lambda v: (not v["language"].startswith("vi-VN"), v["language"], v["id"]))
        _EDGE_VOICES_CACHE = voices
    return _EDGE_VOICES_CACHE


def list_voices(backend: str) -> list[dict[str, Any]]:
    """Return selectable voices for a backend. Never raises for a known backend;
    falls back to the backend's default_voice on any enumeration failure."""
    try:
        if backend == "edge-tts":
            voices = _edge_voices()
        elif backend == "gtts":
            voices = [
                {"id": code, "label": name, "language": code}
                for code, name in sorted(_gtts_langs().items(), key=lambda kv: kv[1])
            ]
        else:
            return _fallback_voice(backend)
        return voices or _fallback_voice(backend)
    except Exception:
        return _fallback_voice(backend)


class LightTTSEngine:
    """Lightweight TTS for preview. No GPU, supports pluggable backends."""

    def __init__(self, backend: str = "edge-tts", voice: str | None = None):
        _check_backend(backend)
        self.backend = backend
        self.voice = voice or _BACKENDS[backend]["default_voice"]

    def list_backends(self) -> list[dict[str, Any]]:
        result = []
        for name, info in _BACKENDS.items():
            available = True
            try:
                _check_backend(name)
            except RuntimeError:
                available = False
            result.append({"name": name, "available": available, **info})
        return result

    def synthesize_to_wav_bytes(self, text: str, voice: str | None = None) -> tuple[bytes, int]:
        """Synthesize text to WAV bytes. Returns (wav_bytes, sample_rate)."""
        synth_fn = _BACKEND_SYNTH[self.backend]
        return synth_fn(text, voice or self.voice)
