"""Resumable, idempotent publishing stages for one patch."""
from __future__ import annotations

import json
import logging
import sqlite3
import soundfile as sf
from datetime import datetime, timezone
from pathlib import Path

from app import image_overlay, repository, video_gen, youtube
from app.config import settings
from app.image_overlay import ensure_patch_overlay
from app.jobqueue import store
from app.repository import build_patch_metadata_context, get_book, get_patch
from app.video_repository import upsert_patch_video, delete_video
from app.video_integrity import validate_video
from app.video_publish import publish_validated_video
from app.youtube_metadata import (get_book_youtube_config, get_patch_youtube_override,
                                  resolve_patch_chapter_range, resolve_patch_youtube_metadata,
                                  validate_book_youtube_config, validate_timeline)
from app.video_config import get_book_video_config
from app.production_defaults import (get_effective_branding_config,
                                     get_effective_video_config, get_effective_youtube_config,
                                     resolve_effective_youtube_metadata, resolve_voice_clip)

STAGES = ("thumbnail", "video", "upload", "thumbnail_setting", "playlist", "published")

# Tổng số lần render/rerender một patch video trước khi pipeline bị khoá chạy tiếp.
MAX_PATCH_RENDER_ATTEMPTS = 3
# phiên bản cấu trúc snapshot cấp vào job patch_video lúc enqueue.
SNAPSHOT_SCHEMA_VERSION = 2
GAMEPLAY_SNAPSHOT_SCHEMA_VERSION = 3

AUTOMATION_PREFLIGHT_STATES = (
    "no_automation", "waiting_config", "waiting_timeline",
    "awaiting_republish_confirmation", "ready", "failed",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row(conn, patch_id):
    row = conn.execute("SELECT * FROM patch_pipeline WHERE patch_id=?", (patch_id,)).fetchone()
    return dict(row) if row else None


def audio_fingerprint(patch) -> str:
    """Định danh nội dung file audio (path:size:hash đầu+cuối) — dùng để nhận diện
    audio mới so với lần publish trước (republish confirmation) và so với snapshot
    lúc enqueue. Nội dung thay đổi mới tính là audio mới; tải lại cùng file không
    kích hoạt xác nhận publish lại."""
    import hashlib

    path = patch.audio_path or ""
    if not path or not Path(path).is_file():
        return f"{path}:missing"
    try:
        size = Path(path).stat().st_size
        head_tail_size = 512 * 1024
        with open(path, "rb") as fh:
            head = fh.read(head_tail_size)
            fh.seek(max(0, size - head_tail_size))
            digest = hashlib.sha1(head + fh.read(head_tail_size) + str(size).encode()).hexdigest()
        return f"{path}:{size}:{digest}"
    except OSError:
        return f"{path}:missing"


def resolve_automation_policy(book, *, request_policy: dict | None = None) -> dict:
    """Hiệu lực hoá cờ tự động hoá cho một patch.

    Cờ đã lưu của sách (cột book.auto_create_video / book.auto_upload_youtube) là
    mặc định; request_policy (payload TTS) có auto_* ưu tiên khi được truyền rõ.
    Upload bao hàm tạo video."""
    create = bool(book.auto_create_video)
    upload = bool(book.auto_upload_youtube)
    if request_policy:
        if request_policy.get("auto_create_video") is not None:
            create = bool(request_policy["auto_create_video"])
        if request_policy.get("auto_upload_youtube") is not None:
            upload = bool(request_policy["auto_upload_youtube"])
    if upload:
        create = True
    return {"auto_create_video": create, "auto_upload_youtube": upload}


def _timeline_status(patch) -> str:
    """Kiểm tra sidecar timeline của audio: 'missing' | 'valid' | 'invalid'."""
    audio = Path(patch.audio_path or "")
    sidecar = audio.with_suffix(".timeline.json")
    if not sidecar.is_file():
        return "missing"
    try:
        timeline = json.loads(sidecar.read_text(encoding="utf-8"))
        info = sf.info(str(audio))
        checked = validate_timeline(timeline, info.samplerate, info.frames)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, sf.SoundFileError):
        return "invalid"
    return "valid" if checked is not None else "invalid"


def _youtube_privacy(conn, book_id: int) -> str | None:
    book = get_book(conn, book_id)
    try:
        return validate_book_youtube_config(get_effective_youtube_config(conn, book))["privacy_status"]
    except (ValueError, AttributeError):
        return None


def evaluate_patch_preflight(conn: sqlite3.Connection, patch_id: int, *,
                             request_policy: dict | None = None) -> dict:
    """Đánh giá một patch có được tự động render/publish hay không.

    Trả về dict state/code/error kèm policy đã hiệu lực hoá; không ghi DB (preflight_patch
    là phiên bản có ghi trạng thái để book_status/màn hình hiển thị)."""
    patch = get_patch(conn, patch_id)
    if patch is None:
        return {"state": "failed", "code": "patch_not_found", "error": f"patch {patch_id} not found",
                "policy": None}
    book = get_book(conn, patch.book_id)
    if book is None:
        return {"state": "failed", "code": "book_not_found", "error": f"book {patch.book_id} not found",
                "policy": None}
    policy = resolve_automation_policy(book, request_policy=request_policy)
    if not policy["auto_create_video"] and not policy["auto_upload_youtube"]:
        return {"state": "no_automation", "code": None, "error": None, "policy": policy}
    pipeline = _row(conn, patch_id) or {}
    published_before = bool(pipeline.get("youtube_upload_id")) or pipeline.get("stage") == "published"
    if published_before:
        if pipeline.get("republish_confirmed_for") != audio_fingerprint(patch):
            return {"state": "awaiting_republish_confirmation",
                    "code": "republish_confirmation_required",
                    "error": "Audio đã thay đổi sau khi publish; cần xác nhận publish lại.",
                    "policy": policy}
    try:
        video_config = get_effective_video_config(conn, book)
    except ValueError as exc:
        return {"state": "waiting_config", "code": "video_config_invalid",
                "error": f"cấu hình video không hợp lệ: {exc}", "policy": policy}
    branding = get_effective_branding_config(conn, book)
    resolved = {"policy": policy, "video_config": video_config, "branding": branding}
    if policy["auto_upload_youtube"]:
        try:
            youtube_config = validate_book_youtube_config(get_effective_youtube_config(conn, book))
        except ValueError as exc:
            return {"state": "waiting_config", "code": "youtube_config_invalid",
                    "error": f"cấu hình YouTube không hợp lệ: {exc}", "policy": policy}
        if not youtube.is_configured() or youtube.get_creds_from_db(conn) is None:
            return {"state": "waiting_config", "code": "youtube_not_connected",
                    "error": "YouTube chưa được cấu hình hoặc kết nối", "policy": policy}
        playlist = youtube_config["playlist"]
        if playlist["mode"] != "existing":
            return {"state": "waiting_config", "code": "youtube_playlist_required",
                    "error": "Auto-upload cần một playlist hợp lệ", "policy": policy}
        if not playlist["playlist_id"]:
            return {"state": "waiting_config", "code": "youtube_playlist_missing",
                    "error": "Playlist chưa được chọn", "policy": policy}
        timeline = _timeline_status(patch)
        if timeline != "valid":
            return {"state": "waiting_timeline",
                    "code": "timeline_invalid" if timeline == "invalid" else "timeline_missing",
                    "error": ("Timeline không hợp lệ" if timeline == "invalid"
                              else "Chưa có timeline để publish phần chương"),
                    "policy": policy}
        resolved["youtube_config"] = youtube_config
        resolved["privacy_status"] = youtube_config["privacy_status"]
    return {"state": "ready", "code": None, "error": None, **resolved}


def preflight_patch(conn: sqlite3.Connection, patch_id: int, *,
                    request_policy: dict | None = None) -> dict:
    """evaluate_patch_preflight + ghi trạng thái vào patch_pipeline (upsert)."""
    result = evaluate_patch_preflight(conn, patch_id, request_policy=request_policy)
    policy = result.get("policy") or {}
    now = _now()
    conn.execute(
        """INSERT INTO patch_pipeline
               (patch_id, stage, config_snapshot, media_snapshot, preflight_status,
                preflight_error_code, preflight_error, checked_at, policy_snapshot,
                created_at, updated_at)
           VALUES (?, 'pending', '{}', '{}', ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(patch_id) DO UPDATE SET
               preflight_status=excluded.preflight_status,
               preflight_error_code=excluded.preflight_error_code,
               preflight_error=excluded.preflight_error,
               checked_at=excluded.checked_at,
               policy_snapshot=excluded.policy_snapshot,
               updated_at=excluded.updated_at""",
        (patch_id, result["state"], result["code"],
         (result.get("error") or "")[:2000], now,
         json.dumps({**policy, "snapshot_schema_version": SNAPSHOT_SCHEMA_VERSION}, ensure_ascii=False),
         now, now),
    )
    conn.commit()
    return result


def _resolve_sequence_inputs(book, patch, config: dict, branding: dict | None = None):
    """Chia chế độ render khi enqueue: chuỗi nhiều ảnh nền hay ảnh đơn.

    Trả về (sequence, backgrounds, image, image_type) — các giá trị này được
    đóng băng vào job payload để render không phụ thuộc config hiện tại.

    ``branding`` is the resolved branding config, applied to the thumbnail image
    when a single non-video background is used."""
    if config.get("background_type") in {"battle_royale", "gameplay"}:
        return False, [], None, None, "none"
    fallback = video_gen.resolve_patch_image(patch, book, settings.default_background_image)
    raw_bg = video_gen.resolve_configured_patch_image(patch, config, fallback or "")
    backgrounds = [p for p in config.get("backgrounds", []) if isinstance(p, str)]
    sequence = len(backgrounds) > 1 and not (patch.image_path and Path(patch.image_path).exists())
    image = raw_bg
    if not sequence and raw_bg and not video_gen.is_video_background(raw_bg):
        image = ensure_patch_overlay(book, patch, settings.default_font_path or None,
                                     background_path=raw_bg, branding=branding) or raw_bg
    image_type = "none" if (sequence or (raw_bg and video_gen.is_video_background(raw_bg))) else (
        patch.image_type if patch.image_type and patch.image_type != "static"
        else (book.default_image_animation or "none"))
    return sequence, backgrounds, raw_bg, image, image_type


def build_enqueue_snapshot(conn: sqlite3.Connection, book, patch, resolved: dict,
                           pipeline: dict) -> dict:
    """Đóng băng toàn bộ dữ liệu render cần thiết vào một dict JSON-safe.

    render_config/audio lấy từ media_snapshot vừa được enqueue_patch_publish ghi
    (bất biến từ lúc enqueue); phần chọn chuỗi ảnh nền lấy theo config lúc enqueue.

    Branding config is frozen into the snapshot so patch video renders use the
    branding settings that were active at enqueue time, even if the user later
    changes them."""
    config = resolved["video_config"]
    branding = resolved.get("branding") or {}
    try:
        media = json.loads(pipeline.get("media_snapshot") or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        media = {}
    render_config = media.get("render_config")
    if not isinstance(render_config, dict):
        render_config = {"resolution": book.video_resolution or "1920x1080",
                         "fps": book.video_fps or 30, "fit_mode": "auto", "codec": "libx264",
                         "crf": 23, "audio_bitrate": "192k"}
    sequence, backgrounds, raw_bg, image, image_type = _resolve_sequence_inputs(
        book, patch, config, branding=branding)
    background_type = config.get("background_type", "media")
    if background_type == "media" and not raw_bg:
        raise ValueError("chưa có ảnh nền để tạo video")
    gameplay = None
    if background_type in {"battle_royale", "gameplay"}:
        from app.gameplay_pool import ensure_patch_coverage
        from app.gameplay_registry import get_game, resolve_game_id
        info = sf.info(str(media.get("audio_path") or patch.audio_path))
        duration = info.frames / info.samplerate
        width, height = (int(v) for v in str(render_config.get("resolution") or "1920x1080").split("x"))
        gameplay_config = ({"selection_mode": "single", "game_id": "battle_royale", "preset": "calm"}
                           if background_type == "battle_royale" else config.get("gameplay", {}))
        game_id = resolve_game_id(gameplay_config, book_id=book.id, patch_id=patch.id,
                                  patch_index=patch.patch_index)
        game = get_game(game_id)
        if config.get("waveform_enabled") and game.waveform_policy == "forbidden":
            raise ValueError(f"waveform không tương thích với game {game_id}")
        clips = ensure_patch_coverage(conn, patch.id, duration, width=width, height=height,
                                      fps=int(render_config.get("fps") or 30), game_id=game_id,
                                      quality=int(render_config.get("crf") or 23), config=gameplay_config)
        gameplay = {
            "game_id": game_id,
            "selection_mode": gameplay_config.get("selection_mode", "single"),
            "profile_key": clips[0]["profile_key"],
            "renderer_version": game.renderer_version,
            "waveform_policy": game.waveform_policy,
            "reservation_token": clips[0].get("reservation_token"),
            "clips": [{"id": c["id"], "replay_id": c["replay_id"], "game_id": c.get("game_id") or "battle_royale",
                       "profile_key": c["profile_key"], "file_path": c["file_path"],
                       "duration_seconds": c["duration_seconds"]} for c in clips],
        }
    snapshot_schema_version = GAMEPLAY_SNAPSHOT_SCHEMA_VERSION if background_type == "gameplay" else SNAPSHOT_SCHEMA_VERSION
    snapshot = {
        "schema_version": snapshot_schema_version,
        "background_type": background_type,
        "gameplay": gameplay,
        "audio_path": media.get("audio_path") or patch.audio_path,
        "audio_fingerprint": audio_fingerprint(patch),
        "thumbnail_path": pipeline.get("thumbnail_path"),
        "render_config": render_config,
        "sequence": sequence,
        "backgrounds": backgrounds,
        "image": image,
        "image_type": image_type,
        "branding": branding,
        "sequence_config": {key: config.get(key) for key in (
            "image_duration_seconds", "background_mode", "crossfade_enabled",
            "crossfade_seconds", "ken_burns_enabled", "progress_bar_enabled",
            "waveform_enabled", "waveform_style", "waveform_color", "waveform_position",
            "waveform_height", "waveform_opacity", "waveform_layout",
            "waveform_background_color", "waveform_background_opacity",
            "subtitle_enabled", "subtitle_font_size", "subtitle_color", "subtitle_position",
        )},
    }
    media["render_snapshot"] = snapshot
    conn.execute(
        "UPDATE patch_pipeline SET media_snapshot=?, snapshot_schema_version=?, updated_at=? WHERE patch_id=?",
        (json.dumps(media), snapshot_schema_version, _now(), patch.id),
    )
    conn.commit()
    return snapshot


def discard_stale_patch_video(conn: sqlite3.Connection, book_id: int, patch_id: int) -> None:
    """Xoá render cũ (row videos + file) và đưa pipeline về trạng thái chờ render mới.

    Dùng khi audio của patch vừa thay đổi và cần dựng lại video từ đầu —
    render_attempts về 0 cho phép một chu kỳ render mới."""
    video_path = Path(settings.data_root) / "books" / str(book_id) / "patch_videos" / f"{patch_id}.mp4"
    row = conn.execute("SELECT id FROM videos WHERE file_path=?", (str(video_path),)).fetchone()
    if row:
        delete_video(conn, row["id"])
    else:
        video_path.unlink(missing_ok=True)
    conn.execute(
        """UPDATE patch_pipeline SET stage='video', video_status='pending', video_id=NULL,
                  video_path=NULL, youtube_upload_id=NULL, upload_status='pending',
                  playlist_status='pending', render_attempts=0, republish_confirmed_for=NULL,
                  validation_report_json=NULL, last_error=NULL, updated_at=?
            WHERE patch_id=?""", (_now(), patch_id),
    )
    conn.commit()


def fetch_thumbnail_inputs(conn: sqlite3.Connection, patch_id: int):
    """Read what warm_patch_thumbnail needs. Cheap: two row lookups and a config read."""
    patch = get_patch(conn, patch_id)
    book = get_book(conn, patch.book_id) if patch else None
    if not patch or not book:
        return None
    policy = resolve_automation_policy(book)
    if not policy["auto_create_video"] and not policy["auto_upload_youtube"]:
        return None
    return book, patch


def warm_patch_thumbnail(inputs) -> None:
    """Render the patch thumbnail ahead of time, outside the shared db_lock.

    enqueue_patch_publish renders it too, but ensure_patch_overlay is cached: it only
    redraws when the file is missing or the background is newer. Doing it here first turns
    the call under the lock into a stat check instead of a full PIL render, which would
    otherwise stall every request once per finished patch.
    """
    if inputs is None:
        return
    book, patch = inputs
    try:
        from app.db import connect
        from app.config import settings as cfg
        conn = connect(cfg.db_path)
        try:
            branding = get_effective_branding_config(conn, book)
        finally:
            conn.close()
        ensure_patch_overlay(book, patch, settings.default_font_path or None, branding=branding)
    except Exception:  # noqa: BLE001 - purely an optimization; the real render still runs
        logging.getLogger(__name__).warning(
            "thumbnail pre-render failed for patch %s; falling back to rendering under the lock",
            patch.id, exc_info=True,
        )


def on_patch_audio_ready(conn: sqlite3.Connection, patch_id: int) -> dict | None:
    patch = get_patch(conn, patch_id)
    if not patch:
        return None
    book = get_book(conn, patch.book_id)
    if book and get_effective_youtube_config(conn, book).get("auto_upload"):
        return enqueue_patch_publish(conn, patch_id)
    return None


def _build_metadata_snapshot(conn: sqlite3.Connection, book, patch) -> dict:
    metadata = resolve_effective_youtube_metadata(
        conn, book, patch, get_patch_youtube_override(conn, patch.id),
        build_patch_metadata_context(conn, book, patch))
    metadata["automation"] = {"youtube": metadata.pop("youtube")}
    chapter_start, chapter_end, patch_name = resolve_patch_chapter_range(patch)
    metadata["playlist_template_values"] = {
        "book_title": book.title,
        "episode_number": patch.patch_index + 1,
        "chapter_start": chapter_start,
        "chapter_end": chapter_end,
        "patch_name": patch_name,
        "genre_tags": ",".join(metadata.get("tags", [])),
    }
    return metadata


def enqueue_patch_publish(conn: sqlite3.Connection, patch_id: int, *, force_new: bool = False) -> dict:
    patch = get_patch(conn, patch_id)
    book = get_book(conn, patch.book_id) if patch else None
    if not patch or not book:
        raise ValueError(f"patch {patch_id} not found")
    existing = _row(conn, patch_id)
    if existing and not force_new:
        return existing
    metadata = _build_metadata_snapshot(conn, book, patch)
    video_config = get_effective_video_config(conn, book)
    intro = resolve_voice_clip(video_config, "intro_voice")
    outro = resolve_voice_clip(video_config, "outro_voice")
    music_path = None
    if book.music_id is not None:
        music = repository.get_music(conn, book.music_id)
        if music and Path(music.file_path).is_file():
            music_path = music.file_path
    render_config = {
        "background_type": video_config.get("background_type", "media"),
        "resolution": video_config["resolution"],
        "fps": video_config["fps"],
        "fit_mode": video_config["fit_mode"],
        "image_type": video_config["image_animation"],
        "codec": video_config["codec"],
        "crf": video_config["quality"],
        "audio_bitrate": video_config["audio_bitrate"],
        "music_path": music_path,
        "music_volume": book.music_volume,
        # Frozen with the rest of the render config: changing the gap settings
        # later must not silently alter a patch already queued for render.
        "music_gap_only": video_config.get("music_gap_only", True),
        "music_gap_min_ms": video_config.get("music_gap_min_ms", 1500),
        "music_gap_fade_ms": video_config.get("music_gap_fade_ms", 400),
        "intro_audio": intro,
        "outro_audio": outro,
    }
    branding = get_effective_branding_config(conn, book)
    media = {"patch_id": patch_id, "audio_path": patch.audio_path,
             "source_image": patch.image_path,
             "thumbnail_path": existing["thumbnail_path"] if existing else None,
             "render_config": render_config}
    thumbnail = ensure_patch_overlay(book, patch, settings.default_font_path or None,
                                     branding=branding) or media["thumbnail_path"]
    thumbnail_ready = bool(thumbnail and Path(thumbnail).is_file())
    media["thumbnail_path"] = thumbnail
    now = _now()
    if existing:
        conn.execute("""UPDATE patch_pipeline SET stage='upload', thumbnail_status=?,
            upload_status='pending', playlist_status='pending', youtube_upload_id=NULL, thumbnail_path=?,
            config_snapshot=?, media_snapshot=?, last_error=NULL, updated_at=? WHERE patch_id=?""",
            ("done" if thumbnail_ready else "pending", thumbnail, json.dumps(metadata), json.dumps(media), now, patch_id))
    else:
        conn.execute("""INSERT INTO patch_pipeline
            (patch_id, stage, thumbnail_status, thumbnail_path, config_snapshot, media_snapshot, created_at, updated_at)
            VALUES (?, 'thumbnail', ?, ?, ?, ?, ?, ?)""",
            (patch_id, "done" if thumbnail_ready else "pending", thumbnail, json.dumps(metadata), json.dumps(media), now, now))
    conn.commit()
    return _row(conn, patch_id)


def seed_patch_video(conn: sqlite3.Connection, patch_id: int, video_id: int, video_path: str) -> dict:
    row = _row(conn, patch_id) or enqueue_patch_publish(conn, patch_id)
    conn.execute("UPDATE patch_pipeline SET stage='upload', video_status='done', video_id=?, video_path=?, updated_at=? WHERE patch_id=?", (video_id, video_path, _now(), patch_id))
    conn.commit()
    return _row(conn, patch_id)


def _fail(conn, patch_id, stage, exc):
    message = str(exc)[:2000]
    conn.execute("UPDATE patch_pipeline SET stage=?, last_error=?, attempt_count=attempt_count+1, updated_at=? WHERE patch_id=?", (stage, message, _now(), patch_id))
    conn.commit()
    return _row(conn, patch_id)


def sync_pipeline_from_upload(conn: sqlite3.Connection, upload_id: int) -> dict | None:
    upload = conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    if not upload:
        return None
    status = upload["status"]
    stage = "auth_required" if (upload["error_message"] or "").startswith("auth_required:") else "published" if status == "done" and upload["thumbnail_status"] == "done" and upload["playlist_status"] == "done" else "thumbnail_setting" if status == "done" else "upload"
    conn.execute("""UPDATE patch_pipeline SET stage=?, upload_status=?, thumbnail_status=?, playlist_status=?,
        last_error=?, updated_at=? WHERE youtube_upload_id=?""", (stage, status, upload["thumbnail_status"], upload["playlist_status"], upload["error_message"], _now(), upload_id))
    conn.commit()
    row = conn.execute("SELECT * FROM patch_pipeline WHERE youtube_upload_id=?", (upload_id,)).fetchone()
    return dict(row) if row else None


def _create_upload_atomically(conn: sqlite3.Connection, row: dict) -> int:
    # Re-resolve instead of trusting the enqueue-time snapshot: config/override edits
    # (and metadata fixes) made between enqueue and upload must reach YouTube. The
    # stored snapshot stays as the fallback when the current config no longer resolves.
    snapshot = row["config_snapshot"]
    try:
        patch = get_patch(conn, row["patch_id"])
        book = get_book(conn, patch.book_id) if patch else None
        if not patch or not book:
            raise ValueError(f"patch {row['patch_id']} not found")
        metadata = _build_metadata_snapshot(conn, book, patch)
        snapshot = json.dumps(metadata)
    except Exception:  # noqa: BLE001 - a stale snapshot still beats a stuck pipeline
        logging.getLogger(__name__).warning(
            "metadata re-resolve failed for patch %s; uploading with the enqueue-time snapshot",
            row["patch_id"], exc_info=True,
        )
        metadata = json.loads(row["config_snapshot"])
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute("SELECT youtube_upload_id FROM patch_pipeline WHERE patch_id=?", (row["patch_id"],)).fetchone()
        if current[0]:
            conn.commit()
            return current[0]
        conn.execute("UPDATE patch_pipeline SET stage='upload', upload_status='claiming', config_snapshot=?, updated_at=? WHERE patch_id=?", (snapshot, _now(), row["patch_id"]))
        cur = conn.execute("""INSERT INTO youtube_uploads
            (video_id, video_path, title, description, tags, privacy_status, status,
             metadata_snapshot, render_source_type, render_source_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, 'patch', ?, ?)""", (row["video_id"], row["video_path"], metadata["title"], metadata["description"], json.dumps(metadata["tags"]), metadata["privacy_status"], snapshot, row["patch_id"], _now()))
        conn.execute("UPDATE patch_pipeline SET upload_status='queued', youtube_upload_id=?, updated_at=? WHERE patch_id=?", (cur.lastrowid, _now(), row["patch_id"]))
        conn.commit()
        return cur.lastrowid
    except Exception:
        conn.rollback()
        conn.execute("UPDATE patch_pipeline SET upload_status='failed', last_error=?, updated_at=? WHERE patch_id=?", ("upload claim failed", _now(), row["patch_id"]))
        conn.commit()
        raise


def run_patch_publish_stage(conn: sqlite3.Connection, patch_id: int) -> dict:
    row = _row(conn, patch_id) or enqueue_patch_publish(conn, patch_id)
    current_stage = row.get("stage", "thumbnail")
    try:
        if row["thumbnail_status"] == "done" and (not row["thumbnail_path"] or not Path(row["thumbnail_path"]).is_file()):
            conn.execute("UPDATE patch_pipeline SET thumbnail_status='pending', stage='thumbnail', updated_at=? WHERE patch_id=?", (_now(), patch_id)); conn.commit()
            row["thumbnail_status"] = "pending"
        if row["thumbnail_status"] != "done":
            current_stage = "thumbnail"
            patch = get_patch(conn, patch_id); book = get_book(conn, patch.book_id)
            path = ensure_patch_overlay(book, patch, settings.default_font_path or None)
            if not path or not Path(path).is_file(): raise ValueError("patch thumbnail could not be created")
            conn.execute("UPDATE patch_pipeline SET stage='video', thumbnail_status='done', thumbnail_path=? WHERE patch_id=?", (path, patch_id)); conn.commit()
        row = _row(conn, patch_id)
        if row["video_status"] == "done" and (not row["video_path"] or not Path(row["video_path"]).is_file()):
            conn.execute("UPDATE patch_pipeline SET video_status='pending', stage='video', updated_at=? WHERE patch_id=?", (_now(), patch_id)); conn.commit()
            row["video_status"] = "pending"
        if row["video_status"] != "done":
            current_stage = "video"
            patch = get_patch(conn, patch_id); book = get_book(conn, patch.book_id)
            output = row["video_path"] or str(Path(patch.audio_path or "").with_suffix(".mp4"))
            publish_validated_video(
                output,
                lambda temp: video_gen.generate_segment(
                    row["thumbnail_path"], patch.audio_path, temp,
                    resolution=tuple(map(int, book.video_resolution.split("x"))),
                    fps=book.video_fps or 30,
                ),
                validator=validate_video,
            )
            video = upsert_patch_video(conn, book_id=book.id, patch_id=patch_id, file_path=output, resolution=book.video_resolution)
            conn.execute("UPDATE patch_pipeline SET stage='upload', video_status='done', video_path=?, video_id=? WHERE patch_id=?", (output, video["id"], patch_id)); conn.commit()
        row = _row(conn, patch_id)
        if row["youtube_upload_id"]:
            upload = dict(conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (row["youtube_upload_id"],)).fetchone() or {})
            if upload.get("status") in {"pending", "uploading"}:
                return row
            if upload.get("status") == "failed":
                conn.execute("UPDATE youtube_uploads SET status='pending', error_message=NULL WHERE id=?", (row["youtube_upload_id"],)); conn.execute("UPDATE patch_pipeline SET upload_status='pending', stage='upload', updated_at=? WHERE patch_id=?", (_now(), patch_id)); conn.commit()
                return _row(conn, patch_id)
            if upload.get("status") == "done":
                current_stage = "thumbnail_setting" if upload.get("thumbnail_status") != "done" else "playlist"
                result = youtube.publish_completed_upload(conn, row["youtube_upload_id"])
                refreshed = dict(conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (row["youtube_upload_id"],)).fetchone())
                stage = "auth_required" if result.get("status") == "auth_required" else "published" if result.get("status") == "published" else current_stage
                conn.execute("""UPDATE patch_pipeline SET stage=?, upload_status='done', thumbnail_status=?,
                    playlist_status=?, last_error=? WHERE patch_id=?""", (stage, refreshed["thumbnail_status"], refreshed["playlist_status"], None if stage == "published" else result.get("error"), patch_id)); conn.commit()
                return _row(conn, patch_id)
        if not row["youtube_upload_id"]:
            _create_upload_atomically(conn, row)
        return _row(conn, patch_id)
    except Exception as exc:
        return _fail(conn, patch_id, current_stage, exc)


def retry_patch_publish(conn: sqlite3.Connection, patch_id: int) -> dict:
    return run_patch_publish_stage(conn, patch_id) if _row(conn, patch_id) else enqueue_patch_publish(conn, patch_id)


def enqueue_patch_video(conn: sqlite3.Connection, patch_id: int, *,
                        request_policy: dict | None = None,
                        force_new: bool = False,
                        privacy: str | None = None) -> dict:
    """Cổng tự động hoá duy nhất: preflight -> snapshot bất biến -> enqueue patch_video.

    force_new=True khi audio vừa thay đổi (result-inbox, republish): xoá render cũ và
    đóng băng snapshot theo config hiện tại. Trả về {"state": ...} — 'queued' hoặc một
    trạng thái preflight để caller hiển thị/ghi log."""
    patch = get_patch(conn, patch_id)
    if patch is None:
        return {"state": "failed", "code": "patch_not_found", "error": f"patch {patch_id} not found"}
    result = preflight_patch(conn, patch_id, request_policy=request_policy)
    if result["state"] != "ready":
        return result
    book = get_book(conn, patch.book_id)
    if book is None:
        return {"state": "failed", "code": "book_not_found",
                "error": f"book {patch.book_id} not found"}
    if force_new:
        discard_stale_patch_video(conn, patch.book_id, patch_id)
        enqueue_patch_publish(conn, patch_id, force_new=True)
    elif _row(conn, patch_id) is None:
        enqueue_patch_publish(conn, patch_id)
    else:
        pipeline_row = _row(conn, patch_id)
        if pipeline_row["stage"] == "pending" and not (pipeline_row["config_snapshot"] or "").strip("{} \n\t"):
            enqueue_patch_publish(conn, patch_id, force_new=True)
    pipeline = _row(conn, patch_id)
    if not pipeline["thumbnail_path"] or not Path(pipeline["thumbnail_path"]).is_file():
        return {"state": "failed", "code": "source_unavailable", "error": "chưa có thumbnail để render"}
    try:
        snapshot = build_enqueue_snapshot(conn, book, patch, result, pipeline)
    except ValueError as exc:
        return {"state": "failed", "code": "source_unavailable", "error": str(exc)}
    if not snapshot["audio_path"] or not Path(snapshot["audio_path"]).is_file():
        return {"state": "failed", "code": "source_unavailable",
                "error": f"audio thiếu: {snapshot['audio_path']}"}
    for key in ("music_path", "intro_audio", "outro_audio"):
        value = snapshot["render_config"].get(key)
        if value and not Path(value).is_file():
            return {"state": "failed", "code": "source_unavailable",
                    "error": f"{key} thiếu: {value}"}
    dedupe_key = f"patch_video:patch={patch_id}"
    existing = store.find_live_by_dedupe(conn, dedupe_key)
    if existing is not None:
        return {"state": "queued", "job_id": existing.id, "deduplicated": True}
    job_id = store.enqueue(
        conn, "patch_video",
        payload={
            "patch_id": patch_id,
            "snapshot": snapshot,
            "schema_version": snapshot["schema_version"],
            "upload_youtube": result["policy"]["auto_upload_youtube"],
            "privacy": privacy or result.get("privacy_status") or "private",
        },
        book_id=patch.book_id, patch_id=patch_id, dedupe_key=dedupe_key,
    )
    if job_id is None:
        return {"state": "failed", "code": "enqueue_failed", "error": "không thể enqueue job tạo video"}
    return {"state": "queued", "job_id": job_id}


def confirm_patch_republish(conn: sqlite3.Connection, patch_id: int) -> dict:
    """Xác nhận publish lại audio đã thay đổi và NGAY LẬP TỨC preflight + snapshot +
    enqueue patch_video mới (upload intent theo policy). Đóng dấu audio hiện tại
    trước khi enqueue để preflight không quay lại awaiting; render mới thay thế
    pipeline nhưng mọi row youtube_uploads cũ (lịch sử upload) được giữ nguyên.

    Trả về state/job_id của lần enqueue kèm pipeline mới."""
    patch = get_patch(conn, patch_id)
    if patch is None:
        raise ValueError(f"patch {patch_id} not found")
    pipeline = _row(conn, patch_id)
    if pipeline is None:
        raise ValueError("patch chưa publish trước đó — không cần xác nhận")
    conn.execute(
        "UPDATE patch_pipeline SET republish_confirmed_for=?, updated_at=? WHERE patch_id=?",
        (audio_fingerprint(patch), _now(), patch_id),
    )
    conn.commit()
    enqueued = enqueue_patch_video(conn, patch_id, force_new=True)
    return {**enqueued, "pipeline": _row(conn, patch_id)}


def reconcile_patch_automation(conn: sqlite3.Connection, *,
                               book_id: int | None = None,
                               request_policy: dict | None = None) -> dict:
    """Đồng bộ tự động hoá cho mọi patch có audio: patch chưa render -> enqueue
    patch_video (snapshot hiện tại), đã có video hợp lệ nhưng chưa upload -> chạy
    publish stage + enqueue youtube_upload. Chạy lúc khởi động và sau khi sửa config.

    Mỗi patch nằm trong try/except riêng — một patch lỗi không chặn phần còn lại."""
    where, params = "WHERE p.status='done'", []
    if book_id is not None:
        where += " AND p.book_id=?"
        params.append(book_id)
    rows = conn.execute(
        f"""SELECT p.id, p.book_id, p.audio_path FROM patch p
            {where} ORDER BY p.book_id, p.patch_index""", params,
    ).fetchall()
    stats = {"checked": 0, "enqueued_render": 0, "enqueued_upload": 0,
             "already_live": 0, "skipped": 0, "errors": []}
    for row in rows:
        stats["checked"] += 1
        patch_id = int(row["id"])
        try:
            if not row["audio_path"] or not Path(row["audio_path"]).is_file():
                stats["skipped"] += 1
                continue
            result = preflight_patch(conn, patch_id, request_policy=request_policy)
            if result["state"] != "ready":
                stats["skipped"] += 1
                continue
            if store.find_live_by_dedupe(conn, f"patch_video:patch={patch_id}") is not None:
                stats["already_live"] += 1
                continue
            pipeline = _row(conn, patch_id) or {}
            upload_id = pipeline.get("youtube_upload_id")
            if upload_id:
                upload = conn.execute(
                    "SELECT status FROM youtube_uploads WHERE id=?", (upload_id,),
                ).fetchone()
                if upload and upload["status"] in ("pending", "claiming", "uploading",
                                                    "waiting_for_rerender"):
                    stats["already_live"] += 1
                    continue
                if upload and upload["status"] == "done":
                    stats["skipped"] += 1
                    continue
                if upload and upload["status"] == "failed":
                    # Retry the same upload identity and rendered file. Creating a
                    # fresh render/upload here can publish the same audio twice when
                    # YouTube accepted the previous transfer but its response was lost.
                    store.enqueue(
                        conn, "youtube_upload", payload={"upload_id": upload_id},
                        book_id=row["book_id"], dedupe_key=f"youtube_upload:upload={upload_id}",
                    )
                    stats["enqueued_upload"] += 1
                    continue
            video_ready = (pipeline.get("video_status") == "done"
                           and pipeline.get("video_path")
                           and Path(pipeline["video_path"]).is_file())
            if video_ready and not upload_id:
                # Đã có video hợp lệ trên đĩa — chỉ thiếu bước upload. run_patch_publish_stage
                # không render lại khi video_status='done' (chỉ resolve metadata + tạo row).
                run_patch_publish_stage(conn, patch_id)
                refreshed = _row(conn, patch_id) or {}
                new_upload_id = refreshed.get("youtube_upload_id")
                if new_upload_id:
                    store.enqueue(
                        conn, "youtube_upload", payload={"upload_id": new_upload_id},
                        book_id=row["book_id"], dedupe_key=f"youtube_upload:upload={new_upload_id}",
                    )
                    stats["enqueued_upload"] += 1
                    continue
                stats["skipped"] += 1
                continue
            enqueued = enqueue_patch_video(conn, patch_id, request_policy=request_policy,
                                           force_new=bool(pipeline))
            if enqueued["state"] == "queued":
                stats["enqueued_render"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:  # noqa: BLE001 - mỗi patch là một chốt chặn riêng
            logging.getLogger(__name__).exception("automation reconcile failed for patch %s", patch_id)
            stats["errors"].append({"patch_id": patch_id, "error": str(exc)[:500]})
    return stats
