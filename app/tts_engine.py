"""Unified lazy-loaded engines for full audiobook TTS.

All five engines -- the three local GPU models plus the two online cloud backends
(Edge TTS, Google Translate TTS) -- share one catalog, one payload shape and one
TTSEngine protocol. Cloud engines synthesize via app.light_tts but return numpy
arrays at a learned sample rate, so they run the exact same audiobook pipeline
(chunk files, resume, book merge, video) as VoxCPM2."""

from __future__ import annotations

import io
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
import soundfile as sf

from app.chunker import split_into_tts_chunks


def _seed_rng(seed: int) -> None:
    """Make sampling reproducible.

    VoxCPM 2.x dropped the per-call `seed` argument from generate() - passing it raises
    TypeError - so the seed has to go onto torch's global RNG right before each call.
    Safe because the worker keeps synthesis strictly sequential."""
    import torch  # heavy import, deferred until first real use

    torch.manual_seed(seed)


class TTSEngine(Protocol):
    @property
    def sample_rate(self) -> int: ...

    def synthesize_chunk(
        self, text: str, reference_wav_path: str | None = None, prompt_text: str | None = None
    ) -> np.ndarray: ...


@dataclass(frozen=True)
class TTSModel:
    id: str
    name: str
    model_id: str
    package: str
    sample_rate: int | None = None
    supports_reference: bool = True
    capabilities: dict = field(default_factory=dict)
    default_voice: str | None = None
    # Voices baked into the model itself, as [{"id", "label", "language"}]. Only engines
    # that ship a fixed cast fill this in; cloud backends enumerate theirs over the network
    # (app.light_tts) and reference-cloning models have no cast at all. Filled in by
    # list_tts_models() rather than stored here, since it comes off disk.
    voices: list = field(default_factory=list)


_CAP_LOCAL = {
    "kind": "local",
    "reference_audio": True,
    "voice_selection": False,
    "offline": True,
    "online": False,
}
_CAP_CLOUD = {
    "kind": "cloud",
    "reference_audio": False,
    "voice_selection": True,
    "offline": False,
    "online": True,
}
# Offline like _CAP_LOCAL, but picks from a fixed cast instead of cloning a reference clip.
# ZeroTTS ships only the decode half of its codec, so there is no way to turn a user's wav
# into voice latents locally -- the published voices are all it can speak in.
_CAP_LOCAL_VOICES = {
    "kind": "local",
    "reference_audio": False,
    "voice_selection": True,
    "offline": True,
    "online": False,
}

_MODELS = {
    "voxcpm2": TTSModel(
        "voxcpm2", "VoxCPM2", "openbmb/VoxCPM2", "voxcpm", 16000, capabilities=_CAP_LOCAL,
    ),
    "omnivoice": TTSModel(
        "omnivoice", "OmniVoice", "k2-fsa/OmniVoice", "omnivoice", 24000, capabilities=_CAP_LOCAL,
    ),
    "vieneu-fast": TTSModel(
        "vieneu-fast", "VieNeu fast", "v3turbo", "vieneu", 48000, capabilities=_CAP_LOCAL,
    ),
    "zerotts": TTSModel(
        "zerotts", "ZeroTTS", "zeroweight-ai/ZeroTTS", "zerotts", 48000,
        supports_reference=False, capabilities=_CAP_LOCAL_VOICES, default_voice="maichi",
    ),
    "edge-tts": TTSModel(
        "edge-tts", "Edge TTS", "edge-tts", "edge-tts", None,
        supports_reference=False, capabilities=_CAP_CLOUD,
        default_voice="vi-VN-HoaiMyNeural",
    ),
    "gtts": TTSModel(
        "gtts", "Google Translate TTS", "gtts", "gtts", None,
        supports_reference=False, capabilities=_CAP_CLOUD, default_voice="vi",
    ),
}


def resolve_engine_id(engine_id: str | None) -> str:
    """Accept an aliased/legacy engine id and validate it against the catalog.

    ``edge-tts`` / ``gtts`` are the canonical ids; ``None`` falls back to the
    configured audiobook engine. Raises ValueError for anything unknown."""
    from app.config import settings

    engine_id = engine_id or settings.tts_engine
    if engine_id not in _MODELS:
        choices = ", ".join(_MODELS)
        raise ValueError(f"Unknown TTS engine {engine_id!r}; choose one of: {choices}")
    return engine_id


def zerotts_model_dir() -> Path:
    """Where the ZeroTTS weights live: the configured override, else <data_root>/zerotts."""
    from app.config import settings

    return Path(settings.zerotts_model_dir or Path(settings.data_root) / "zerotts")


def _zerotts_voices(model_dir: Path) -> list[dict]:
    """The published cast, read from the weights' own voices/index.json.

    Explicit utf-8: the display names are Vietnamese, and the app must not depend on the
    process locale (zerotts' own voices.load_voice does depend on it, which is why the
    engine below never calls it). Returns [] when the weights are missing -- the catalog
    still lists the model so the UI can explain what to download."""
    index = model_dir / "voices" / "index.json"
    if not index.exists():
        return []
    try:
        entries = json.loads(index.read_text(encoding="utf-8")).get("voices", [])
    except (OSError, ValueError):
        return []
    return [
        {
            "id": voice["name"],
            "label": voice.get("description")
            and f"{voice.get('display_name') or voice['name']} — {voice['description']}"
            or (voice.get("display_name") or voice["name"]),
            "language": voice.get("language", ""),
        }
        for voice in entries
        if voice.get("name")
    ]


def list_tts_models() -> list[dict]:
    """Serializable model catalog shared by the UI, worker and remote runtimes."""
    models = []
    for model in _MODELS.values():
        entry = asdict(model)
        if model.id == "zerotts":
            entry["voices"] = _zerotts_voices(zerotts_model_dir())
        models.append(entry)
    return models


def create_tts_engine(engine_id: str = "voxcpm2", **options) -> TTSEngine:
    """Construct any catalog engine. Heavy models import lazily on first synthesis;
    cloud engines take ``voice`` (popped here so local engines never see it)."""
    voice = options.pop("voice", None)
    factories = {
        "voxcpm2": VoxCPMEngine,
        "omnivoice": OmniVoiceEngine,
        "vieneu-fast": VieNeuFastEngine,
        "zerotts": ZeroTTSEngine,
        "edge-tts": EdgeTTSEngine,
        "gtts": GTTSEngine,
    }
    try:
        cls = factories[engine_id]
    except KeyError as exc:
        choices = ", ".join(factories)
        raise ValueError(f"Unknown TTS engine {engine_id!r}; choose one of: {choices}") from exc
    # Engines that pick from a named cast take the voice; reference-cloning ones get the
    # clip through synthesize_chunk instead and must never see this kwarg.
    if not _MODELS[engine_id].supports_reference:
        return cls(voice=voice, **options)
    return cls(**options)


def normalize_tt_payload(payload: dict | None, *, default_engine: str | None = None) -> dict:
    """The one canonical TTS job payload used by every engine and entry point:

        {"patch_id", "tts_engine", "voice", "max_chars", "with_effects"}

    Accepts the legacy shapes unchanged: voxcpm's bare {"patch_id"} and light_tts's
    {"patch_id", "backend", "voice", ...} (``backend`` is kept as an alias for
    ``tts_engine``). ``default_engine`` lets the preview path default to the light
    backend while the audiobook path defaults to ``settings.tts_engine``."""
    from app.config import settings

    p = dict(payload or {})
    engine = p.get("tts_engine") or p.get("backend") or default_engine or settings.tts_engine
    engine = resolve_engine_id(engine)
    p["tts_engine"] = engine
    p["voice"] = p.get("voice") or None
    p["max_chars"] = int(p.get("max_chars") or 0)
    p["with_effects"] = bool(p.get("with_effects"))
    return p


class VoxCPMEngine:
    def __init__(
        self,
        model_id: str = "openbmb/VoxCPM2",
        load_denoiser: bool = False,
        cfg_value: float = 2.0,
        inference_timesteps: int = 10,
        seed: int = 42,
    ):
        self.model_id = model_id
        self.load_denoiser = load_denoiser
        self.cfg_value = cfg_value
        self.inference_timesteps = inference_timesteps
        self.seed = seed
        self._model = None

    def config_fingerprint(self) -> str:
        return (
            f"voxcpm2:{self.model_id}:denoiser={self.load_denoiser}:"
            f"cfg={self.cfg_value}:steps={self.inference_timesteps}:seed={self.seed}"
        )

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
        _seed_rng(self.seed)
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


class OmniVoiceEngine:
    sample_rate = 24000

    def __init__(self, model_id: str = "k2-fsa/OmniVoice", device: str | None = None, seed: int = 42):
        self.model_id = model_id
        self.device = device
        self.seed = seed
        self._model = None

    def config_fingerprint(self) -> str:
        return f"omnivoice:{self.model_id}:device={self.device}:seed={self.seed}"

    def _ensure_loaded(self) -> None:
        if self._model is None:
            import torch
            from omnivoice import OmniVoice

            device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
            dtype = torch.float16 if device.startswith("cuda") else torch.float32
            self._model = OmniVoice.from_pretrained(self.model_id, device_map=device, dtype=dtype)

    def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None) -> np.ndarray:
        self._ensure_loaded()
        kwargs = {}
        if reference_wav_path:
            kwargs["ref_audio"] = reference_wav_path
        if prompt_text:
            kwargs["ref_text"] = prompt_text
        _seed_rng(self.seed)
        audio = self._model.generate(text=text, **kwargs)
        return np.asarray(audio[0] if isinstance(audio, (list, tuple)) else audio, dtype=np.float32)


class VieNeuFastEngine:
    sample_rate = 48000

    def __init__(self, backend: str | None = None, precision: str = "int8", style: str = "doc_truyen"):
        self.backend = backend
        self.precision = precision
        self.style = style
        self._model = None

    def config_fingerprint(self) -> str:
        return f"vieneu-fast:{self.backend}:{self.precision}:{self.style}"

    def _ensure_loaded(self) -> None:
        if self._model is None:
            from vieneu import Vieneu

            kwargs = {"precision": self.precision}
            if self.backend:
                kwargs["backend"] = self.backend
            self._model = Vieneu(**kwargs)

    def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None) -> np.ndarray:
        self._ensure_loaded()
        kwargs = {"style": self.style}
        if reference_wav_path:
            kwargs["ref_audio"] = reference_wav_path
        return np.asarray(self._model.infer(text, **kwargs), dtype=np.float32)


class ZeroTTSEngine:
    """ZeroTTS (local, ONNX, CPU-only -- no torch, no GPU).

    Speaks one of the eight voices published with the weights. The repo ships only the
    decode half of the MOSS codec, so there is no encoder to turn a reference clip into
    voice latents: ``reference_wav_path`` is accepted and ignored, exactly like the cloud
    engines. Voice latents are read straight out of ``voice.npz`` rather than through
    ``zerotts.voices.load_voice``, which reads the voice's meta.json with the process
    locale and therefore raises UnicodeDecodeError on Windows for the Vietnamese
    descriptions."""

    sample_rate = 48000
    supports_reference = False

    def __init__(self, voice: str | None = None, model_dir: str | None = None,
                 threads: int | None = None, **options):
        from app.config import settings

        self.voice = voice or _MODELS["zerotts"].default_voice
        self.model_dir = Path(model_dir) if model_dir else zerotts_model_dir()
        self.threads = threads or settings.zerotts_threads
        self._model = None
        self._voice_emb: np.ndarray | None = None

    def config_fingerprint(self) -> str:
        return f"zerotts:{self.voice}"

    def _load_voice_emb(self, n_voice_queries: int) -> np.ndarray:
        npz = self.model_dir / "voices" / str(self.voice) / "voice.npz"
        if not npz.exists():
            available = sorted(p.parent.name for p in (self.model_dir / "voices").glob("*/voice.npz"))
            raise ValueError(
                f"ZeroTTS không có giọng {self.voice!r} (có: {', '.join(available) or 'không có'})"
            )
        data = np.load(npz)
        emb = np.asarray(data["voice_emb"], dtype=np.float32)
        if emb.ndim == 2:
            emb = emb[None, :, :]
        # Latents built for other weights have the right dtype and rank, so they would feed
        # the graph cleanly and produce confident nonsense. Refuse them instead.
        if emb.shape[1] != n_voice_queries:
            raise ValueError(
                f"giọng {self.voice!r} có n_voice_queries={emb.shape[1]} nhưng model dùng "
                f"{n_voice_queries} — voice pack không khớp với weights trong {self.model_dir}"
            )
        return emb

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        if not (self.model_dir / "config.json").exists():
            raise RuntimeError(
                f"Chưa có weights ZeroTTS ở {self.model_dir}. "
                f"Chạy: python scripts/download_zerotts.py"
            )
        from zerotts import ZeroTTS  # heavy import, deferred until first real use

        self._model = ZeroTTS.from_pretrained(
            str(self.model_dir), intra_op_num_threads=self.threads
        )
        self._voice_emb = self._load_voice_emb(self._model.n_voice_queries)

    def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None) -> np.ndarray:
        self._ensure_loaded()
        audio = self._model.synthesize(text, voice=self._voice_emb)
        return np.asarray(audio, dtype=np.float32).reshape(-1)


def _decode_wav_bytes(wav_bytes: bytes) -> tuple[np.ndarray, int]:
    """Decode WAV bytes to a mono float32 array plus its sample rate."""
    data, sr = sf.read(io.BytesIO(wav_bytes))
    data = np.asarray(data, dtype=np.float32)
    if data.ndim > 1:
        data = data.mean(axis=1)
    return data, sr


class CloudTTSEngine:
    """Base for online backends (Edge TTS, Google Translate TTS) adapted to the
    TTSEngine protocol so they drive the full audiobook pipeline exactly like local
    models. ``sample_rate`` is unknown until the first chunk decodes, then cached --
    every chunk of a fixed voice decodes at the same rate."""
    supports_reference = False

    def __init__(self, voice: str | None = None):
        self.voice = voice
        self._sample_rate: int | None = None

    @property
    def sample_rate(self) -> int:
        if self._sample_rate is None:
            raise RuntimeError("sample_rate is known only after the first chunk is synthesized")
        return self._sample_rate

    def _synth_wav_bytes(self, text: str) -> bytes:
        raise NotImplementedError

    def synthesize_chunk(
        self, text: str, reference_wav_path: str | None = None, prompt_text: str | None = None
    ) -> np.ndarray:
        wav_bytes = self._synth_wav_bytes(text)
        data, sr = _decode_wav_bytes(wav_bytes)
        if self._sample_rate is None:
            self._sample_rate = sr
        return data


class EdgeTTSEngine(CloudTTSEngine):
    """Microsoft Edge TTS (online). Reference audio is unsupported; the voice id is
    the only knob and it is captured by the chunk fingerprint."""

    def __init__(self, voice: str | None = None, **options):
        super().__init__(voice=voice or _MODELS["edge-tts"].default_voice)

    def _synth_wav_bytes(self, text: str) -> bytes:
        from app.light_tts import _edge_tts_synthesize

        wav_bytes, _ = _edge_tts_synthesize(text, self.voice)
        return wav_bytes

    def config_fingerprint(self) -> str:
        return f"edge-tts:{self.voice}"


class GTTSEngine(CloudTTSEngine):
    """Google Translate TTS (online). ``voice`` is a language code like ``vi``."""

    def __init__(self, voice: str | None = None, **options):
        super().__init__(voice=voice or _MODELS["gtts"].default_voice)

    def _synth_wav_bytes(self, text: str) -> bytes:
        from app.light_tts import _gtts_synthesize

        wav_bytes, _ = _gtts_synthesize(text, self.voice)
        return wav_bytes

    def config_fingerprint(self) -> str:
        return f"gtts:{self.voice}"
