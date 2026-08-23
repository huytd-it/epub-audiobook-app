"""Unified lazy-loaded engines for full audiobook TTS.

All five engines -- the three local GPU models plus the two online cloud backends
(Edge TTS, Google Translate TTS) -- share one catalog, one payload shape and one
TTSEngine protocol. Cloud engines synthesize via app.light_tts but return numpy
arrays at a learned sample rate, so they run the exact same audiobook pipeline
(chunk files, resume, book merge, video) as VoxCPM2."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
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
    # Declarative controls rendered by the UI.  Engines without real inference
    # controls deliberately expose no schema, keeping the advanced section out
    # of their forms instead of showing misleading disabled inputs.
    options_schema: list[dict] = field(default_factory=list)


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
# ZeroTTS has no choice -- it ships only the decode half of its codec, so a user's wav can
# never become voice latents. VieNeu could clone, but a curated preset is what keeps one
# narrator identical across every chunk and patch of a book, which is the whole job here.
_CAP_LOCAL_VOICES = {
    "kind": "local",
    "reference_audio": False,
    "voice_selection": True,
    "offline": True,
    "online": False,
}

_CONFUCIUS_OPTIONS = [
    {"key": "lang", "label": "Ngôn ngữ", "type": "select", "default": "vi",
     "choices": [{"value": "vi", "label": "Tiếng Việt"}, {"value": "zh", "label": "Chinese"},
                 {"value": "en", "label": "English"}, {"value": "ja", "label": "Japanese"},
                 {"value": "ko", "label": "Korean"}, {"value": "th", "label": "Thai"}]},
    {"key": "device", "label": "Thiết bị", "type": "select", "default": "auto",
     "choices": [{"value": "auto", "label": "Tự động"}, {"value": "cuda", "label": "CUDA"},
                 {"value": "cpu", "label": "CPU"}]},
]

_F5_VIVOICE_OPTIONS = [
    {"key": "speed", "label": "Tốc độ", "type": "number", "default": 1.0, "min": 0.3, "max": 2.0, "step": 0.05},
    {"key": "nfe_step", "label": "Số bước suy luận", "type": "number", "default": 32, "min": 4, "max": 128, "step": 1},
    {"key": "cfg_strength", "label": "CFG strength", "type": "number", "default": 2.0, "min": 0.0, "max": 10.0, "step": 0.1},
    {"key": "sway_sampling_coef", "label": "Sway sampling", "type": "number", "default": -1.0, "min": -1.0, "max": 2.0, "step": 0.05},
    {"key": "device", "label": "Thiết bị", "type": "select", "default": "auto",
     "choices": [{"value": "auto", "label": "Tự động"}, {"value": "cuda", "label": "CUDA"},
                 {"value": "cpu", "label": "CPU"}]},
]

_MODELS = {
    "voxcpm2": TTSModel(
        # 48 kHz: the model takes a 16 kHz reference but its AudioVAE decoder upsamples.
        "voxcpm2", "VoxCPM2", "openbmb/VoxCPM2", "voxcpm", 48000, capabilities=_CAP_LOCAL,
    ),
    "omnivoice": TTSModel(
        "omnivoice", "OmniVoice", "k2-fsa/OmniVoice", "omnivoice", 24000, capabilities=_CAP_LOCAL,
    ),
    "confucius4": TTSModel(
        "confucius4", "Confucius4-TTS", "netease-youdao/Confucius4-TTS", "confuciustts", 22050,
        capabilities=_CAP_LOCAL, options_schema=_CONFUCIUS_OPTIONS,
    ),
    "f5-vivoice": TTSModel(
        "f5-vivoice", "F5-TTS Vietnamese ViVoice", "hynt/F5-TTS-Vietnamese-ViVoice", "f5-tts", 24000,
        capabilities=_CAP_LOCAL, options_schema=_F5_VIVOICE_OPTIONS,
    ),
    "vieneu-fast": TTSModel(
        "vieneu-fast", "VieNeu fast", "pnnbao-ump/VieNeu-TTS-v3-Turbo", "vieneu", 48000,
        supports_reference=False, capabilities=_CAP_LOCAL_VOICES, default_voice="Adam",
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


def _vieneu_voices() -> list[dict]:
    """VieNeu's curated presets, read from the installed package's own asset file.

    ``vieneu/assets/voices_v3_turbo.json`` ships inside the wheel, so the cast is known
    without downloading the weights or importing vieneu (which pulls onnxruntime). Returns
    [] when the package is absent -- the catalog still lists the model."""
    try:
        import importlib.util

        spec = importlib.util.find_spec("vieneu")
    except (ImportError, ValueError):
        return []
    if not spec or not spec.origin:
        return []
    index = Path(spec.origin).parent / "assets" / "voices_v3_turbo.json"
    if not index.exists():
        return []
    try:
        presets = json.loads(index.read_text(encoding="utf-8")).get("presets", {})
    except (OSError, ValueError):
        return []
    return [
        {
            "id": name,
            "label": f"{name} — {info['description']}" if info.get("description") else name,
            "language": "vi",
        }
        for name, info in presets.items()
    ]


# Where each fixed-cast engine's voice list comes from. Also the set of engines a preset
# reference voice can be borrowed from (see preset_reference_clip below).
_VOICE_SOURCES = {
    "zerotts": lambda: _zerotts_voices(zerotts_model_dir()),
    "vieneu-fast": _vieneu_voices,
}

# A preset voice can also stand in as the *reference clip* for the cloning models: rendering
# one line with VieNeu/ZeroTTS gives VoxCPM/OmniVoice a clip whose transcript is known
# exactly (which is what their prompt/cloning mode wants) and which is identical on every
# run, so a book keeps one narrator without anyone having to record and upload a sample.
# Selected as "preset:<engine id>:<voice id>" - a shape no library filename can take, so it
# never collides with the uploaded clips in the same dropdown.
PRESET_VOICE_PREFIX = "preset:"

# The line read to build that clip: two sentences (~10s), long enough to carry timbre and
# pacing, short enough that the cloning models treat it as a prompt rather than as material
# to continue. Changing it re-renders every cached clip (the text is part of the filename).
PRESET_REFERENCE_TEXT = (
    "Trời vừa hửng sáng, gió từ mặt sông thổi vào mát rượi. "
    "Người kể chuyện dừng lại một nhịp rồi đọc tiếp trang sách còn dang dở."
)


def parse_preset_voice(voice: str | None) -> tuple[str, str] | None:
    """Split ``preset:<engine>:<voice>`` into (engine id, voice id).

    Returns None for everything else - a library filename, a cloud voice id, or no
    selection - so callers can treat "is this a preset reference?" as one question. A value
    that *is* prefixed but names no real engine/voice raises instead of falling back: it can
    only come from an explicit pick, and silently narrating a whole book in some other voice
    is worse than refusing the job."""
    if not voice or not str(voice).startswith(PRESET_VOICE_PREFIX):
        return None
    engine_id, _, voice_id = str(voice)[len(PRESET_VOICE_PREFIX):].partition(":")
    if engine_id not in _VOICE_SOURCES or not voice_id:
        choices = ", ".join(_VOICE_SOURCES)
        raise ValueError(
            f"Giọng preset {voice!r} không hợp lệ; dạng đúng là "
            f"'{PRESET_VOICE_PREFIX}<engine>:<voice>' với engine thuộc: {choices}"
        )
    return engine_id, voice_id


def preset_reference_options() -> list[dict]:
    """Every preset voice offered as a reference clip, as [{"value", "label", "language"}].

    The UI merges these into the voice dropdown of the cloning models; ``value`` is exactly
    what has to come back as the job's ``voice``."""
    options = []
    for engine_id, source in _VOICE_SOURCES.items():
        engine_name = _MODELS[engine_id].name
        for voice in source():
            options.append({
                "value": f"{PRESET_VOICE_PREFIX}{engine_id}:{voice['id']}",
                "label": f"{engine_name} · {voice.get('label') or voice['id']}",
                "language": voice.get("language", ""),
            })
    return options


def _preset_clip_path(engine_id: str, voice_id: str, transcript: str) -> Path:
    """Cache path for one rendered preset clip.

    Lives in a subdirectory of the voice library rather than in it: the library routes
    refuse names containing a separator and /api/ui/media lists files only, so these
    generated clips can never be renamed, deleted or tagged as if a user had uploaded them.
    The transcript hash is part of the name so editing PRESET_REFERENCE_TEXT renders a fresh
    clip instead of pairing new prompt text with old audio."""
    from app.config import settings

    stamp = hashlib.md5(f"{voice_id}|{transcript}".encode("utf-8")).hexdigest()[:8]
    safe = re.sub(r"[^\w.-]+", "_", voice_id, flags=re.UNICODE).strip("_") or "voice"
    return Path(settings.data_root) / "voices" / "_presets" / f"{engine_id}__{safe}-{stamp}.wav"


def preset_reference_clip(voice: str, transcript: str | None = None) -> tuple[str, str]:
    """Render (once) the reference clip for a ``preset:...`` voice; returns (path, transcript).

    Rendered on the first chunk that needs it and reused by every later chunk, patch and
    export - handing the cloning model the same file every time is what keeps one narrator
    across a whole book."""
    engine_id, voice_id = parse_preset_voice(voice)
    transcript = transcript or PRESET_REFERENCE_TEXT
    dest = _preset_clip_path(engine_id, voice_id, transcript)
    if dest.is_file():
        return str(dest), transcript

    engine = create_tts_engine(engine_id, voice=voice_id)
    audio = np.asarray(engine.synthesize_chunk(transcript), dtype=np.float32).reshape(-1)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Write beside the target and rename: a half-written wav left by a crash (or picked up
    # by the other process while the app and the worker both run) would be cloned as-is.
    staging = dest.with_name(f"{dest.name}.{os.getpid()}.part")
    sf.write(str(staging), audio, engine.sample_rate, format="WAV")  # ".part" hides the format
    staging.replace(dest)
    return str(dest), transcript


def voice_library_clip(voice: str) -> Path:
    """The library clip a cloning model's voice id names: a bare filename under
    ``<data_root>/voices``.

    Cloning models have no cast of their own, so a voice that is not a preset id can only
    be one of the uploaded clips. Refusing a name that escapes the library or is no longer
    there - with the message the Colab/Kaggle export has always used - is what makes a
    stale selection fail the same way whether the book is synthesized here or remotely."""
    from app.config import settings

    name = Path(voice).name
    clip = Path(settings.data_root) / "voices" / name
    if name != voice or not clip.is_file():
        raise ValueError(f"unknown reference voice: {voice}")
    return clip


def _is_same_clip(path: str | None, clip: Path) -> bool:
    return bool(path) and Path(path).resolve() == clip.resolve()


def resolve_reference(
    voice: str | None, reference_wav_path: str | None, prompt_text: str | None
) -> tuple[str | None, str | None]:
    """The clip and transcript a cloning engine should actually use.

    The picked voice beats the book's own clip - it is the explicit choice for this run -
    and both entry points (this app, and the export packages built for Colab/Kaggle) read
    it the same way, so a book sounds identical wherever it is synthesized:

    * ``preset:<engine>:<voice>`` - render the preset clip, transcript known word for word;
    * a filename - that library clip, keeping ``prompt_text`` only when it really is the
      book's own clip (``book.voice_transcript`` describes that one, and pairing it with
      any other clip would hand the model words its audio does not say);
    * nothing picked - the book's clip and transcript, untouched."""
    if parse_preset_voice(voice):
        return preset_reference_clip(voice)
    if not voice:
        return reference_wav_path, prompt_text
    clip = voice_library_clip(voice)
    return str(clip), prompt_text if _is_same_clip(reference_wav_path, clip) else None


def requires_book_reference(engine_id: str, voice: str | None) -> bool:
    """True when a run still needs the book's own clip: a cloning model with no voice picked.

    Any pick - a library clip or a preset - already names the reference (resolve_reference),
    and cloud and fixed-cast engines speak their own voice, so none of those need one."""
    return _MODELS[engine_id].supports_reference and not voice


def list_tts_models() -> list[dict]:
    """Serializable model catalog shared by the UI, worker and remote runtimes."""
    models = []
    for model in _MODELS.values():
        entry = asdict(model)
        source = _VOICE_SOURCES.get(model.id)
        if source:
            entry["voices"] = source()
        models.append(entry)
    return models


def create_tts_engine(engine_id: str = "voxcpm2", **options) -> TTSEngine:
    """Construct any catalog engine. Heavy models import lazily on first synthesis;
    ``voice`` reaches every engine (see below for what each does with it)."""
    voice = options.pop("voice", None)
    factories = {
        "voxcpm2": VoxCPMEngine,
        "omnivoice": OmniVoiceEngine,
        "confucius4": Confucius4Engine,
        "f5-vivoice": F5ViVoiceEngine,
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
    # Every engine takes the voice, but they read it differently: fixed-cast and cloud
    # engines speak it, while a cloning engine only acts on it when it names a preset voice
    # to borrow a reference clip from (resolve_reference) - otherwise its clip still arrives
    # per chunk from the book.
    return cls(voice=voice, **options)


def normalize_tt_payload(payload: dict | None, *, default_engine: str | None = None) -> dict:
    """The one canonical TTS job payload used by every engine and entry point:

        {"patch_id", "tts_engine", "voice", "max_chars", "with_effects", "tts_options",
         "chunk_pause_ms", "chapter_pause_ms"}

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
    p["tts_options"] = normalize_tts_options(engine, p.get("tts_options"))
    # The merge pauses ride along with the rest of the audio config so a job
    # enqueued yesterday keeps the spacing it was queued with. Missing values
    # (every payload written before this shipped) fall back to the defaults.
    from app import audio_merge

    p["chunk_pause_ms"] = _pause_value(p.get("chunk_pause_ms"), audio_merge.DEFAULT_CHUNK_PAUSE_MS)
    p["chapter_pause_ms"] = _pause_value(p.get("chapter_pause_ms"), audio_merge.DEFAULT_CHAPTER_PAUSE_MS)
    return p


def normalize_tts_options(engine_id: str, raw: object) -> dict:
    """Keep only documented controls and clamp numeric values to their schema.

    This is also the trust boundary for persisted automation JSON and queue
    payloads: arbitrary constructor arguments must never be accepted from the UI.
    """
    supplied = raw if isinstance(raw, dict) else {}
    model = _MODELS.get(engine_id)
    if model is None:
        return {}
    schema = model.options_schema
    result = {}
    for field_spec in schema:
        key = field_spec["key"]
        value = supplied.get(key, field_spec.get("default"))
        if field_spec.get("type") == "number":
            try:
                value = float(value)
            except (TypeError, ValueError):
                value = float(field_spec.get("default", 0))
            value = max(float(field_spec.get("min", value)), min(float(field_spec.get("max", value)), value))
            if field_spec.get("step") == 1:
                value = int(round(value))
        elif field_spec.get("type") == "select":
            choices = {str(choice["value"]) for choice in field_spec.get("choices", [])}
            value = str(value)
            if value not in choices:
                value = field_spec.get("default", "")
        result[key] = value
    return result


def _pause_value(raw, default: int) -> int:
    if raw is None or raw == "":
        return default
    try:
        return max(0, min(30000, int(round(float(raw)))))
    except (TypeError, ValueError):
        return default


class VoxCPMEngine:
    def __init__(
        self,
        model_id: str = "openbmb/VoxCPM2",
        load_denoiser: bool = False,
        cfg_value: float = 2.0,
        inference_timesteps: int = 10,
        seed: int = 42,
        voice: str | None = None,
        **options,
    ):
        # Names the clip to clone: a library filename, or "preset:..." for a VieNeu/ZeroTTS
        # preset voice. Unset falls back to the book's clip (see resolve_reference).
        self.voice = voice
        self.model_id = model_id
        self.load_denoiser = load_denoiser
        self.cfg_value = cfg_value
        self.inference_timesteps = inference_timesteps
        self.seed = seed
        self._model = None

    def config_fingerprint(self) -> str:
        base = (
            f"voxcpm2:{self.model_id}:denoiser={self.load_denoiser}:"
            f"cfg={self.cfg_value}:steps={self.inference_timesteps}:seed={self.seed}"
        )
        # The picked voice decides which clip is cloned, so it keys the chunk cache too:
        # resuming chunks recorded against another reference would mix two narrators inside
        # one patch. Books that never picked one keep their original fingerprint.
        return f"{base}:ref={self.voice}" if self.voice else base

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
        # Resolve before loading: a preset reference briefly loads VieNeu/ZeroTTS to render
        # its clip, and doing that first keeps the two models out of memory at once.
        reference_wav_path, prompt_text = resolve_reference(self.voice, reference_wav_path, prompt_text)
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

    def __init__(self, model_id: str = "k2-fsa/OmniVoice", device: str | None = None,
                 seed: int = 42, voice: str | None = None, **options):
        # Like VoxCPM: names the clip to clone (library filename or "preset:...").
        self.voice = voice
        self.model_id = model_id
        self.device = device
        self.seed = seed
        self._model = None

    def config_fingerprint(self) -> str:
        base = f"omnivoice:{self.model_id}:device={self.device}:seed={self.seed}"
        return f"{base}:ref={self.voice}" if self.voice else base

    def _ensure_loaded(self) -> None:
        if self._model is None:
            import torch
            from omnivoice import OmniVoice

            device = self.device or ("cuda" if torch.cuda.is_available() else "cpu")
            dtype = torch.float16 if device.startswith("cuda") else torch.float32
            self._model = OmniVoice.from_pretrained(self.model_id, device_map=device, dtype=dtype)

    def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None) -> np.ndarray:
        reference_wav_path, prompt_text = resolve_reference(self.voice, reference_wav_path, prompt_text)
        self._ensure_loaded()
        kwargs = {}
        if reference_wav_path:
            kwargs["ref_audio"] = reference_wav_path
        if prompt_text:
            kwargs["ref_text"] = prompt_text
        _seed_rng(self.seed)
        audio = self._model.generate(text=text, **kwargs)
        return np.asarray(audio[0] if isinstance(audio, (list, tuple)) else audio, dtype=np.float32)


class Confucius4Engine:
    """Confucius4 zero-shot multilingual cloning.

    The upstream API intentionally has a compact inference surface: language,
    device and the repository inference YAML.  It does not need a reference
    transcript, unlike several older cloning engines.
    """

    sample_rate = 22050

    def __init__(self, voice: str | None = None, lang: str = "vi", device: str = "auto",
                 config_path: str = "config/inference_config.yaml", **options):
        self.voice = voice
        self.lang = lang
        self.device = device
        self.config_path = config_path
        self._model = None

    def config_fingerprint(self) -> str:
        return f"confucius4:lang={self.lang}:device={self.device}:config={self.config_path}:ref={self.voice or ''}"

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        import torch
        from app.config import settings
        try:
            if settings.confucius4_repo_dir:
                import sys
                repo = Path(settings.confucius4_repo_dir)
                if not repo.is_dir():
                    raise RuntimeError(f"CONFUCIUS4_REPO_DIR không tồn tại: {repo}")
                if str(repo) not in sys.path:
                    sys.path.insert(0, str(repo))
                if self.config_path == "config/inference_config.yaml":
                    self.config_path = str(repo / self.config_path)
            from confuciustts.cli.inference import ConfuciusTTS
        except ImportError as exc:
            raise RuntimeError(
                "Chưa cài Confucius4-TTS. Cài repo netease-youdao/Confucius4-TTS "
                "và các requirements của nó, rồi chạy lại."
            ) from exc
        device = self.device
        if device == "auto":
            device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = ConfuciusTTS(config_path=self.config_path, device=device)
        self.sample_rate = int(self._model.sample_rate)

    def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None) -> np.ndarray:
        reference_wav_path, _ = resolve_reference(self.voice, reference_wav_path, prompt_text)
        if not reference_wav_path:
            raise ValueError("Confucius4-TTS cần audio mẫu để clone giọng")
        self._ensure_loaded()
        audio = self._model.generate(text, self.lang, reference_wav_path, verbose=False)
        if hasattr(audio, "detach"):
            audio = audio.detach().cpu().numpy()
        return np.asarray(audio, dtype=np.float32).reshape(-1)


class F5ViVoiceEngine:
    """hynt's Vietnamese F5-TTS fine-tune, using the installed F5 inference API."""

    sample_rate = 24000

    def __init__(self, voice: str | None = None, speed: float = 1.0, nfe_step: int = 32,
                 cfg_strength: float = 2.0, sway_sampling_coef: float = -1.0,
                 device: str = "auto", **options):
        self.voice = voice
        self.speed = speed
        self.nfe_step = nfe_step
        self.cfg_strength = cfg_strength
        self.sway_sampling_coef = sway_sampling_coef
        self.device = device
        self._model = None
        self._vocoder = None
        self._utils = None

    def config_fingerprint(self) -> str:
        return (f"f5-vivoice:speed={self.speed}:steps={self.nfe_step}:cfg={self.cfg_strength}:"
                f"sway={self.sway_sampling_coef}:device={self.device}:ref={self.voice or ''}")

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        try:
            from cached_path import cached_path
            from f5_tts.model import DiT
            from f5_tts.infer import utils_infer
        except ImportError as exc:
            raise RuntimeError(
                "Chưa cài F5-TTS Vietnamese. Cài f5-tts và cached_path trước khi dùng model này."
            ) from exc
        # This is the exact architecture/checkpoint pairing published by hynt.
        self._vocoder = utils_infer.load_vocoder()
        import inspect
        load_kwargs = {
            "ckpt_path": str(cached_path("hf://hynt/F5-TTS-Vietnamese-ViVoice/model_last.pt")),
            "vocab_file": str(cached_path("hf://hynt/F5-TTS-Vietnamese-ViVoice/config.json")),
        }
        if self.device != "auto" and "device" in inspect.signature(utils_infer.load_model).parameters:
            load_kwargs["device"] = self.device
        self._model = utils_infer.load_model(
            DiT, dict(dim=1024, depth=22, heads=16, ff_mult=2, text_dim=512, conv_layers=4), **load_kwargs
        )
        self._utils = utils_infer

    def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None) -> np.ndarray:
        reference_wav_path, prompt_text = resolve_reference(self.voice, reference_wav_path, prompt_text)
        if not reference_wav_path:
            raise ValueError("F5-TTS ViVoice cần audio mẫu để clone giọng")
        self._ensure_loaded()
        ref_audio, detected_text = self._utils.preprocess_ref_audio_text(reference_wav_path, prompt_text or "")
        import inspect
        kwargs = {
            "speed": self.speed,
            "nfe_step": self.nfe_step,
            "cfg_strength": self.cfg_strength,
            "sway_sampling_coef": self.sway_sampling_coef,
        }
        # F5 forks have changed this signature several times.  Passing only
        # supported knobs keeps the engine compatible while preserving every
        # detailed control on current releases.
        accepted = inspect.signature(self._utils.infer_process).parameters
        kwargs = {key: value for key, value in kwargs.items() if key in accepted}
        wave, sample_rate, _ = self._utils.infer_process(
            ref_audio, str(detected_text).lower(), str(text).lower(), self._model, self._vocoder, **kwargs
        )
        self.sample_rate = int(sample_rate)
        return np.asarray(wave, dtype=np.float32).reshape(-1)


class VieNeuFastEngine:
    """VieNeu-TTS v3 Turbo (48 kHz), speaking one of its 20 curated preset voices.

    v3 Turbo can clone from a clip, but this engine deliberately does not: ``infer``
    resolves ``ref_audio`` before ``voice``, so passing the book's reference clip would
    silently override the narrator the user picked. A preset also costs nothing per chunk
    and is bit-identical across patches, which cloning is not.

    ``precision`` selects the int8 vs fp32 ONNX graph and only applies on the CPU path;
    ``backend`` left as None keeps vieneu's own "auto" (ONNX on CPU, PyTorch on GPU)."""

    sample_rate = 48000
    supports_reference = False

    def __init__(
        self,
        voice: str | None = None,
        model_id: str = "pnnbao-ump/VieNeu-TTS-v3-Turbo",
        backend: str | None = None,
        precision: str = "int8",
        **options,
    ):
        self.voice = voice or _MODELS["vieneu-fast"].default_voice
        self.model_id = model_id
        self.backend = backend
        self.precision = precision
        self._model = None

    def config_fingerprint(self) -> str:
        return f"vieneu-fast:{self.model_id}:{self.voice}:{self.backend}:{self.precision}"

    def _ensure_loaded(self) -> None:
        if self._model is None:
            from vieneu import Vieneu  # heavy import, deferred until first real use

            # mode="v3turbo" is the factory default, but spelling it out pins which weights
            # load: the other modes ("standard", "fast", "turbo", ...) need vieneu[gpu].
            kwargs = {"mode": "v3turbo", "backbone_repo": self.model_id, "precision": self.precision}
            if self.backend:
                kwargs["backend"] = self.backend
            self._model = Vieneu(**kwargs)

    def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None) -> np.ndarray:
        self._ensure_loaded()
        # reference_wav_path/prompt_text are accepted and ignored, like the cloud engines:
        # the book's clip is passed to every engine uniformly, and feeding it here would
        # take precedence over self.voice. v3 Turbo also dropped `style=` -- the reading
        # style is part of each preset now.
        return np.asarray(self._model.infer(text, voice=self.voice), dtype=np.float32)


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
