"""Tests for the media browser API (app/routes/media_browser.py)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    # Create all whitelisted dirs
    for d in ("backgrounds", "music", "voices", "uploads", "effects", "videos"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    # Also create an assets dir next to app/
    assets = Path(__file__).resolve().parent.parent / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    with TestClient(app) as c:
        yield c


def _seed_files(tmp_path: Path):
    """Create sample files in various roots, including book media."""
    # Backgrounds
    (tmp_path / "backgrounds" / "sunset.jpg").write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
    (tmp_path / "backgrounds" / "ocean.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (tmp_path / "backgrounds" / "clip.mp4").write_bytes(b"\x00\x00\x00\x1cftypisom")

    # Music
    (tmp_path / "music" / "theme.mp3").write_bytes(b"\xff\xfb\x90\x00fake-mp3")

    # Voices
    (tmp_path / "voices" / "narrator.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE")

    # Uploads
    (tmp_path / "uploads" / "doc.pdf").write_bytes(b"%PDF-1.4 fake")

    # Nested subdir
    sub = tmp_path / "backgrounds" / "subdir"
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "nested.png").write_bytes(b"\x89PNG\r\n\x1a\nnested")

    # --- Book media (patch overlays, podcast cover, patch videos, patch audio) ---
    book_dir = tmp_path / "books" / "1"
    (book_dir / "patch_overlays").mkdir(parents=True, exist_ok=True)
    (book_dir / "patch_overlays" / "1_001.png").write_bytes(b"\x89PNG\r\n\x1a\noverlay")
    (book_dir / "patch_overlays" / "1_002.png").write_bytes(b"\x89PNG\r\n\x1a\noverlay2")

    (book_dir / "podcast_cover.png").write_bytes(b"\x89PNG\r\n\x1a\ncover")

    (book_dir / "patch_videos").mkdir(parents=True, exist_ok=True)
    (book_dir / "patch_videos" / "1.mp4").write_bytes(b"\x00\x00\x00\x1cftypisom-vid")

    (book_dir / "patches").mkdir(parents=True, exist_ok=True)
    (book_dir / "patches" / "1.wav").write_bytes(b"RIFF\x00\x00\x00\x00WAVE-patch")

    # Final videos
    (tmp_path / "videos").mkdir(parents=True, exist_ok=True)
    (tmp_path / "videos" / "final_1.mp4").write_bytes(b"\x00\x00\x00\x1cftypisom-final")


# ------------------------------------------------------------------ #
#  browse – root
# ------------------------------------------------------------------ #


def test_browse_empty_path_returns_roots(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse")
    assert resp.status_code == 200
    data = resp.json()
    names = {e["name"] for e in data["entries"]}
    assert "Ảnh nền" in names
    assert "Nhạc nền" in names
    assert "Giọng mẫu" in names
    assert "Sách" in names
    assert "Video" in names
    assert all(e["is_dir"] for e in data["entries"])
    # All root keys returned
    roots = data["roots"]
    assert "_Nền" in roots
    assert "_Sách" in roots
    assert "_Video" in roots
    assert "_Nhạc" in roots
    assert "_Giọng" in roots


def test_browse_root_paths_are_qualified(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse")
    data = resp.json()
    paths = {e["path"] for e in data["entries"]}
    assert "_Nền" in paths
    assert "_Sách" in paths
    assert "_Video" in paths


# ------------------------------------------------------------------ #
#  browse – subdirectory
# ------------------------------------------------------------------ #


def test_browse_subdirectory_lists_files(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Nền"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] == "_Nền"
    names = {e["name"] for e in data["entries"]}
    assert "sunset.jpg" in names
    assert "ocean.png" in names
    assert "subdir" in names


def test_browse_nested_dir_paths_are_qualified(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Nền/subdir"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["path"] == "_Nền/subdir"
    for entry in data["entries"]:
        assert entry["path"].startswith("_Nền/subdir/")


def test_browse_category_filter(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Nền", "category": "videos"})
    assert resp.status_code == 200
    names = {e["name"] for e in resp.json()["entries"]}
    assert "clip.mp4" in names
    assert "sunset.jpg" not in names


def test_browse_nested_dir(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Nền/subdir"})
    assert resp.status_code == 200
    names = {e["name"] for e in resp.json()["entries"]}
    assert "nested.png" in names


# ------------------------------------------------------------------ #
#  browse – book media visibility
# ------------------------------------------------------------------ #


def test_browse_books_root_shows_book_dirs(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Sách"})
    assert resp.status_code == 200
    names = {e["name"] for e in resp.json()["entries"]}
    assert "1" in names


def test_browse_book_dir_shows_patch_overlays_and_podcast_cover(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Sách/1"})
    assert resp.status_code == 200
    names = {e["name"] for e in resp.json()["entries"]}
    assert "patch_overlays" in names
    assert "podcast_cover.png" in names
    assert "patch_videos" in names
    assert "patches" in names


def test_browse_patch_overlays_lists_images(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Sách/1/patch_overlays"})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    names = {e["name"] for e in entries}
    assert "1_001.png" in names
    assert "1_002.png" in names
    for e in entries:
        if e["name"].endswith(".png"):
            assert e["kind"] == "image"
            assert e["path"].startswith("_Sách/1/patch_overlays/")


def test_browse_patch_videos_lists_videos(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Sách/1/patch_videos"})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["name"] == "1.mp4"
    assert entries[0]["kind"] == "video"
    assert entries[0]["path"] == "_Sách/1/patch_videos/1.mp4"


def test_browse_patch_audio_lists_audio(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Sách/1/patches"})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["name"] == "1.wav"
    assert entries[0]["kind"] == "audio"
    assert entries[0]["path"] == "_Sách/1/patches/1.wav"


def test_browse_final_videos(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Video"})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    names = {e["name"] for e in entries}
    assert "final_1.mp4" in names
    for e in entries:
        if e["name"].endswith(".mp4"):
            assert e["kind"] == "video"


def test_browse_category_thumbnails_shows_book_overlays_and_covers(client, tmp_path):
    """The 'thumbnails' category should surface image files from book patch_overlays."""
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Sách/1/patch_overlays", "category": "thumbnails"})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 2
    for e in entries:
        assert e["kind"] == "image"


def test_browse_category_videos_shows_book_patch_videos_and_final(client, tmp_path):
    """The 'videos' category should surface video files from books and the videos root."""
    _seed_files(tmp_path)
    # Book patch videos
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Sách/1/patch_videos", "category": "videos"})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["kind"] == "video"

    # Final videos
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Video", "category": "videos"})
    assert resp.status_code == 200
    entries = resp.json()["entries"]
    assert len(entries) == 1
    assert entries[0]["kind"] == "video"


# ------------------------------------------------------------------ #
#  browse – security
# ------------------------------------------------------------------ #


def test_browse_unknown_root_404(client):
    resp = client.get("/api/ui/media-browser/browse", params={"path": "nonexistent"})
    assert resp.status_code == 404


def test_browse_traversal_rejected(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Nền/../../etc"})
    assert resp.status_code == 403


def test_browse_traversal_rejected_book(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Sách/../../etc"})
    assert resp.status_code == 403


def test_browse_symlink_escape_rejected(client, tmp_path):
    """Symlinks pointing outside the root should not be traversable."""
    _seed_files(tmp_path)
    target = tmp_path / "backgrounds" / "escapee"
    # Create a symlink that points outside the allowed area
    try:
        target.symlink_to(tmp_path.parent / "secret.txt")
    except OSError:
        pytest.skip("Symlinks not supported on this platform")
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Nền"})
    assert resp.status_code == 200
    # The symlink entry itself may appear, but its contents cannot be navigated into
    names = {e["name"] for e in resp.json()["entries"]}
    assert "escapee" in names  # entry is visible
    # Navigating into it should fail (target resolves outside root)
    resp2 = client.get("/api/ui/media-browser/browse", params={"path": "_Nền/escapee"})
    assert resp2.status_code in (403, 404)


# ------------------------------------------------------------------ #
#  preview
# ------------------------------------------------------------------ #


def test_preview_serves_file(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/preview", params={"path": "_Nền/sunset.jpg"})
    assert resp.status_code == 200
    assert "image" in resp.headers["content-type"]


def test_preview_book_overlay(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/preview", params={"path": "_Sách/1/patch_overlays/1_001.png"})
    assert resp.status_code == 200
    assert "image" in resp.headers["content-type"]


def test_preview_book_video(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/preview", params={"path": "_Sách/1/patch_videos/1.mp4"})
    assert resp.status_code == 200
    assert "video" in resp.headers["content-type"]


def test_preview_missing_file_404(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/preview", params={"path": "_Nền/nope.jpg"})
    assert resp.status_code == 404


def test_preview_traversal_rejected(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/preview", params={"path": "_Nền/../../.env"})
    assert resp.status_code == 403


def test_preview_requires_path(client):
    resp = client.get("/api/ui/media-browser/preview")
    assert resp.status_code == 422  # missing required param


def test_preview_unknown_root_404(client):
    resp = client.get("/api/ui/media-browser/preview", params={"path": "nonexistent/file.txt"})
    assert resp.status_code == 404


# ------------------------------------------------------------------ #
#  info
# ------------------------------------------------------------------ #


def test_info_returns_metadata(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/info", params={"path": "_Nhạc/theme.mp3"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "theme.mp3"
    assert data["kind"] == "audio"
    assert data["size"] > 0
    assert data["path"].startswith("_Nhạc/")


def test_info_book_overlay(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/info", params={"path": "_Sách/1/patch_overlays/1_001.png"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "1_001.png"
    assert data["kind"] == "image"
    assert data["path"] == "_Sách/1/patch_overlays/1_001.png"


def test_info_traversal_rejected(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/info", params={"path": "_Nhạc/../../config.py"})
    assert resp.status_code == 403


def test_info_unknown_root_404(client):
    resp = client.get("/api/ui/media-browser/info", params={"path": "nonexistent/file.txt"})
    assert resp.status_code == 404


# ------------------------------------------------------------------ #
#  category filters
# ------------------------------------------------------------------ #


def test_category_backgrounds_includes_videos_and_images(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Nền", "category": "backgrounds"})
    assert resp.status_code == 200
    kinds = {e["kind"] for e in resp.json()["entries"]}
    assert "image" in kinds
    assert "video" in kinds


def test_category_voices_filters_to_audio(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Giọng", "category": "voices"})
    assert resp.status_code == 200
    for e in resp.json()["entries"]:
        assert e["kind"] == "audio"


def test_category_music_filters_to_audio(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Nhạc", "category": "music"})
    assert resp.status_code == 200
    for e in resp.json()["entries"]:
        assert e["kind"] == "audio"


def test_category_logos_filters_to_images(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/browse", params={"path": "_Logo", "category": "logos"})
    assert resp.status_code == 200
    for e in resp.json()["entries"]:
        if not e["is_dir"]:
            assert e["kind"] == "image"


# ------------------------------------------------------------------ #
#  SPA routing integration
# ------------------------------------------------------------------ #


def test_spa_media_browser_path(client):
    resp = client.get("/media-browser", headers={"accept": "text/html"})
    assert resp.status_code == 200
    assert "studio-mark.svg" in resp.text


# ------------------------------------------------------------------ #
#  /api/ui/media-browser/resolve
# ------------------------------------------------------------------ #


def test_resolve_returns_absolute_path_for_existing_file(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/resolve", params={"path": "_Nền/sunset.jpg"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["exists"] is True
    assert body["name"] == "sunset.jpg"
    assert Path(body["path"]).is_absolute()


def test_resolve_404_for_nonexistent_file(client, tmp_path):
    _seed_files(tmp_path)
    resp = client.get("/api/ui/media-browser/resolve", params={"path": "_Nền/nope.png"})
    assert resp.status_code == 404


def test_resolve_404_for_unknown_root(client, tmp_path):
    resp = client.get("/api/ui/media-browser/resolve", params={"path": "unknown/file.txt"})
    assert resp.status_code == 404


def test_resolve_requires_subpath(client, tmp_path):
    resp = client.get("/api/ui/media-browser/resolve", params={"path": "_Nền"})
    assert resp.status_code == 400
