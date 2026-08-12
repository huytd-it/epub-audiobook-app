"""Validation and persistence helpers for shared book video configuration."""
from __future__ import annotations

import copy
import json
from pathlib import Path

from app import repository

VIDEO_DEFAULTS = {
    "backgrounds": [],
    "background_mode": "sequential",
    "image_duration_seconds": 15,
    "intro_voice": "",
    "outro_voice": "",
    "codec": "libx264",
    "audio_bitrate": "320k",
    "quality": 23,
    "concurrency": 3,
    "crossfade_enabled": False,
    "crossfade_seconds": 1,
    "ken_burns_enabled": False,
    "progress_bar_enabled": False,
    "waveform_enabled": False,
    "waveform_style": "line",
    "waveform_color": "#ffffff",
    "waveform_position": "bottom",
    "waveform_height": 120,
    "waveform_opacity": 0.85,
    "waveform_layout": "horizontal",
    "waveform_background_color": "#050816",
    "waveform_background_opacity": 0.55,
    "resolution": "1920x1080",
    "fps": 30,
    "image_animation": "none",
    "fit_mode": "auto",
}

_RESOLUTIONS = {"1920x1080", "1280x720", "854x480", "1080x1920", "1080x1080"}
VALID_FPS = {24, 30, 60}
_CODECS = {"libx264", "h264_nvenc"}
_BITRATES = {"128k", "192k", "256k", "320k"}
_ANIMATIONS = {"none", "static", "zoom-in", "zoom-out", "pan-left", "pan-right"}
# How a background that does not match the frame aspect is fitted. "auto" picks
# "blur" for portrait/square frames and "contain" for landscape ones, so a book
# switched to 9:16 stops rendering two thirds black without any extra setting.
_FIT_MODES = {"auto", "contain", "cover", "blur"}
_WAVEFORM_STYLES = {"line", "cline", "p2p", "point"}


def _json_object(value) -> dict:
    if isinstance(value, dict):
        return copy.deepcopy(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return copy.deepcopy(parsed) if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def validate_video_config(config: dict | None) -> dict:
    config = _json_object(config)
    result = {**copy.deepcopy(VIDEO_DEFAULTS), **config}
    if not isinstance(result["backgrounds"], list) or not all(isinstance(p, str) and p.strip() for p in result["backgrounds"]):
        raise ValueError("backgrounds must be a list of paths")
    result["backgrounds"] = list(dict.fromkeys(p.strip() for p in result["backgrounds"]))
    if result["background_mode"] not in {"sequential", "random"}:
        raise ValueError("invalid background mode")
    if not isinstance(result["image_duration_seconds"], (int, float)) or not 1 <= result["image_duration_seconds"] <= 600:
        raise ValueError("image duration must be 1-600 seconds")
    if result["codec"] not in _CODECS:
        raise ValueError("invalid codec")
    if result["resolution"] not in _RESOLUTIONS:
        raise ValueError("invalid resolution")
    if result["fps"] not in VALID_FPS:
        raise ValueError("invalid fps")
    if result["image_animation"] not in _ANIMATIONS:
        raise ValueError("invalid animation")
    if result["fit_mode"] not in _FIT_MODES:
        raise ValueError("invalid fit mode")
    if result["audio_bitrate"] not in _BITRATES:
        raise ValueError("invalid audio bitrate")
    if not isinstance(result["quality"], int) or not 18 <= result["quality"] <= 28:
        raise ValueError("quality must be 18-28")
    if not isinstance(result["concurrency"], int) or result["concurrency"] not in {1, 2, 3, 4, 6, 8}:
        raise ValueError("invalid concurrency")
    if not isinstance(result["crossfade_enabled"], bool) or not isinstance(result["ken_burns_enabled"], bool) or not isinstance(result["progress_bar_enabled"], bool) or not isinstance(result["waveform_enabled"], bool):
        raise ValueError("enhancement flags must be boolean")
    if not isinstance(result["crossfade_seconds"], (int, float)) or not 0 <= result["crossfade_seconds"] <= 3:
        raise ValueError("crossfade must be 0-3 seconds")
    for field in ("intro_voice", "outro_voice"):
        if not isinstance(result[field], str):
            raise ValueError(f"{field} must be a string")
    if result["waveform_style"] not in _WAVEFORM_STYLES:
        raise ValueError("invalid waveform style")
    if result["waveform_position"] not in {"top", "center", "bottom"}:
        raise ValueError("invalid waveform position")
    if result["waveform_layout"] not in {"horizontal", "vertical", "circular"}:
        raise ValueError("invalid waveform layout")
    for field in ("waveform_color", "waveform_background_color"):
        color = result[field]
        if not isinstance(color, str) or len(color) != 7 or not color.startswith("#"):
            raise ValueError(f"invalid {field}")
        try:
            int(color[1:], 16)
        except ValueError as exc:
            raise ValueError(f"invalid {field}") from exc
    if not isinstance(result["waveform_height"], int) or not 40 <= result["waveform_height"] <= 400:
        raise ValueError("waveform height must be 40-400")
    if not isinstance(result["waveform_opacity"], (int, float)) or not 0.1 <= result["waveform_opacity"] <= 1:
        raise ValueError("waveform opacity must be 0.1-1")
    if not isinstance(result["waveform_background_opacity"], (int, float)) or not 0 <= result["waveform_background_opacity"] <= 1:
        raise ValueError("waveform background opacity must be 0-1")
    return result


def get_book_video_config(conn, book) -> dict:
    raw = _json_object(book.automation_config)
    video = validate_video_config(raw.get("video", {}))
    # Fill from book table columns as they are the source of truth
    video["resolution"] = book.video_resolution if book.video_resolution in _RESOLUTIONS else "1920x1080"
    video["fps"] = book.video_fps if book.video_fps in VALID_FPS else 30
    video["image_animation"] = book.default_image_animation if book.default_image_animation in _ANIMATIONS else "none"
    return video


def save_book_video_config(conn, book_id: int, config: dict) -> dict:
    video = validate_video_config(config)
    row = conn.execute("SELECT video_resolution, video_fps, default_image_animation FROM book WHERE id = ?", (book_id,)).fetchone()
    video["resolution"] = config.get("resolution") or (row[0] if row else "1920x1080")
    video["fps"] = config.get("fps") or (row[1] if row else 30)
    video["image_animation"] = config.get("image_animation") or (row[2] if row else "none")
    row = conn.execute("SELECT automation_config FROM book WHERE id = ?", (book_id,)).fetchone()
    raw = _json_object(row[0] if row else None)
    stored = {key: value for key, value in video.items() if key not in {"resolution", "fps", "image_animation"}}
    raw["video"] = stored
    conn.execute(
        """UPDATE book SET automation_config = ?, video_resolution = ?, video_fps = ?,
           default_image_animation = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?""",
        (json.dumps(raw), video["resolution"], video["fps"], video["image_animation"], book_id),
    )
    conn.commit()
    return video


def validate_media_path(path: str, allowed_dir: str | Path) -> str:
    candidate = Path(path).resolve()
    root = Path(allowed_dir).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("media path is outside the allowed library")
    if not candidate.is_file():
        raise ValueError("media file not found")
    return str(candidate)
