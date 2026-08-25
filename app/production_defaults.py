"""Global production defaults and per-book inherit/custom resolution.

Global settings live in the single-row ``automation_settings`` table (id=1) and
group the same four blocks a book has: ``audio``, ``normalization``, ``video``
and ``youtube``.

A book inherits each group from the global defaults, or carries its own settings
(custom). The mode is stored in ``book.automation_config["inherit"]`` — a dict of
group -> bool, where True means inherit. The key is consulted first; books
written before this feature have no ``inherit`` key, and the fallback is
conservative so their behavior never changes:

* a stored per-group section (``{"video": {...}}``) marks that group custom;
* any stored section at all marks every group custom (a legacy book);
* non-default book columns (tts_*, normalize_*, video_*) mark the matching
  group custom, since legacy saves wrote only columns;
* a book with nothing stored (a fresh upload) inherits the global defaults.

Global changes therefore apply live to inherited books: every resolution reads
the ``automation_settings`` row at call time, so the next render/metadata run
picks the new values. Jobs already enqueued keep their frozen snapshots.
"""
from __future__ import annotations

import copy
import json
import sqlite3
from pathlib import Path

from app import audio_merge
from app.config import settings
from app.normalization import NormalizationOptions

GROUPS = ("audio", "normalization", "video", "youtube", "branding")

# Global audio defaults: the production default engine is settings.tts_engine, the
# same value the save route and the bulk queue treat as the canonical default.
DEFAULT_AUDIO_CONFIG = {
    "model_id": settings.tts_engine,
    "voice_id": "",
    "max_chars": settings.tts_max_chars,
    "with_effects": False,
    "tts_options": {},
    # Silence stitched between chunks when a patch is merged. The chapter value
    # is the beat between two chapters inside one patch - long enough to read as
    # a break, and the slot gap music is placed in (app/music_bed.py).
    "chunk_pause_ms": audio_merge.DEFAULT_CHUNK_PAUSE_MS,
    "chapter_pause_ms": audio_merge.DEFAULT_CHAPTER_PAUSE_MS,
}

MIN_PAUSE_MS = 0
MAX_PAUSE_MS = 30000

# The CUSTOM audio fallback at book level (a custom book with NULL tts columns)
# stays "edge-tts" — the value GET /books/{id}/audio-settings always exposed.
CUSTOM_AUDIO_FALLBACK_MODEL = "edge-tts"

DEFAULT_NORMALIZATION_CONFIG = {
    "numbers": True,
    "junk": True,
    "spellcheck": True,
    "dictionary": False,
    "transliteration": False,
}

# Branding defaults for text watermark and logo overlay on thumbnails, podcast
# covers, and generated videos.
DEFAULT_BRANDING_CONFIG = {
    "watermark": {
        "enabled": False,
        "text": "",
        "position": "bottom-right",  # top-left | top-right | bottom-left | bottom-right | center
        "font_size": 28,
        "text_color": "#FFFFFF",
        "opacity": 80,       # 0-100
        "margin": 16,
        "shadow_enabled": True,
        "shadow_color": "#000000",
    },
    "logo": {
        "enabled": False,
        "path": "",
        "position": "bottom-right",  # top-left | top-right | bottom-left | bottom-right | center
        "size": 80,          # max width/height in px
        "opacity": 80,       # 0-100
        "margin": 16,
    },
    # Per-target overrides for whether branding is applied. When a target
    # inherits branding, it inherits from global defaults; this lets a user
    # disable branding on podcast covers while keeping it on thumbnails.
    "targets": {
        "thumbnail": True,
        "podcast": True,
        "video": True,
    },
}

_BRANDING_POSITIONS = {"top-left", "top-right", "bottom-left", "bottom-right", "center"}

_TABLE = "automation_settings"
_SINGLE_ROW = "automation_settings WHERE id = 1"


def _json_object(value, default):
    try:
        parsed = json.loads(value or "{}") if isinstance(value, str) else value
    except (TypeError, ValueError, json.JSONDecodeError):
        return copy.deepcopy(default)
    return copy.deepcopy(parsed) if isinstance(parsed, dict) else copy.deepcopy(default)


def _flag(value, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "on", "yes", "y"}:
            return True
        if lowered in {"0", "false", "off", "no", "n"}:
            return False
    return default


def validate_pause_ms(value, default: int) -> int:
    """Clamp a pause setting, falling back to the default for anything unusable.

    A bad pause must never fail a save: the worst case is a silence of the wrong
    length, and refusing the whole audio config over it would block the fields
    next to it."""
    if value is None or value == "":
        return default
    try:
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        return default
    return max(MIN_PAUSE_MS, min(MAX_PAUSE_MS, parsed))


def validate_audio_config(config) -> dict:
    if not isinstance(config, dict):
        raise ValueError("audio config must be an object")
    model_id = str(config.get("model_id") or settings.tts_engine).strip()
    voice_id = str(config.get("voice_id") or "").strip()
    raw_max = config.get("max_chars", settings.tts_max_chars)
    try:
        max_chars = max(0, int(raw_max))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_chars must be a non-negative integer") from exc
    from app.tts_engine import normalize_tts_options, resolve_engine_id
    try:
        model_id = resolve_engine_id(model_id)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    return {
        "model_id": model_id,
        "voice_id": voice_id,
        "max_chars": max_chars,
        "with_effects": _flag(config.get("with_effects", False), False),
        "tts_options": normalize_tts_options(model_id, config.get("tts_options")),
        "chunk_pause_ms": validate_pause_ms(config.get("chunk_pause_ms"), audio_merge.DEFAULT_CHUNK_PAUSE_MS),
        "chapter_pause_ms": validate_pause_ms(config.get("chapter_pause_ms"), audio_merge.DEFAULT_CHAPTER_PAUSE_MS),
    }


def validate_normalization_config(config) -> dict:
    if not isinstance(config, dict):
        raise ValueError("normalization config must be an object")
    return {
        "numbers": _flag(config.get("numbers", True), True),
        "junk": _flag(config.get("junk", True), True),
        "spellcheck": _flag(config.get("spellcheck", True), True),
        "dictionary": _flag(config.get("dictionary", False), False),
        "transliteration": _flag(config.get("transliteration", False), False),
    }


def validate_video_config(config) -> dict:
    from app.video_config import validate_video_config as _validate

    return _validate(config)


def validate_youtube_config(config) -> dict:
    from app.youtube_metadata import validate_book_youtube_config as _validate

    return _validate(config)


def validate_branding_config(config) -> dict:
    """Validate and clamp the branding configuration block."""
    if not isinstance(config, dict):
        return copy.deepcopy(DEFAULT_BRANDING_CONFIG)

    def _clamped_int(value, default: int, lo: int, hi: int) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    watermark_raw = config.get("watermark") if isinstance(config.get("watermark"), dict) else {}
    watermark = {
        "enabled": _flag(watermark_raw.get("enabled", False), False),
        "text": str(watermark_raw.get("text") or "")[:200],
        "position": watermark_raw.get("position") if watermark_raw.get("position") in _BRANDING_POSITIONS else "bottom-right",
        "font_size": _clamped_int(watermark_raw.get("font_size"), 28, 12, 120),
        "text_color": str(watermark_raw.get("text_color") or "#FFFFFF"),
        "opacity": _clamped_int(watermark_raw.get("opacity"), 80, 0, 100),
        "margin": _clamped_int(watermark_raw.get("margin"), 16, 0, 200),
        "shadow_enabled": _flag(watermark_raw.get("shadow_enabled", True), True),
        "shadow_color": str(watermark_raw.get("shadow_color") or "#000000"),
    }

    logo_raw = config.get("logo") if isinstance(config.get("logo"), dict) else {}
    logo = {
        "enabled": _flag(logo_raw.get("enabled", False), False),
        "path": str(logo_raw.get("path") or "")[:500],
        "position": logo_raw.get("position") if logo_raw.get("position") in _BRANDING_POSITIONS else "bottom-right",
        "size": _clamped_int(logo_raw.get("size"), 80, 16, 500),
        "opacity": _clamped_int(logo_raw.get("opacity"), 80, 0, 100),
        "margin": _clamped_int(logo_raw.get("margin"), 16, 0, 200),
    }

    targets_raw = config.get("targets") if isinstance(config.get("targets"), dict) else {}
    targets = {
        "thumbnail": _flag(targets_raw.get("thumbnail", True), True),
        "podcast": _flag(targets_raw.get("podcast", True), True),
        "video": _flag(targets_raw.get("video", True), True),
    }

    return {"watermark": watermark, "logo": logo, "targets": targets}


def _group_validator(group: str):
    if group == "audio":
        return validate_audio_config
    if group == "normalization":
        return validate_normalization_config
    if group == "video":
        return validate_video_config
    if group == "branding":
        return validate_branding_config
    return validate_youtube_config


# ---------------------------------------------------------------------------
# Global production settings
# ---------------------------------------------------------------------------


def get_global_production_defaults(conn: sqlite3.Connection) -> dict:
    """Validated global defaults: stored values merged over the hardcoded ones."""
    row = conn.execute(f"SELECT config_json, updated_at FROM {_SINGLE_ROW}").fetchone()
    raw = _json_object(row["config_json"] if row else None, {})
    result = {}
    for group in GROUPS:
        try:
            result[group] = _group_validator(group)(raw.get(group, {}))
        except ValueError:
            # A corrupted stored block must never take the app down; fall back to
            # the hardcoded defaults for that group.
            result[group] = _group_validator(group)({})
    result["schema_version"] = 1
    result["updated_at"] = row["updated_at"] if row else None
    return result


def _merge_group_deltas(conn: sqlite3.Connection, deltas: dict) -> dict:
    """Merge the provided groups over what is already stored and return the full,
    validated config. ``deltas`` may carry any subset of the four groups."""
    row = conn.execute(f"SELECT config_json FROM {_SINGLE_ROW}").fetchone()
    raw = _json_object(row["config_json"] if row else None, {})
    merged = {}
    for group in GROUPS:
        candidate = deltas.get(group)
        merged[group] = _group_validator(group)(
            candidate if candidate is not None else raw.get(group, {})
        )
    return merged


def save_global_production_defaults(conn: sqlite3.Connection, deltas: dict) -> dict:
    """Persist global defaults (partial update: only the groups present in
    ``deltas`` are written, the rest keep their stored values) and return the
    full validated config. Only groups are accepted; anything else is ignored."""
    from datetime import datetime, timezone

    clean = {}
    for group in GROUPS:
        if group in deltas:
            clean[group] = _group_validator(group)(deltas[group])
    if not clean:
        raise ValueError("no valid production setting group provided")
    stored = _merge_group_deltas(conn, clean)
    now = datetime.now(timezone.utc).isoformat()
    payload = {group: stored[group] for group in GROUPS}
    conn.execute(
        f"""INSERT INTO {_TABLE}
               (id, schema_version, config_json, created_at, updated_at)
            VALUES (1, 1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
               schema_version=excluded.schema_version,
               config_json=excluded.config_json,
               updated_at=excluded.updated_at""",
        (json.dumps(payload), now, now),
    )
    conn.commit()
    stored["schema_version"] = 1
    stored["updated_at"] = now
    return stored


# ---------------------------------------------------------------------------
# Per-book mode
# ---------------------------------------------------------------------------


def parse_book_config(book) -> dict:
    """The book's automation_config as a dict, whatever its stored shape."""
    if isinstance(book, dict):
        value = book.get("automation_config")
    else:
        value = getattr(book, "automation_config", None)
    return _json_object(value, {})


def _has_stored_section(config: dict) -> bool:
    return any(
        key != "inherit" and isinstance(config[key], (dict, list)) and config[key]
        for key in config
    )


def _columns_suggest_custom(book, group: str) -> bool:
    """Legacy books customized only through columns (tts_*, normalize_*,
    video_*) must stay custom. Fresh books keep the column defaults, so this
    never fires for them."""
    if group == "audio":
        return bool(
            getattr(book, "tts_model", None)
            or getattr(book, "tts_voice_id", None)
            or getattr(book, "tts_max_chars", None)
            or getattr(book, "tts_with_effects", 0)
        )
    if group == "normalization":
        return not (
            bool(getattr(book, "normalize_numbers_enabled", 1))
            and bool(getattr(book, "normalize_junk_enabled", 1))
            and bool(getattr(book, "normalize_spellcheck_enabled", 1))
            and not bool(getattr(book, "normalize_dictionary_enabled", 0))
            and not bool(getattr(book, "normalize_transliteration_enabled", 0))
        )
    if group == "video":
        return not (
            (getattr(book, "video_resolution", "1920x1080") or "1920x1080") == "1920x1080"
            and int(getattr(book, "video_fps", 30) or 30) == 30
            and (getattr(book, "default_image_animation", "none") or "none") == "none"
        )
    return False


def get_group_mode(config: dict | None, group: str, book=None) -> str:
    """'inherit' or 'custom' for one group of one book.

    ``config`` is the parsed book automation_config; ``book`` (optional) adds
    the column-based legacy heuristic. The ``inherit`` key is authoritative:
    groups not listed inside it are treated as inherit. Without an explicit
    ``inherit`` key the legacy heuristics decide, and a book with nothing
    stored inherits.
    """
    config = config or {}
    inherit = config.get("inherit")
    if isinstance(inherit, dict):
        if group in inherit:
            return "inherit" if inherit[group] else "custom"
        return "inherit"
    if isinstance(inherit, bool):
        return "inherit" if inherit else "custom"
    if isinstance(config.get(group), (dict, list)) and config[group]:
        return "custom"
    if _has_stored_section(config):
        return "custom"
    if book is not None and _columns_suggest_custom(book, group):
        return "custom"
    return "inherit"


def get_group_modes(config: dict | None, book=None) -> dict:
    return {group: get_group_mode(config, group, book=book) for group in GROUPS}


def set_group_mode(raw: dict, group: str, mode: str) -> dict:
    """Record the mode inside automation_config under the ``inherit`` key."""
    inherit = raw.setdefault("inherit", {})
    if not isinstance(inherit, dict):
        inherit = raw["inherit"] = {}
    inherit[group] = mode == "inherit"
    return raw


def save_book_branding_config(conn: sqlite3.Connection, book_id: int, branding: dict) -> dict:
    """Persist branding into automation_config['branding'] and set mode to custom.

    Returns the validated branding config that was stored.  Runs inside the
    caller's transaction so the branding write and the mode flag are atomic.
    """
    validated = validate_branding_config(branding)
    row = conn.execute("SELECT automation_config FROM book WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        raise ValueError("book not found")
    raw = _json_object(row["automation_config"], {})
    raw["branding"] = validated
    set_group_mode(raw, "branding", "custom")
    conn.execute(
        "UPDATE book SET automation_config = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (json.dumps(raw), book_id),
    )
    return validated


def save_book_audio_section(conn: sqlite3.Connection, book_id: int, **values) -> dict:
    """Merge ``values`` into the book's automation_config["audio"] section.

    Only the audio settings that have no book column of their own live here (the
    merge pauses). Returns the stored section. The caller commits - this runs
    inside the same transaction as the column update it accompanies."""
    row = conn.execute("SELECT automation_config FROM book WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        return {}
    raw = _json_object(row["automation_config"], {})
    section = raw.get("audio")
    section = dict(section) if isinstance(section, dict) else {}
    section.update({key: value for key, value in values.items() if value is not None})
    raw["audio"] = section
    conn.execute(
        "UPDATE book SET automation_config = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (json.dumps(raw), book_id),
    )
    return section


def set_book_group_mode_db(conn: sqlite3.Connection, book_id: int, group: str, mode: str) -> None:
    """Persist one group's mode on a book (used by legacy save routes so a save
    always means 'this book customizes the group')."""
    if group not in GROUPS or mode not in ("inherit", "custom"):
        raise ValueError("invalid group or mode")
    row = conn.execute("SELECT automation_config FROM book WHERE id = ?", (book_id,)).fetchone()
    if row is None:
        return
    raw = _json_object(row["automation_config"], {})
    set_group_mode(raw, group, mode)
    conn.execute(
        "UPDATE book SET automation_config = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
        (json.dumps(raw), book_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Effective configs
# ---------------------------------------------------------------------------


def _global_group(conn: sqlite3.Connection, group: str) -> dict:
    return get_global_production_defaults(conn)[group]


def get_effective_video_config(conn: sqlite3.Connection, book) -> dict:
    """hardcoded defaults -> global defaults -> book custom.

    Inherited books render with the global video block (including resolution /
    fps / animation), so global changes apply live. Custom books keep their
    stored section and columns."""
    config = parse_book_config(book)
    if get_group_mode(config, "video", book=book) == "inherit":
        return _global_group(conn, "video")
    from app.video_config import get_book_video_config

    return get_book_video_config(conn, book)


def get_effective_youtube_config(conn: sqlite3.Connection, book) -> dict:
    config = parse_book_config(book)
    if get_group_mode(config, "youtube", book=book) == "inherit":
        return _global_group(conn, "youtube")
    from app.youtube_metadata import get_book_youtube_config

    return get_book_youtube_config(conn, getattr(book, "id", None))


def get_effective_audio_config(conn: sqlite3.Connection, book) -> dict:
    config = parse_book_config(book)
    if get_group_mode(config, "audio", book=book) == "inherit":
        return _global_group(conn, "audio")
    # The four legacy fields live in book columns; pauses and model-specific
    # controls arrived later in automation_config["audio"] to avoid a migration.
    # A book saved before they existed falls back to the defaults.
    stored = config.get("audio") if isinstance(config.get("audio"), dict) else {}
    model_id = getattr(book, "tts_model", None) or CUSTOM_AUDIO_FALLBACK_MODEL
    from app.tts_engine import normalize_tts_options
    return {
        "model_id": model_id,
        "voice_id": getattr(book, "tts_voice_id", None) or "",
        "max_chars": getattr(book, "tts_max_chars", None) or settings.tts_max_chars,
        "with_effects": bool(getattr(book, "tts_with_effects", 0)),
        "tts_options": normalize_tts_options(model_id, stored.get("tts_options")),
        "chunk_pause_ms": validate_pause_ms(stored.get("chunk_pause_ms"), audio_merge.DEFAULT_CHUNK_PAUSE_MS),
        "chapter_pause_ms": validate_pause_ms(stored.get("chapter_pause_ms"), audio_merge.DEFAULT_CHAPTER_PAUSE_MS),
    }


def get_effective_normalization_config(conn: sqlite3.Connection, book) -> dict:
    config = parse_book_config(book)
    if get_group_mode(config, "normalization", book=book) == "inherit":
        return _global_group(conn, "normalization")
    return {
        "numbers": bool(getattr(book, "normalize_numbers_enabled", 1)),
        "junk": bool(getattr(book, "normalize_junk_enabled", 1)),
        "spellcheck": bool(getattr(book, "normalize_spellcheck_enabled", 1)),
        "dictionary": bool(getattr(book, "normalize_dictionary_enabled", 0)),
        "transliteration": bool(getattr(book, "normalize_transliteration_enabled", 0)),
    }


def get_effective_normalization_options(conn: sqlite3.Connection, book) -> NormalizationOptions:
    return NormalizationOptions(**get_effective_normalization_config(conn, book))


def get_effective_branding_config(conn: sqlite3.Connection, book) -> dict:
    """Resolved branding config: global defaults -> per-book override.

    Branding lives entirely in automation_config JSON (no book columns), so
    resolution is straightforward: check the inherit flag, return global or
    stored block."""
    config = parse_book_config(book)
    if get_group_mode(config, "branding", book=book) == "inherit":
        return _global_group(conn, "branding")
    stored = config.get("branding") if isinstance(config.get("branding"), dict) else {}
    return validate_branding_config(stored)


def resolve_voice_clip(video_config: dict | None, key: str) -> str | None:
    """Đường dẫn clip intro/outro đang cấu hình, None nếu chưa chọn hoặc thiếu file."""
    name = (video_config or {}).get(key)
    if not name:
        return None
    path = Path(settings.data_root) / "voices" / str(name)
    return str(path) if path.is_file() else None


def resolve_effective_youtube_metadata(conn: sqlite3.Connection, book, patch, override,
                                       context: dict | None = None) -> dict:
    """resolve_patch_youtube_metadata with the effective youtube config.

    The pure resolver has no connection; this helper is the conn-coupled entry
    point so every call site with a connection (preflight, snapshots, previews,
    uploads) resolves against the effective config.

    Nó cũng đo intro của video config: video phát intro trước nội dung patch, nên
    timeline chương trong description phải dời theo đúng độ dài đó."""
    from app.youtube_metadata import audio_duration_seconds, resolve_patch_youtube_metadata

    intro = resolve_voice_clip(get_effective_video_config(conn, book), "intro_voice")
    context = {**(context if isinstance(context, dict) else {}),
               "intro_seconds": audio_duration_seconds(intro) if intro else 0.0}
    return resolve_patch_youtube_metadata(
        book, patch, override, context,
        config=get_effective_youtube_config(conn, book),
    )
