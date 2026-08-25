"""Video branding overlay: transparent PNG rendered once, applied via FFmpeg overlay.

Covers: render_branding_overlay (resolution, targets, content), video_gen
branding_overlay_path plumbing, and double-application prevention.
"""
from __future__ import annotations

import inspect
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from app import image_overlay
from app.image_overlay import render_branding_overlay
from app.production_defaults import validate_branding_config


def _make_image(w=640, h=480, color=(100, 100, 100)):
    return Image.new("RGB", (w, h), color)


def _make_logo(path: str, size=(100, 100)):
    img = Image.new("RGBA", size, (255, 0, 0, 200))
    img.save(path, "PNG")
    return path


# ---------------------------------------------------------------------------
# render_branding_overlay
# ---------------------------------------------------------------------------


def test_render_branding_overlay_returns_none_when_disabled():
    branding = validate_branding_config({})
    result = render_branding_overlay((1920, 1080), branding, target="video")
    assert result is None


def test_render_branding_overlay_returns_none_when_watermark_disabled():
    branding = validate_branding_config({
        "watermark": {"enabled": False, "text": "Hidden"},
    })
    result = render_branding_overlay((1920, 1080), branding, target="video")
    assert result is None


def test_render_branding_overlay_returns_none_when_target_disabled():
    branding = validate_branding_config({
        "watermark": {"enabled": True, "text": "Test"},
        "targets": {"video": False},
    })
    result = render_branding_overlay((1920, 1080), branding, target="video")
    assert result is None


def test_render_branding_overlay_produces_rgba_image():
    branding = validate_branding_config({
        "watermark": {"enabled": True, "text": "MyChannel"},
    })
    result = render_branding_overlay((1920, 1080), branding, target="video")
    assert result is not None
    assert result.mode == "RGBA"
    assert result.size == (1920, 1080)


def test_render_branding_overlay_has_transparency():
    branding = validate_branding_config({
        "watermark": {"enabled": True, "text": "Watermark"},
    })
    result = render_branding_overlay((640, 480), branding, target="video")
    assert result is not None
    # Most pixels should be transparent (only watermark area is opaque)
    pixels = list(result.getdata())
    transparent_count = sum(1 for p in pixels if p[3] == 0)
    assert transparent_count > len(pixels) * 0.5, "overlay should be mostly transparent"


def test_render_branding_overlay_with_logo():
    with tempfile.TemporaryDirectory() as tmp:
        logo_path = _make_logo(str(Path(tmp) / "logo.png"))
        branding = validate_branding_config({
            "watermark": {"enabled": True, "text": "Text"},
            "logo": {"enabled": True, "path": logo_path, "size": 50},
        })
        result = render_branding_overlay((1920, 1080), branding, target="video")
        assert result is not None
        assert result.size == (1920, 1080)


def test_render_branding_overlay_logo_only():
    with tempfile.TemporaryDirectory() as tmp:
        logo_path = _make_logo(str(Path(tmp) / "logo.png"))
        branding = validate_branding_config({
            "logo": {"enabled": True, "path": logo_path, "size": 50},
        })
        result = render_branding_overlay((1280, 720), branding, target="video")
        assert result is not None
        assert result.size == (1280, 720)


def test_render_branding_overlay_logo_missing_file():
    branding = validate_branding_config({
        "logo": {"enabled": True, "path": "/nonexistent/logo.png"},
    })
    result = render_branding_overlay((1920, 1080), branding, target="video")
    # Logo missing, watermark disabled => None
    assert result is None


def test_render_branding_overlay_logo_missing_but_watermark_enabled():
    branding = validate_branding_config({
        "watermark": {"enabled": True, "text": "Watermark"},
        "logo": {"enabled": True, "path": "/nonexistent/logo.png"},
    })
    result = render_branding_overlay((1920, 1080), branding, target="video")
    assert result is not None
    assert result.size == (1920, 1080)


def test_render_branding_overlay_different_targets():
    branding = validate_branding_config({
        "watermark": {"enabled": True, "text": "Test"},
        "targets": {"thumbnail": True, "podcast": False, "video": True},
    })
    # video target should work
    assert render_branding_overlay((1920, 1080), branding, target="video") is not None
    # thumbnail target should work
    assert render_branding_overlay((1920, 1080), branding, target="thumbnail") is not None
    # podcast target is disabled
    assert render_branding_overlay((1920, 1080), branding, target="podcast") is None


def test_render_branding_overlay_saves_to_file():
    branding = validate_branding_config({
        "watermark": {"enabled": True, "text": "File Test"},
    })
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "branding.png"
        result = render_branding_overlay((1920, 1080), branding, target="video")
        assert result is not None
        result.save(str(out), "PNG")
        result.close()
        assert out.exists()
        # Re-open and verify
        img = Image.open(str(out))
        assert img.mode == "RGBA"
        assert img.size == (1920, 1080)
        img.close()


# ---------------------------------------------------------------------------
# Branding not applied to source thumbnails (no double application)
# ---------------------------------------------------------------------------


def test_compose_patch_overlay_no_double_branding(tmp_path):
    """compose_patch_overlay applies branding to thumbnail, not to video overlay."""
    bg_path = tmp_path / "bg.png"
    _make_image().save(str(bg_path))
    book = SimpleNamespace(id=1, title="Test Book", background_image_path=str(bg_path))
    patch = SimpleNamespace(name="Ep 1", patch_index=0, chapter_start=1, chapter_end=5)
    cfg = image_overlay.parse_overlay_config(None)
    branding = validate_branding_config({
        "watermark": {"enabled": True, "text": "Branded"},
    })
    # Compose overlay with branding (thumbnail target)
    result = image_overlay.compose_patch_overlay(book, patch, cfg, str(bg_path), branding=branding)
    # The result should have branding applied via PIL (for thumbnail use)
    assert result.size[0] > 0

    # Now render the video branding overlay - it should be separate
    video_overlay = render_branding_overlay((1920, 1080), branding, target="video")
    assert video_overlay is not None
    assert video_overlay.mode == "RGBA"
    # The video overlay is a separate transparent image, not the same as the thumbnail
    assert video_overlay.size == (1920, 1080)


# ---------------------------------------------------------------------------
# generate_segment accepts branding_overlay_path
# ---------------------------------------------------------------------------


def test_generate_segment_signature_includes_branding():
    """Verify generate_segment has the branding_overlay_path parameter."""
    import inspect
    sig = inspect.signature(image_overlay.apply_branding)
    # Check apply_branding has target param
    assert "target" in sig.parameters

    from app.video_gen import generate_segment
    sig = inspect.signature(generate_segment)
    assert "branding_overlay_path" in sig.parameters


def test_generate_background_sequence_signature_includes_branding():
    from app.video_gen import generate_background_sequence
    sig = inspect.signature(generate_background_sequence)
    assert "branding_overlay_path" in sig.parameters


def test_generate_full_video_signature_includes_branding():
    from app.video_gen import generate_full_video
    sig = inspect.signature(generate_full_video)
    assert "branding_overlay_path" in sig.parameters


def test_generate_standalone_video_signature_includes_branding():
    from app.video_gen import generate_standalone_video
    sig = inspect.signature(generate_standalone_video)
    assert "branding_overlay_path" in sig.parameters


# ---------------------------------------------------------------------------
# All video paths pass branding
# ---------------------------------------------------------------------------


def test_patch_video_handler_passes_branding_to_render(tmp_path, monkeypatch):
    """_render_from_snapshot reads branding from snapshot and generates overlay."""
    import json
    from datetime import datetime, timezone
    from app import db, youtube
    from app.config import settings
    from app.jobqueue import store
    from app.jobqueue.context import JobContext
    from app.jobqueue.handlers import patch_video
    from app.jobqueue.joblog import JobLogger
    from app.video_integrity import ValidationFacts, ValidationResult

    conn = db.connect(str(tmp_path / "brand.db")); db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    audio = tmp_path / "a.wav"; audio.write_bytes(b"RIFF" + b"\x00" * 100)
    image = tmp_path / "bg.jpg"; image.write_bytes(b"i")
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "default_background_image", str(image))
    monkeypatch.setattr(settings, "default_font_path", "")
    conn.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,video_resolution,video_fps,created_at,updated_at) VALUES (1,'B','b','b',1,'done','1280x720',24,?,?)", (now, now))
    conn.execute("INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,audio_path,created_at,updated_at) VALUES (2,1,0,0,0,'done',?,?,?)", (str(audio), now, now))
    conn.commit()

    seen = {}
    monkeypatch.setattr(patch_video.image_overlay, "ensure_patch_overlay", lambda *a, **k: str(image))

    def mock_generate_segment(image_path, audio_path, out_path, **kw):
        seen["branding_overlay_path"] = kw.get("branding_overlay_path")
        Path(out_path).write_bytes(b"new")
    monkeypatch.setattr(patch_video.video_gen, "generate_segment", mock_generate_segment)
    monkeypatch.setattr(patch_video, "validate_video", lambda p, **kw: ValidationResult(True, None, "", (), ValidationFacts(), 0))

    # Build a valid audio fingerprint for the snapshot
    patch_obj = conn.execute("SELECT * FROM patch WHERE id=2").fetchone()
    from app.patch_publishing import audio_fingerprint
    # Create a simple namespace to match what audio_fingerprint expects
    patch_ns = SimpleNamespace(audio_path=str(audio))
    fp = audio_fingerprint(patch_ns)

    branding_config = validate_branding_config({
        "watermark": {"enabled": True, "text": "TestBrand"},
    })
    snapshot = {
        "schema_version": 2,
        "background_type": "media",
        "audio_path": str(audio),
        "audio_fingerprint": fp,
        "thumbnail_path": str(image),
        "render_config": {"resolution": "1280x720", "fps": 24, "fit_mode": "auto", "codec": "libx264", "crf": 23, "audio_bitrate": "192k"},
        "sequence": False,
        "backgrounds": [],
        "image": str(image),
        "image_type": "none",
        "branding": branding_config,
        "sequence_config": {},
    }

    job_id = store.enqueue(conn, "patch_video", payload={"patch_id": 2, "snapshot": snapshot, "schema_version": 2}, book_id=1)
    job = store.claim(conn, "patch_video", "w")
    patch_video.handle(JobContext(job, conn, JobLogger(job_id, "patch_video"), lambda: False))

    # Branding overlay was passed to generate_segment
    assert seen.get("branding_overlay_path") is not None
    assert Path(seen["branding_overlay_path"]).exists()


def test_video_handler_passes_branding(tmp_path, monkeypatch):
    """video.py _render generates branding overlay and passes to generate_full_video."""
    import json
    from datetime import datetime, timezone
    from app import db, repository
    from app.config import settings
    from app.jobqueue import store
    from app.jobqueue.context import JobContext
    from app.jobqueue.handlers import video as video_handler
    from app.jobqueue.joblog import JobLogger
    from app.video_integrity import ValidationFacts, ValidationResult

    conn = db.connect(str(tmp_path / "vh.db")); db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "default_background_image", str(tmp_path / "bg.jpg"))
    (tmp_path / "bg.jpg").write_bytes(b"i")
    branding_config = validate_branding_config({
        "watermark": {"enabled": True, "text": "VHBrand"},
    })
    config = json.dumps({"inherit": {"branding": False}, "branding": branding_config})
    final_audio = tmp_path / "final.wav"; final_audio.write_bytes(b"final")
    conn.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,video_resolution,video_fps,automation_config,final_audio_path,created_at,updated_at) VALUES (1,'B','b','b',1,'done','1280x720',24,?,?,?,?)", (config, str(final_audio), now, now))
    conn.execute("INSERT INTO book_job (id,book_id,job_type,status,created_at,updated_at) VALUES (1,1,'video','running',?,?)", (now, now))
    conn.commit()

    seen = {}
    def mock_full_video(patches, book, out, **kw):
        seen["branding_overlay_path"] = kw.get("branding_overlay_path")
        Path(out).write_bytes(b"full")
    monkeypatch.setattr(video_handler.video_gen, "generate_full_video", mock_full_video)
    monkeypatch.setattr(video_handler, "validate_video", lambda p, **kw: ValidationResult(True, None, "", (), ValidationFacts(), 0))

    # Create a mock context with .conn attribute
    class MockCtx:
        def __init__(self, conn):
            self.conn = conn
        def progress(self, *a, **kw): pass
        def log(self, *a, **kw): pass
        def heartbeat(self): pass
    ctx = MockCtx(conn)
    result = video_handler._render(ctx, 1, 1)
    assert seen.get("branding_overlay_path") is not None
    assert Path(seen["branding_overlay_path"]).exists()


def test_standalone_video_handler_passes_branding(tmp_path, monkeypatch):
    """standalone_video.py resolves branding from book and passes to generate_standalone_video."""
    import json
    from datetime import datetime, timezone
    from app import db, repository
    from app.config import settings
    from app.jobqueue import store
    from app.jobqueue.context import JobContext
    from app.jobqueue.handlers import standalone_video
    from app.jobqueue.joblog import JobLogger
    from app.video_integrity import ValidationFacts, ValidationResult

    conn = db.connect(str(tmp_path / "sv.db")); db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    audio = tmp_path / "a.wav"; audio.write_bytes(b"audio")
    bg = tmp_path / "bg.jpg"; bg.write_bytes(b"bg")
    branding_config = validate_branding_config({
        "watermark": {"enabled": True, "text": "SVBrand"},
    })
    config = json.dumps({"inherit": {"branding": False}, "branding": branding_config})
    conn.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,video_resolution,video_fps,automation_config,created_at,updated_at) VALUES (1,'B','b','b',1,'done','1920x1080',30,?,?,?)", (config, now, now))
    conn.execute("INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,audio_path,created_at,updated_at) VALUES (2,1,0,0,0,'done',?,?,?)", (str(audio), now, now))
    render_config = {"resolution": "1920x1080", "fps": 30, "fit_mode": "auto", "codec": "libx264", "audio_bitrate": "192k", "image_type": "none", "crf": 23}
    conn.execute("INSERT INTO videos (filename,original_name,file_path,source_audio,background_path,render_config_json,book_id,patch_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 ("out.mp4", "out.mp4", str(tmp_path / "out.mp4"), str(audio), str(bg), json.dumps(render_config), 1, 2, now, now))
    conn.commit()

    seen = {}
    def mock_standalone(audio_path, image_path, out_path, **kw):
        seen["branding_overlay_path"] = kw.get("branding_overlay_path")
        Path(out_path).write_bytes(b"standalone")
    monkeypatch.setattr(standalone_video.video_gen, "generate_standalone_video", mock_standalone)
    monkeypatch.setattr(standalone_video, "validate_video", lambda p, **kw: ValidationResult(True, None, "", (), ValidationFacts(), 0))

    video_id = conn.execute("SELECT id FROM videos").fetchone()[0]
    job_id = store.enqueue(conn, "standalone_video", payload={"video_id": video_id}, book_id=1)
    job = store.claim(conn, "standalone_video", "w")
    standalone_video.handle(JobContext(job, conn, JobLogger(job_id, "standalone_video"), lambda: False))

    assert seen.get("branding_overlay_path") is not None
    assert Path(seen["branding_overlay_path"]).exists()
