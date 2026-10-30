"""Local merge of notebook chunk output: real export manifest, real WAVs, real merge."""
from __future__ import annotations

import json
import zipfile
from contextlib import nullcontext

import numpy as np
import pytest
import soundfile as sf

from app import chunk_import, chunk_store, drive_export, repository
from app.config import settings

from test_drive_export import conn  # noqa: F401 - shared book/patch fixture

RATE = 24000


def _export(connection, book, patch, root):
    """The patch folder a notebook run leaves behind: manifest.json + output/ chunks."""
    folder = root / "patches" / f"patch_{patch.patch_index:03d}"
    manifest = drive_export._write_patch_files(
        connection, book, patch, folder, None,
        tts={"model_id": "zerotts", "voice_id": "maichi", "options": {}})
    (folder / "output").mkdir()
    for index in range(manifest["chunk_count"]):
        tone = 0.2 * np.sin(np.linspace(0, 440 * 2 * np.pi, RATE)).astype(np.float32)
        sf.write(folder / "output" / f"chunk_{index:03d}.wav", tone, RATE)
    return folder, manifest


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    # Without this the chunk stores and patch audio land in the real data/ folder.
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "data"))
    return tmp_path / "data"


def test_assembles_patch_audio_and_fills_the_chapter_chunk_stores(conn, tmp_path, data_root):
    connection, book, patch = conn
    folder, manifest = _export(connection, book, patch, tmp_path / "batch")

    audio_path = chunk_import.assemble_patch_from_chunks(
        lambda: nullcontext(connection), patch, chunk_import.read_patch_folder(folder))

    assert audio_path == str(repository.get_patch_audio_path(book.id, patch.patch_index))
    info = sf.info(audio_path)
    # Every chunk plus the pauses between them: merged here, not in the notebook.
    assert info.frames > manifest["chunk_count"] * RATE
    # Chunks now live in the per-chapter stores, keyed as remote output.
    plan = repository.build_patch_chunk_plan(connection, patch, max_chars=manifest["max_chars"] or None)
    targets = chunk_store.resolve_plan_chunks(
        book.id, repository.get_patch_chunk_dir(book.id, patch.patch_index), plan)
    for item, (cache, local) in zip(plan, targets):
        assert cache.path(local).is_file()
        assert cache.entry(local)["key"] == chunk_store.chunk_key("remote:zerotts", "maichi", item["text"])
        assert cache.has_text(local, item["text"])


def test_refuses_chunks_whose_text_no_longer_matches_the_patch(conn, tmp_path, data_root):
    connection, book, patch = conn
    folder, _ = _export(connection, book, patch, tmp_path / "batch")
    connection.execute("UPDATE chapter SET text='Completely different words now.' WHERE book_id=? AND chapter_index=0",
                       (book.id,))
    connection.commit()

    with pytest.raises(chunk_import.ChunkImportError, match="đã đổi") as caught:
        chunk_import.assemble_patch_from_chunks(
            lambda: nullcontext(connection), patch, chunk_import.read_patch_folder(folder))
    assert caught.value.transient is False
    assert not repository.get_patch_audio_path(book.id, patch.patch_index).exists()


def test_a_missing_chunk_is_transient(conn, tmp_path, data_root):
    connection, book, patch = conn
    folder, _ = _export(connection, book, patch, tmp_path / "batch")
    (folder / "output" / "chunk_001.wav").unlink()

    with pytest.raises(chunk_import.ChunkImportError, match="thiếu 1/") as caught:
        chunk_import.assemble_patch_from_chunks(
            lambda: nullcontext(connection), patch, chunk_import.read_patch_folder(folder))
    assert caught.value.transient is True


def test_refuses_a_manifest_from_another_patch(conn, tmp_path, data_root):
    connection, book, patch = conn
    folder, _ = _export(connection, book, patch, tmp_path / "batch")
    source = chunk_import.read_patch_folder(folder)
    source.manifest["patch_id"] = patch.id + 1

    with pytest.raises(chunk_import.ChunkImportError, match="không phải patch"):
        chunk_import.assemble_patch_from_chunks(lambda: nullcontext(connection), patch, source)


def test_split_drive_zips_reassemble_one_patch_folder(tmp_path):
    """Drive splits a big folder download across zips; one patch may straddle two."""
    first, second = tmp_path / "a.zip", tmp_path / "b.zip"
    with zipfile.ZipFile(first, "w") as zf:
        zf.writestr("Batch/patches/patch_000/manifest.json", "{}")
        zf.writestr("Batch/patches/patch_000/output/chunk_000.wav", b"a")
        zf.writestr("Batch/batch_manifest.json", "{}")
        zf.writestr("Batch/notebook.ipynb", "{}")
        zf.writestr("../escape/chunk_009.wav", b"x")
    with zipfile.ZipFile(second, "w") as zf:
        zf.writestr("Batch/patches/patch_000/output/chunk_001.wav", b"b")
    tree = tmp_path / "tree"
    tree.mkdir()

    chunk_import.extract_chunk_zip(first, tree)
    chunk_import.extract_chunk_zip(second, tree)

    folders = list(chunk_import.find_patch_folders(tree))
    assert [f.relative_to(tree).as_posix() for f in folders] == ["Batch/patches/patch_000"]
    assert sorted(p.name for p in (folders[0] / "output").iterdir()) == ["chunk_000.wav", "chunk_001.wav"]
    assert not (tree / "Batch" / "notebook.ipynb").exists()
    assert not (tmp_path / "escape").exists()


def test_marker_name_follows_the_result_wav_name(conn):
    _, _, patch = conn
    assert chunk_import.marker_name(drive_export.result_wav_name(patch)) == drive_export.result_chunks_name(patch)


def test_batch_manifest_points_the_notebook_at_each_marker(conn, tmp_path, monkeypatch):
    connection, _, patch = conn
    monkeypatch.setattr(drive_export, "_TMP_DIR", tmp_path / "export")
    package_dir, batch_manifest = drive_export.build_batch_export_package(
        connection, [patch], model_id="zerotts")
    try:
        entry = batch_manifest["patches"][0]
        assert entry["result_chunks"] == f"result/{drive_export.result_chunks_name(patch)}"
        assert json.loads((package_dir / "batch_manifest.json").read_text(encoding="utf-8")) == batch_manifest
    finally:
        import shutil

        shutil.rmtree(package_dir, ignore_errors=True)
