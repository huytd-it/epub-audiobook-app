"""Production defaults + per-book inherit/custom mode resolution.

Covers: fresh books inherit the global defaults; legacy books stay custom;
the f:inherit dict is authoritative; saves pin a group to custom; the
/production-settings endpoints expose defaults + modes + effective values;
the bulk Start queue resolves the book's actual audio config.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import db, repository
from app.config import settings
from app.jobqueue import store
from app.jobqueue.backfill import enqueue_pending_patch_jobs
from app.production_defaults import (get_effective_audio_config,
                                     get_effective_normalization_options,
                                     get_effective_video_config,
                                     get_effective_youtube_config,
                                     get_global_production_defaults,
                                     get_group_mode, parse_book_config,
                                     save_global_production_defaults,
                                     set_book_group_mode_db, set_group_mode)


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _insert_book(conn, book_id=1, config=None, **columns):
    base = {"video_resolution": "1920x1080", "video_fps": 30,
            "default_image_animation": "none",
            "normalize_numbers_enabled": 1, "normalize_junk_enabled": 1,
            "normalize_spellcheck_enabled": 1, "normalize_dictionary_enabled": 0,
            "normalize_transliteration_enabled": 0,
            "tts_model": None, "tts_voice_id": None, "tts_max_chars": None,
            "tts_with_effects": 0}
    base.update(columns)
    automation = json.dumps(config) if config is not None else None
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                             automation_config, video_resolution, video_fps,
                             default_image_animation, normalize_numbers_enabled,
                             normalize_junk_enabled, normalize_spellcheck_enabled,
                             normalize_dictionary_enabled, normalize_transliteration_enabled,
                             tts_model, tts_voice_id, tts_max_chars, tts_with_effects,
                             created_at, updated_at)
           VALUES (?, 'Book', 'b.epub', 'b.epub', 10, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (book_id, automation, base["video_resolution"], base["video_fps"],
         base["default_image_animation"], base["normalize_numbers_enabled"],
         base["normalize_junk_enabled"], base["normalize_spellcheck_enabled"],
         base["normalize_dictionary_enabled"], base["normalize_transliteration_enabled"],
         base["tts_model"], base["tts_voice_id"], base["tts_max_chars"],
         base["tts_with_effects"],
         "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    return repository.get_book(conn, book_id)


def _book(conn, book_id=1):
    return repository.get_book(conn, book_id)


# ---------------------------------------------------------------------------
# Mode resolution
# ---------------------------------------------------------------------------


def test_fresh_book_inherits_every_group():
    conn = _conn()
    book = _insert_book(conn)
    config = parse_book_config(book)
    assert get_group_mode(config, "audio", book=book) == "inherit"
    assert get_group_mode(config, "normalization", book=book) == "inherit"
    assert get_group_mode(config, "video", book=book) == "inherit"
    assert get_group_mode(config, "youtube", book=book) == "inherit"


def test_legacy_book_with_non_default_columns_is_custom():
    conn = _conn()
    book = _insert_book(conn, config={}, video_resolution="1280x720",
                        normalize_numbers_enabled=0,
                        tts_model="edge-tts", tts_voice_id="vi-VN-HoaiMyNeural")
    config = parse_book_config(book)
    assert get_group_mode(config, "video", book=book) == "custom"
    assert get_group_mode(config, "normalization", book=book) == "custom"
    assert get_group_mode(config, "audio", book=book) == "custom"
    # youtube has no book columns: a legacy book with only columns stays inherit
    assert get_group_mode(config, "youtube", book=book) == "inherit"


def test_any_stored_section_marks_a_legacy_book_custom():
    conn = _conn()
    book = _insert_book(conn, config={"youtube": {"genre_tags": "a,b"}})
    config = parse_book_config(book)
    for group in ("audio", "normalization", "video", "youtube"):
        assert get_group_mode(config, group, book=book) == "custom"


def test_explicit_inherit_dict_is_authoritative():
    config = {"inherit": {"youtube": False}, "youtube": {"genre_tags": "a,b"}}
    assert get_group_mode(config, "youtube", book=None) == "custom"
    assert get_group_mode(config, "audio", book=None) == "inherit"
    assert get_group_mode(config, "video", book=None) == "inherit"
    assert get_group_mode(config, "normalization", book=None) == "inherit"


def test_set_group_mode_maps_bool_flags():
    raw = {}
    set_group_mode(raw, "audio", "inherit")
    set_group_mode(raw, "youtube", "custom")
    assert raw["inherit"] == {"audio": True, "youtube": False}
    assert get_group_mode(raw, "audio") == "inherit"
    assert get_group_mode(raw, "youtube") == "custom"


def test_set_book_group_mode_db_is_persisted():
    conn = _conn()
    book = _insert_book(conn)
    set_book_group_mode_db(conn, book.id, "normalization", "custom")
    reloaded = _book(conn, book.id)
    config = parse_book_config(reloaded)
    assert config["inherit"] == {"normalization": False}
    assert get_group_mode(config, "normalization", book=reloaded) == "custom"
    assert get_group_mode(config, "audio", book=reloaded) == "inherit"


# ---------------------------------------------------------------------------
# Global defaults CRUD
# ---------------------------------------------------------------------------


def test_global_defaults_return_hardcoded_values_when_unset():
    conn = _conn()
    defaults = get_global_production_defaults(conn)
    assert defaults["audio"]["model_id"] == settings.tts_engine
    assert defaults["audio"]["max_chars"] == settings.tts_max_chars
    assert defaults["audio"]["with_effects"] is False
    assert defaults["normalization"] == {
        "numbers": True, "junk": True, "spellcheck": True,
        "dictionary": False, "transliteration": False,
    }
    assert defaults["video"]["resolution"] == "1920x1080"
    assert defaults["video"]["fps"] == 30
    assert defaults["youtube"]["privacy_status"] == "private"


def test_save_global_defaults_merges_partial_updates():
    conn = _conn()
    saved = save_global_production_defaults(conn, {"audio": {"model_id": "edge-tts"}})
    assert saved["audio"]["model_id"] == "edge-tts"
    assert saved["video"]["resolution"] == "1920x1080"
    # untouched groups keep their defaults, not the fresh hardcoded ones
    later = save_global_production_defaults(conn, {"video": {"resolution": "1280x720"}})
    assert later["audio"]["model_id"] == "edge-tts"
    assert later["video"]["resolution"] == "1280x720"
    reloaded = get_global_production_defaults(conn)
    assert reloaded["audio"]["model_id"] == "edge-tts"
    assert reloaded["video"]["resolution"] == "1280x720"


def test_corrupted_stored_block_falls_back_to_hardcoded_defaults():
    conn = _conn()
    conn.execute(
        """INSERT INTO automation_settings (id, schema_version, config_json, created_at, updated_at)
           VALUES (1, 1, 'not-json', ?, ?)""",
        ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    defaults = get_global_production_defaults(conn)
    assert defaults["audio"]["model_id"] == settings.tts_engine


def test_effective_configs_follow_mode():
    conn = _conn()
    fresh = _insert_book(conn, book_id=1)
    save_global_production_defaults(conn, {"audio": {"model_id": "vieneu-fast"}})
    # fresh book inherits the new global audio default live
    assert get_effective_audio_config(conn, fresh)["model_id"] == "vieneu-fast"
    # a legacy custom audio book keeps its own config
    custom = _insert_book(conn, book_id=2, tts_model="edge-tts", tts_voice_id="vi-VN-HoaiMyNeural")
    assert get_effective_audio_config(conn, custom)["model_id"] == "edge-tts"
    assert get_effective_audio_config(conn, custom)["voice_id"] == "vi-VN-HoaiMyNeural"


def test_effective_video_matches_book_columns_and_global():
    conn = _conn()
    fresh = _insert_book(conn)
    assert get_effective_video_config(conn, fresh)["resolution"] == "1920x1080"
    save_global_production_defaults(conn, {"video": {"resolution": "1280x720"}})
    assert get_effective_video_config(conn, fresh)["resolution"] == "1280x720"
    legacy = _insert_book(conn, book_id=2, config={"video": {"codec": "h264_nvenc"}}, video_fps=24)
    assert get_effective_video_config(conn, legacy)["codec"] == "h264_nvenc"
    assert get_effective_video_config(conn, legacy)["fps"] == 24


def test_effective_youtube_resolves_from_book_and_global():
    conn = _conn()
    fresh = _insert_book(conn)
    assert get_effective_youtube_config(conn, fresh)["privacy_status"] == "private"
    save_global_production_defaults(conn, {"youtube": {"privacy_status": "unlisted"}})
    assert get_effective_youtube_config(conn, fresh)["privacy_status"] == "unlisted"
    legacy = _insert_book(conn, book_id=2, config={"youtube": {"privacy_status": "public"}})
    assert get_effective_youtube_config(conn, legacy)["privacy_status"] == "public"


def test_effective_normalization_follows_mode():
    conn = _conn()
    fresh = _insert_book(conn)
    save_global_production_defaults(conn, {"normalization": {"numbers": False}})
    opts = get_effective_normalization_options(conn, fresh)
    assert opts.numbers is False
    legacy = _insert_book(conn, book_id=2, normalize_numbers_enabled=0)
    assert get_effective_normalization_options(conn, legacy).numbers is False


# ---------------------------------------------------------------------------
# /production-settings endpoints
# ---------------------------------------------------------------------------


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "enable_worker", False)
    return TestClient(__import__("app.main", fromlist=["app"]).app)


def test_production_settings_get_defaults(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/production-settings")
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["defaults"]["audio"]["model_id"] == settings.tts_engine
    assert set(body["defaults"]) == {"audio", "normalization", "video", "youtube", "branding"}


def test_production_settings_round_trip_the_merge_pauses(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        assert client.get("/production-settings").json()["defaults"]["audio"]["chapter_pause_ms"] == 1500
        saved = client.post("/production-settings", json={
            "audio": {"model_id": "edge-tts", "chunk_pause_ms": 250, "chapter_pause_ms": 2200},
        })
        assert saved.status_code == 200, saved.text
        reloaded = client.get("/production-settings").json()["defaults"]["audio"]
    assert reloaded["chunk_pause_ms"] == 250
    assert reloaded["chapter_pause_ms"] == 2200


def test_production_settings_get_per_book_modes_and_effective(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        conn = client.app.state.conn
        _insert_book(conn, book_id=1, tts_model="edge-tts")
        response = client.get("/production-settings?book_id=1")
    assert response.status_code == 200
    body = response.json()
    assert body["book_id"] == 1
    assert body["modes"]["audio"] == "custom"
    assert body["modes"]["youtube"] == "inherit"
    assert body["effective"]["audio"]["model_id"] == "edge-tts"


def test_production_settings_post_partial_and_applies_live(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        conn = client.app.state.conn
        _insert_book(conn, book_id=1)
        saved = client.post("/production-settings", json={"audio": {"model_id": "gtts"}})
        reloaded = client.get("/production-settings?book_id=1")
        invalid = client.post("/production-settings", json={"max_chars": 999})
    assert saved.status_code == 200
    assert saved.json()["defaults"]["audio"]["model_id"] == "gtts"
    assert reloaded.json()["effective"]["audio"]["model_id"] == "gtts"
    assert invalid.status_code == 400


def test_book_mode_endpoint_switches_to_inherit(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        conn = client.app.state.conn
        _insert_book(conn, book_id=1, tts_model="edge-tts")
        client.post("/production-settings", json={"audio": {"model_id": "gtts"}})
        response = client.post("/books/1/production-settings-mode", json={"group": "audio", "mode": "inherit"})
    assert response.status_code == 200
    assert response.json()["effective"]["model_id"] == "gtts"


# ---------------------------------------------------------------------------
# Legacy save routes pin the group to custom
# ---------------------------------------------------------------------------


def test_audio_settings_save_marks_mode_custom(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        conn = client.app.state.conn
        _insert_book(conn, book_id=1)
        response = client.post("/books/1/audio-settings", json={
            "model_id": "omnivoice", "voice_id": "", "max_chars": 500, "with_effects": True,
        })
        book = repository.get_book(conn, 1)
    assert response.status_code == 200
    assert response.json()["model_id"] == "omnivoice"
    assert get_group_mode(parse_book_config(book), "audio", book=book) == "custom"
    # and the GET now reflects the book's own config
    with _client(monkeypatch, tmp_path) as client:
        conn = client.app.state.conn
        got = client.get("/books/1/audio-settings")
    assert got.json() == {
        "model_id": "omnivoice", "voice_id": "", "max_chars": 500, "with_effects": True,
        "chunk_pause_ms": 300, "chapter_pause_ms": 1500,
        "tts_options": {},
    }


def test_video_config_save_marks_mode_custom(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        conn = client.app.state.conn
        _insert_book(conn, book_id=1)
        response = client.post("/books/1/video-config", json={"codec": "h264_nvenc", "quality": 20})
        book = repository.get_book(conn, 1)
    assert response.status_code == 200
    assert get_group_mode(parse_book_config(book), "video", book=book) == "custom"


def test_normalization_save_marks_mode_custom(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        conn = client.app.state.conn
        _insert_book(conn, book_id=1)
        response = client.post("/books/1/normalization",
                               data={"numbers": "on", "junk": "off", "spellcheck": "off",
                                     "dictionary": "on", "transliteration": "off"},
                               headers={"X-Requested-With": "autosave"})
        book = repository.get_book(conn, 1)
    assert response.status_code == 200
    assert get_group_mode(parse_book_config(book), "normalization", book=book) == "custom"


# ---------------------------------------------------------------------------
# Bulk Start queue reads the book's actual audio config
# ---------------------------------------------------------------------------


def _patch(conn, book_id=1):
    cur = conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                              attempt_count, created_at, updated_at)
           VALUES (?, 0, 0, 0, 'pending', 0, ?, ?)""",
        (book_id, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))
    conn.commit()
    return cur.lastrowid


def test_bulk_start_queue_uses_book_audio_config_when_custom():
    conn = _conn()
    _insert_book(conn, tts_model="edge-tts", tts_voice_id="vi-VN-HoaiMyNeural")
    _patch(conn)
    assert enqueue_pending_patch_jobs(conn) == 1
    job = store.list_jobs(conn, job_type="audiobook_tts")[0]
    assert job.payload["tts_engine"] == "edge-tts"
    assert job.payload["voice"] == "vi-VN-HoaiMyNeural"


def test_bulk_start_queue_uses_default_engine_for_inherited_book():
    conn = _conn()
    _insert_book(conn)
    _patch(conn)
    assert enqueue_pending_patch_jobs(conn) == 1
    job = store.list_jobs(conn, job_type="audiobook_tts")[0]
    assert job.payload["tts_engine"] == settings.tts_engine


def test_bulk_start_queue_explicit_engine_still_wins():
    conn = _conn()
    _insert_book(conn, tts_model="edge-tts")
    _patch(conn)
    assert enqueue_pending_patch_jobs(conn, tts_engine="voxcpm2") == 1
    job = store.list_jobs(conn, job_type="audiobook_tts")[0]
    assert job.payload["tts_engine"] == "voxcpm2"


def test_production_settings_round_trip_youtube_timeline_and_extra(tmp_path, monkeypatch):
    """The defaults page edits the youtube group as a whole; the two new blocks
    must survive a save/read cycle and reach a book that inherits them."""
    extra = {"enabled": True, "contact_email": "me@example.com", "story_title": "Truyen X",
             "story_source_name": "Nguon", "story_source_url": "https://example.com",
             "fair_use_url": "https://example.com/fair-use", "template": "Contact: {contact_email}"}
    with _client(monkeypatch, tmp_path) as client:
        conn = client.app.state.conn
        _insert_book(conn, book_id=1)
        current = client.get("/production-settings").json()["defaults"]["youtube"]
        saved = client.post("/production-settings", json={"groups": {
            "youtube": {**current, "timeline_enabled": False, "description_extra": extra},
        }})
        assert saved.status_code == 200
        stored = saved.json()["defaults"]["youtube"]
        assert stored["timeline_enabled"] is False
        assert stored["description_extra"]["contact_email"] == "me@example.com"
        effective = client.get("/production-settings?book_id=1").json()["effective"]["youtube"]
    assert effective["timeline_enabled"] is False
    assert effective["description_extra"]["story_title"] == "Truyen X"


def test_production_settings_rejects_an_invalid_extra_block(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post("/production-settings", json={"groups": {
            "youtube": {"description_extra": {"enabled": "sometimes"}},
        }})
    assert response.status_code == 400


def test_tts_models_are_listed_without_a_book_in_scope(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/api/ui/tts-models")
    assert response.status_code == 200
    assert response.json()["tts_models"]


def _patch_with_timeline(tmp_path):
    """Một patch có audio 30 giây + sidecar timeline 3 chương cách nhau 10 giây."""
    import numpy as np
    import soundfile as sf

    audio = tmp_path / "episode.wav"
    sf.write(audio, np.zeros(300), 10)
    audio.with_suffix(".timeline.json").write_text(json.dumps({
        "version": 1, "sample_rate": 10, "total_frames": 300, "chapters": [
            {"chapter_index": 1, "start_frame": 0, "start_seconds": 0, "title": "Mot"},
            {"chapter_index": 2, "start_frame": 100, "start_seconds": 10, "title": "Hai"},
            {"chapter_index": 3, "start_frame": 200, "start_seconds": 20, "title": "Ba"},
        ]}), encoding="utf-8")
    return SimpleNamespace(name="Mua", chapter_start=1, chapter_end=3, patch_index=0,
                           audio_path=str(audio))


def test_effective_metadata_shifts_the_timeline_by_the_configured_intro(tmp_path, monkeypatch):
    """Video phát intro trước nội dung patch: mốc chương phải dời đúng độ dài intro."""
    import numpy as np
    import soundfile as sf

    from app.production_defaults import resolve_effective_youtube_metadata

    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    voices = tmp_path / "voices"
    voices.mkdir()
    sf.write(voices / "intro.wav", np.zeros(12 * 16000), 16000)

    conn = _conn()
    book = _insert_book(conn, config={"youtube": {"description": "mo ta"}})
    # video inherit để nhận intro từ global defaults, youtube custom để giữ
    # description tự viết (timeline được nối vào sau nó).
    set_book_group_mode_db(conn, book.id, "video", "inherit")
    set_book_group_mode_db(conn, book.id, "youtube", "custom")
    book = _book(conn)
    patch = _patch_with_timeline(tmp_path)

    before = resolve_effective_youtube_metadata(conn, book, patch, None)["description"]
    assert before == "mo ta\n\n00:00 Mot\n00:10 Hai\n00:20 Ba"

    save_global_production_defaults(conn, {"video": {"intro_voice": "intro.wav"}})
    after = resolve_effective_youtube_metadata(conn, _book(conn), patch, None)["description"]
    assert after == "mo ta\n\n00:00 Giới thiệu\n00:12 Mot\n00:22 Hai\n00:32 Ba"


def test_effective_metadata_ignores_an_intro_whose_file_is_gone(tmp_path, monkeypatch):
    from app.production_defaults import resolve_effective_youtube_metadata

    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn = _conn()
    book = _insert_book(conn, config={"youtube": {"description": "mo ta"}})
    set_book_group_mode_db(conn, book.id, "video", "inherit")
    set_book_group_mode_db(conn, book.id, "youtube", "custom")
    save_global_production_defaults(conn, {"video": {"intro_voice": "missing.wav"}})
    description = resolve_effective_youtube_metadata(
        conn, _book(conn), _patch_with_timeline(tmp_path), None)["description"]
    assert description == "mo ta\n\n00:00 Mot\n00:10 Hai\n00:20 Ba"


# ---------------------------------------------------------------------------
# Branding
# ---------------------------------------------------------------------------


def test_branding_is_included_in_global_groups():
    from app.production_defaults import GROUPS, validate_branding_config
    assert "branding" in GROUPS
    cfg = validate_branding_config({})
    assert cfg["watermark"]["enabled"] is False
    assert cfg["logo"]["enabled"] is False
    assert cfg["targets"]["thumbnail"] is True
    assert cfg["targets"]["podcast"] is True
    assert cfg["targets"]["video"] is True


def test_branding_validates_positions():
    from app.production_defaults import validate_branding_config
    cfg = validate_branding_config({
        "watermark": {"position": "invalid", "text": "hi"},
        "logo": {"position": "invalid"},
    })
    assert cfg["watermark"]["position"] == "bottom-right"
    assert cfg["logo"]["position"] == "bottom-right"


def test_branding_validates_ranges():
    from app.production_defaults import validate_branding_config
    cfg = validate_branding_config({
        "watermark": {"font_size": 999, "opacity": 150, "margin": -5},
        "logo": {"size": 0, "opacity": -10, "margin": 999},
    })
    assert cfg["watermark"]["font_size"] == 120
    assert cfg["watermark"]["opacity"] == 100
    assert cfg["watermark"]["margin"] == 0
    assert cfg["logo"]["size"] == 16
    assert cfg["logo"]["opacity"] == 0
    assert cfg["logo"]["margin"] == 200


def test_fresh_book_inherits_branding():
    conn = _conn()
    book = _insert_book(conn)
    config = parse_book_config(book)
    assert get_group_mode(config, "branding", book=book) == "inherit"


def test_branding_custom_mode_via_inherit_dict():
    raw = {"inherit": {"branding": False}, "branding": {"watermark": {"enabled": True}}}
    assert get_group_mode(raw, "branding") == "custom"


def test_global_branding_defaults_are_empty():
    conn = _conn()
    defaults = get_global_production_defaults(conn)
    assert defaults["branding"]["watermark"]["enabled"] is False
    assert defaults["branding"]["logo"]["enabled"] is False


def test_save_global_branding_persists():
    conn = _conn()
    saved = save_global_production_defaults(conn, {"branding": {
        "watermark": {"enabled": True, "text": "My Channel"},
        "logo": {"enabled": True, "path": "/tmp/logo.png"},
    }})
    assert saved["branding"]["watermark"]["enabled"] is True
    assert saved["branding"]["watermark"]["text"] == "My Channel"
    assert saved["branding"]["logo"]["enabled"] is True
    assert saved["branding"]["logo"]["path"] == "/tmp/logo.png"
    reloaded = get_global_production_defaults(conn)
    assert reloaded["branding"]["watermark"]["text"] == "My Channel"


def test_effective_branding_follows_mode():
    from app.production_defaults import get_effective_branding_config
    conn = _conn()
    fresh = _insert_book(conn)
    save_global_production_defaults(conn, {"branding": {
        "watermark": {"enabled": True, "text": "Global"},
    }})
    assert get_effective_branding_config(conn, fresh)["watermark"]["text"] == "Global"
    custom = _insert_book(conn, book_id=2, config={
        "inherit": {"branding": False},
        "branding": {"watermark": {"enabled": True, "text": "Book"}},
    })
    assert get_effective_branding_config(conn, custom)["watermark"]["text"] == "Book"


def test_branding_appears_in_production_settings_endpoint(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        response = client.get("/production-settings")
    assert response.status_code == 200
    body = response.json()
    assert "branding" in body["defaults"]
    assert body["defaults"]["branding"]["watermark"]["enabled"] is False


def test_branding_save_via_production_settings_endpoint(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        saved = client.post("/production-settings", json={"branding": {
            "watermark": {"enabled": True, "text": "Test"},
        }})
        assert saved.status_code == 200
        assert saved.json()["defaults"]["branding"]["watermark"]["text"] == "Test"
        reloaded = client.get("/production-settings")
    assert reloaded.json()["defaults"]["branding"]["watermark"]["text"] == "Test"


def test_book_branding_mode_endpoint(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        conn = client.app.state.conn
        _insert_book(conn, book_id=1)
        response = client.post("/books/1/production-settings-mode",
                               json={"group": "branding", "mode": "custom"})
    assert response.status_code == 200
    assert response.json()["mode"] == "custom"
