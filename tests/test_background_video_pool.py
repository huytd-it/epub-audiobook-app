"""save_clip must refuse a download that is not a decodable video.

A 16-byte stub (`ftyp` header, no `moov` atom) used to be persisted as a
background clip; at render time ffmpeg aborted the whole piece with
AVERROR_INVALIDDATA. Reject it at the boundary instead.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.config import settings
from app.services.background_video import pool


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "bg_video_root", "")
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def _candidate(*, source_url="https://example.test/v.mp4"):
    def download_to(dest):
        dest.write_bytes(b"junk")
        return dest.stat().st_size

    return pool.ClipCandidate(
        keyword="thien-nhien", platform="pexels", source_url=source_url,
        download_to=download_to,
    )


def test_save_clip_rejects_a_non_video_download(conn, monkeypatch):
    monkeypatch.setattr(pool, "_probe_duration", lambda path: 0.0)

    with pytest.raises(RuntimeError, match="not a decodable video"):
        pool.save_clip(conn, _candidate())

    assert conn.execute("SELECT COUNT(*) FROM background_clip").fetchone()[0] == 0
    saved_dir = Path(settings.data_root) / "backgrounds" / "thien-nhien"
    assert list(saved_dir.glob("*.mp4")) == []


def test_save_clip_validates_even_when_duration_is_supplied(conn, monkeypatch):
    """Pexels/DTK report a duration up front; it must not skip the check."""
    monkeypatch.setattr(pool, "_probe_duration", lambda path: 0.0)
    clip = _candidate()
    clip.duration_seconds = 42.0

    with pytest.raises(RuntimeError, match="not a decodable video"):
        pool.save_clip(conn, clip)


def test_save_clip_stores_a_decodable_video(conn, monkeypatch):
    monkeypatch.setattr(pool, "_probe_duration", lambda path: 12.5)

    saved = pool.save_clip(conn, _candidate())

    assert saved["duration_seconds"] == 12.5
    assert Path(saved["path"]).is_file()
    assert conn.execute("SELECT COUNT(*) FROM background_clip").fetchone()[0] == 1
