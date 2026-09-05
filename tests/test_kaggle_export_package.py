"""build_kaggle_export_package: a batch package for the Kaggle Kernels API round
trip - same manifest/reference-clip construction as the Drive package, but the
notebook copy has MODE set to "kaggle_native" and never bakes a Drive secret."""
from __future__ import annotations

import json
import sqlite3

import pytest

from app import db as app_db, drive_export, repository


def _cell1_source(package_dir):
    """Cell 1's actual source (unescaped), where MODE is assigned - reading the whole
    file as text would also match "kaggle_native" mentioned in the markdown docs and
    Cell 4's elif branch, which exist regardless of which mode was requested."""
    nb = json.loads((package_dir / "colab_kaggle_batch_tts_template.ipynb").read_text(encoding="utf-8"))
    return "".join(nb["cells"][1]["source"])


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


def test_kaggle_package_sets_mode_kaggle_native(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn)

    package_dir, _ = drive_export.build_kaggle_export_package(conn, [patch], model_id="zerotts")
    try:
        assert 'MODE = "kaggle_native"' in _cell1_source(package_dir)
    finally:
        import shutil
        shutil.rmtree(package_dir, ignore_errors=True)


def test_kaggle_package_never_bakes_a_gdrive_secret(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn)

    package_dir, _ = drive_export.build_kaggle_export_package(conn, [patch], model_id="zerotts")
    try:
        notebook = (package_dir / "colab_kaggle_batch_tts_template.ipynb").read_text(encoding="utf-8")
        assert "__GDRIVE_CREDS__" not in notebook
        # The placeholder resolves to an empty string - never a live refresh token/secret.
        assert 'GDRIVE_CREDS = \\"\\"' in notebook
    finally:
        import shutil
        shutil.rmtree(package_dir, ignore_errors=True)


def test_kaggle_package_still_writes_manifest_and_batch_manifest(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn)

    package_dir, manifest = drive_export.build_kaggle_export_package(conn, [patch], model_id="zerotts")
    try:
        assert (package_dir / "batch_manifest.json").is_file()
        assert manifest["patch_count"] == 1
        assert manifest["tts"]["model_id"] == "zerotts"
    finally:
        import shutil
        shutil.rmtree(package_dir, ignore_errors=True)


def test_drive_package_still_defaults_to_drive_mode(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn)

    package_dir, _ = drive_export.build_batch_export_package(conn, [patch], model_id="zerotts")
    try:
        # "kaggle_native" still appears in Cell 4's elif branch and the docs - only the
        # MODE assignment itself must stay "drive" when no mode is requested.
        src = _cell1_source(package_dir)
        assert 'MODE = "drive"' in src
        assert 'MODE = "kaggle_native"' not in src
    finally:
        import shutil
        shutil.rmtree(package_dir, ignore_errors=True)


def test_kaggle_package_requires_reference_for_cloning_models(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn, voice_clip_path=None)
    with pytest.raises(ValueError, match="voice reference"):
        drive_export.build_kaggle_export_package(conn, [patch], model_id="voxcpm2")
