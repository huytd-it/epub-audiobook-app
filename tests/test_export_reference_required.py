"""Batch export requires a reference clip for reference-based models."""
from __future__ import annotations

import sqlite3

import pytest

from app import db as app_db, drive_export, repository


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    app_db.init_schema(c)
    yield c
    c.close()


def _seed_book_and_patch(conn, voice_clip_path=None):
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "voice_clip_path, created_at, updated_at) VALUES ('test', 't.epub', 't.epub', "
        "10, 'ready', ?, ?, ?)",
        (voice_clip_path, now, now),
    )
    book_id = cur.lastrowid
    conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) "
        "VALUES (?, 0, 'Chapter 1', 'Some chapter text to synthesize.', 32)",
        (book_id,),
    )
    cur = conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, "
        "status, created_at, updated_at) VALUES (?, 0, 0, 0, 'pending', ?, ?)",
        (book_id, now, now),
    )
    conn.commit()
    return repository.get_patch(conn, cur.lastrowid)


def test_batch_export_requires_voice_reference(conn):
    patch = _seed_book_and_patch(conn, voice_clip_path=None)
    with pytest.raises(ValueError, match="voice reference"):
        drive_export.build_batch_export_package(conn, [patch])


def test_batch_export_bundles_reference(conn, tmp_path, monkeypatch):
    clip = tmp_path / "voice.wav"
    clip.write_bytes(b"RIFFfakewav")
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn, voice_clip_path=str(clip))

    package_dir, batch_manifest = drive_export.build_batch_export_package(conn, [patch])
    try:
        assert batch_manifest["reference_wav"] == "reference.wav"
        assert (package_dir / "reference.wav").exists()
    finally:
        import shutil

        shutil.rmtree(package_dir, ignore_errors=True)


def test_batch_export_uses_selected_reference_voice(conn, tmp_path, monkeypatch):
    book_clip = tmp_path / "book.wav"
    book_clip.write_bytes(b"book voice")
    selected_clip = tmp_path / "voices" / "selected.wav"
    selected_clip.parent.mkdir()
    selected_clip.write_bytes(b"selected voice")
    monkeypatch.setattr(drive_export.settings, "data_root", str(tmp_path))
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn, voice_clip_path=str(book_clip))

    package_dir, _ = drive_export.build_batch_export_package(
        conn, [patch], model_id="omnivoice", voice_id="selected.wav"
    )
    try:
        assert (package_dir / "reference.wav").read_bytes() == b"selected voice"
    finally:
        import shutil

        shutil.rmtree(package_dir, ignore_errors=True)


def test_batch_export_leaves_background_and_music_behind(conn, tmp_path, monkeypatch):
    """Only text and the voice clip travel: video is rendered back in the app, so
    shipping the background image and the music track would just bloat the sync."""
    clip = tmp_path / "voice.wav"
    clip.write_bytes(b"RIFFfakewav")
    background = tmp_path / "cover.jpg"
    background.write_bytes(b"\xff\xd8fakejpeg")
    music_file = tmp_path / "loop.mp3"
    music_file.write_bytes(b"ID3fakemp3")
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn, voice_clip_path=str(clip))
    music_id = conn.execute(
        "INSERT INTO music (name, file_path, created_at) VALUES ('loop', ?, ?)",
        (str(music_file), "2026-01-01T00:00:00+00:00"),
    ).lastrowid
    conn.execute(
        "UPDATE book SET background_image_path = ?, music_id = ? WHERE id = ?",
        (str(background), music_id, patch.book_id),
    )
    conn.commit()

    package_dir, batch_manifest = drive_export.build_batch_export_package(conn, [patch])
    try:
        files = sorted(p.relative_to(package_dir).as_posix() for p in package_dir.rglob("*") if p.is_file())
        assert files == [
            "batch_manifest.json",
            "colab_kaggle_batch_tts_template.ipynb",
            "patches/patch_000/manifest.json",
            "reference.wav",
        ]
        assert "music_file" not in batch_manifest["video_config"]
        assert "background_image" not in batch_manifest["patches"][0]
    finally:
        import shutil

        shutil.rmtree(package_dir, ignore_errors=True)
