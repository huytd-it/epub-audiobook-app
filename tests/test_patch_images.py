"""Tests for patch image CRUD, resolve_patch_image, video generation, and Video Creator."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app import db, repository, video_gen
from app.models import Book, Patch


def _make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _insert_book(conn, *, book_id=1, status="ready", final_audio_path=None,
                 background_image_path=None, video_resolution="1920x1080",
                 video_fps=30, default_image_animation="none"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                              final_audio_path, background_image_path, video_resolution,
                              video_fps, default_image_animation, created_at, updated_at)
           VALUES (?, 't', 'f.epub', '/tmp/f.epub', 10, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (book_id, status, final_audio_path, background_image_path,
         video_resolution, video_fps, default_image_animation, now, now),
    )
    conn.commit()


def _insert_patch(conn, *, patch_id=1, book_id=1, patch_index=0, status="done",
                  audio_path="/tmp/audio.wav", image_path=None, image_type="static"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end,
                              status, audio_path, image_path, image_type,
                              created_at, updated_at)
           VALUES (?, ?, ?, 0, 5, ?, ?, ?, ?, ?, ?)""",
        (patch_id, book_id, patch_index, status, audio_path, image_path, image_type, now, now),
    )
    conn.commit()


# ---- Patch Image CRUD ----

def test_save_patch_image():
    conn = _make_conn()
    _insert_book(conn)
    _insert_patch(conn, image_path=None)
    repository.save_patch_image(conn, 1, "/tmp/img.jpg")
    p = repository.get_patch(conn, 1)
    assert p.image_path == "/tmp/img.jpg"


def test_clear_patch_image():
    conn = _make_conn()
    _insert_book(conn)
    _insert_patch(conn, image_path="/tmp/img.jpg")
    repository.clear_patch_image(conn, 1)
    p = repository.get_patch(conn, 1)
    assert p.image_path is None


def test_update_patch_image_type():
    conn = _make_conn()
    _insert_book(conn)
    _insert_patch(conn)
    repository.update_patch_image_type(conn, 1, "zoom-in")
    p = repository.get_patch(conn, 1)
    assert p.image_type == "zoom-in"


def test_update_book_video_settings():
    conn = _make_conn()
    _insert_book(conn)
    repository.update_book_video_settings(
        conn, 1, video_resolution="1280x720", video_fps=24, default_image_animation="zoom-in"
    )
    b = repository.get_book(conn, 1)
    assert b.video_resolution == "1280x720"
    assert b.video_fps == 24
    assert b.default_image_animation == "zoom-in"


# ---- resolve_patch_image fallback chain ----

def test_resolve_patch_image_uses_patch_image(tmp_path):
    img = tmp_path / "patch.jpg"
    img.touch()
    patch = Patch(id=1, book_id=1, patch_index=0, chapter_start=0, chapter_end=5,
                  status="done", audio_path="/tmp/a.wav", error_message=None,
                  attempt_count=0, created_at="", updated_at="",
                  image_path=str(img), image_type="static")
    book = Book(id=1, title="t", original_filename="f", epub_path="", patch_size=10,
                status="done", final_audio_path="/tmp/a.wav", final_video_path=None,
                background_image_path="/tmp/bg.jpg", voice_clip_path=None,
                voice_transcript=None, created_at="", updated_at="")
    result = video_gen.resolve_patch_image(patch, book, "/tmp/default.jpg")
    assert result == str(img)


def test_resolve_patch_image_falls_back_to_book_bg(tmp_path):
    bg = tmp_path / "bg.jpg"
    bg.touch()
    patch = Patch(id=1, book_id=1, patch_index=0, chapter_start=0, chapter_end=5,
                  status="done", audio_path="/tmp/a.wav", error_message=None,
                  attempt_count=0, created_at="", updated_at="",
                  image_path=None, image_type="static")
    book = Book(id=1, title="t", original_filename="f", epub_path="", patch_size=10,
                status="done", final_audio_path="/tmp/a.wav", final_video_path=None,
                background_image_path=str(bg), voice_clip_path=None,
                voice_transcript=None, created_at="", updated_at="")
    result = video_gen.resolve_patch_image(patch, book, "/tmp/default.jpg")
    assert result == str(bg)


def test_resolve_patch_image_falls_back_to_default(tmp_path):
    default = tmp_path / "default.jpg"
    default.touch()
    patch = Patch(id=1, book_id=1, patch_index=0, chapter_start=0, chapter_end=5,
                  status="done", audio_path="/tmp/a.wav", error_message=None,
                  attempt_count=0, created_at="", updated_at="",
                  image_path=None, image_type="static")
    book = Book(id=1, title="t", original_filename="f", epub_path="", patch_size=10,
                status="done", final_audio_path="/tmp/a.wav", final_video_path=None,
                background_image_path=None, voice_clip_path=None,
                voice_transcript=None, created_at="", updated_at="")
    result = video_gen.resolve_patch_image(patch, book, str(default))
    assert result == str(default)


def test_resolve_patch_image_returns_none_when_nothing_available():
    patch = Patch(id=1, book_id=1, patch_index=0, chapter_start=0, chapter_end=5,
                  status="done", audio_path="/tmp/a.wav", error_message=None,
                  attempt_count=0, created_at="", updated_at="",
                  image_path=None, image_type="static")
    result = video_gen.resolve_patch_image(patch, None, "/tmp/nonexistent.jpg")
    assert result is None


# ---- generate_segment (mock ffmpeg) ----

@patch("app.video_gen.subprocess.run")
def test_generate_segment_static(mock_run):
    video_gen.generate_segment(
        "/tmp/img.jpg", "/tmp/audio.wav", "/tmp/out.mp4",
        image_type="none", resolution=(1920, 1080), fps=30,
    )
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert "ffmpeg" in cmd[0]
    assert "-loop" in cmd
    assert "1920" in " ".join(cmd)


@patch("app.video_gen.subprocess.run")
def test_generate_segment_animated_zoom_in(mock_run):
    mock_run.return_value = MagicMock(stdout="10.0\n")
    video_gen.generate_segment(
        "/tmp/img.jpg", "/tmp/audio.wav", "/tmp/out.mp4",
        image_type="zoom-in", resolution=(1280, 720), fps=24,
    )
    assert mock_run.call_count == 2  # ffprobe + ffmpeg
    ffmpeg_cmd = mock_run.call_args_list[1][0][0]
    assert "zoompan" in " ".join(ffmpeg_cmd)


# ---- concat_segments (mock ffmpeg) ----

@patch("app.video_gen.subprocess.run")
def test_concat_segments(mock_run, tmp_path):
    seg1 = tmp_path / "seg1.mp4"
    seg1.write_bytes(b"fake1")
    seg2 = tmp_path / "seg2.mp4"
    seg2.write_bytes(b"fake2")
    out = tmp_path / "out.mp4"
    video_gen.concat_segments([str(seg1), str(seg2)], str(out))
    mock_run.assert_called_once()


# ---- backfill with patch images ----

def test_backfill_includes_books_with_patch_images():
    conn = _make_conn()
    _insert_book(conn, book_id=1, status="done", final_audio_path="/tmp/1.wav",
                 background_image_path=None)
    _insert_patch(conn, patch_id=1, book_id=1, image_path="/tmp/patch1.jpg")
    n = repository.backfill_video_book_jobs(conn)
    assert n == 1


def test_backfill_excludes_books_without_any_images():
    conn = _make_conn()
    _insert_book(conn, book_id=1, status="done", final_audio_path="/tmp/1.wav",
                 background_image_path=None)
    _insert_patch(conn, patch_id=1, book_id=1, image_path=None)
    n = repository.backfill_video_book_jobs(conn)
    assert n == 0
