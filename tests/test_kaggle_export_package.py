"""build_kaggle_export_package: a batch package for the Kaggle Kernels API round
trip - same manifest/reference-clip construction as the Drive package, with MODE
set to "kaggle_native". Drive creds are baked only when the app has a Drive
account (retry resume via Drive sync); without creds the kernel stays offline."""
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


def test_kaggle_package_enables_is_kaggle(conn, tmp_path, monkeypatch):
    """Cell 3/Cell 4 branch on IS_KAGGLE, not on MODE: with IS_KAGGLE=False the pushed
    kernel runs Cell 3's Colab drive.mount branch and dies with NotImplementedError
    (first live run, 2026-09-06)."""
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn)

    package_dir, _ = drive_export.build_kaggle_export_package(conn, [patch], model_id="zerotts")
    try:
        src = _cell1_source(package_dir)
        assert "IS_KAGGLE = True" in src
        assert "IS_KAGGLE = False" not in src
        # The lookalikes elsewhere in the notebook must be untouched: the manual-flow
        # docs and Cell 4's own Colab-path message still say False.
        raw = (package_dir / "colab_kaggle_batch_tts_template.ipynb").read_text(encoding="utf-8")
        assert "Keep `IS_KAGGLE = False`" in raw
        assert 'IS_KAGGLE = False - skipping the Kaggle' in raw
    finally:
        import shutil
        shutil.rmtree(package_dir, ignore_errors=True)


def test_kaggle_package_without_creds_stays_offline(conn, tmp_path, monkeypatch):
    """No Drive account -> empty GDRIVE_CREDS placeholder, kernel runs fully
    offline and results travel back only through kernel_output()."""
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


def test_kaggle_package_with_creds_bakes_drive_sync(conn, tmp_path, monkeypatch):
    """With a Drive account the creds are baked into BOTH Drive branches of Cell 4
    (manual-drive mode and kaggle_native retry-resume), so the kernel can mirror
    chunk/output files to Drive and a retry resumes instead of restarting."""
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn)
    creds = {"client_id": "cid", "client_secret": "cs", "refresh_token": "rt"}

    package_dir, _ = drive_export.build_kaggle_export_package(
        conn, [patch], model_id="zerotts", gdrive_creds=creds,
    )
    try:
        nb = json.loads((package_dir / "colab_kaggle_batch_tts_template.ipynb").read_text(encoding="utf-8"))
        cell4 = "".join(nb["cells"][4]["source"])
        assert "__GDRIVE_CREDS__" not in cell4
        # Escaped JSON payload lands in the notebook (not the raw refresh token).
        assert "rt" not in cell4 or "refresh_token" in cell4
        assert cell4.count("cid") >= 2  # both branches
    finally:
        import shutil
        shutil.rmtree(package_dir, ignore_errors=True)


def test_kaggle_package_accepts_stable_batch_id(conn, tmp_path, monkeypatch):
    """The handler passes one stable batch_id per job so every retry maps to the
    same Drive sync folder; it must land in the manifest and every BATCH_ID slot."""
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn)

    package_dir, manifest = drive_export.build_kaggle_export_package(
        conn, [patch], model_id="zerotts", batch_id="stable-batch-1",
        drive_folder_name="stable-folder",
    )
    try:
        assert manifest["batch_id"] == "stable-batch-1"
        nb = json.loads((package_dir / "colab_kaggle_batch_tts_template.ipynb").read_text(encoding="utf-8"))
        cell4 = "".join(nb["cells"][4]["source"])
        assert "__BATCH_ID__" not in cell4
        assert cell4.count("stable-batch-1") >= 2  # native + drive branches
        assert "stable-folder" in cell4
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
        # The Drive/Colab package keeps IS_KAGGLE=False: on Colab Cell 3 must mount
        # Drive, and manual-Kaggle users flip the flag by hand per the docs.
        assert "IS_KAGGLE = False" in src
        assert "IS_KAGGLE = True" not in src
    finally:
        import shutil
        shutil.rmtree(package_dir, ignore_errors=True)


def test_kaggle_package_requires_reference_for_cloning_models(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    patch = _seed_book_and_patch(conn, voice_clip_path=None)
    with pytest.raises(ValueError, match="voice reference"):
        drive_export.build_kaggle_export_package(conn, [patch], model_id="voxcpm2")
