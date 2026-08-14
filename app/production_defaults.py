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

from app.config import settings
from app.normalization import NormalizationOptions

GROUPS = ("audio", "normalization", "video", "youtube")

# Global audio defaults: the production default engine is settings.tts_engine, the
# same value the save route and the bulk queue treat as the canonical default.
DEFAULT_AUDIO_CONFIG = {
    "model_id": settings.tts_engine,
    "voice_id": "",
    "max_chars": settings.tts_max_chars,
    "with_effects": False,
}

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
    return {
        "model_id": model_id,
        "voice_id": voice_id,
        "max_chars": max_chars,
        "with_effects": _flag(config.get("with_effects", False), False),
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


def _group_validator(group: str):
    if group == "audio":
        return validate_audio_config
    if group == "normalization":
        return validate_normalization_config
    if group == "video":
        return validate_video_config
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
    return {
        "model_id": getattr(book, "tts_model", None) or CUSTOM_AUDIO_FALLBACK_MODEL,
        "voice_id": getattr(book, "tts_voice_id", None) or "",
        "max_chars": getattr(book, "tts_max_chars", None) or settings.tts_max_chars,
        "with_effects": bool(getattr(book, "tts_with_effects", 0)),
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


def resolve_effective_youtube_metadata(conn: sqlite3.Connection, book, patch, override,
                                       context: dict | None = None) -> dict:
    """resolve_patch_youtube_metadata with the effective youtube config.

    The pure resolver has no connection; this helper is the conn-coupled entry
    point so every call site with a connection (preflight, snapshots, previews,
    uploads) resolves against the effective config."""
    from app.youtube_metadata import resolve_patch_youtube_metadata

    return resolve_patch_youtube_metadata(
        book, patch, override, context,
        config=get_effective_youtube_config(conn, book),
    )
