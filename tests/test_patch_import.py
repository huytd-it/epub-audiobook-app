"""app.patch_import: resolving a batch result WAV, building an import timeline, and
atomically installing an imported WAV + timeline sidecar.

These test bodies moved unchanged out of tests/test_drive_desktop_sync.py when the
helpers themselves moved out of app.routes.patches into app.patch_import (Task 5 of
the Kaggle API automation implementation plan) -- only the import target and the
`routes.` -> `patch_import.` prefix changed."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from app import patch_import


def _wav(path, frames, rate=100):
    sf.write(path, np.zeros(frames), rate)


def test_batch_result_resolver_prefers_safe_result_and_patch_manifest(tmp_path):
    root = tmp_path / "batch"
    result = root / "result"
    patch_dir = root / "patches" / "patch_000"
    result.mkdir(parents=True)
    patch_dir.mkdir(parents=True)
    target = result / "001 - result.wav"
    target.write_bytes(b"wav")
    (patch_dir / "manifest.json").write_text("{}", encoding="utf-8")
    (root / "batch_manifest.json").write_text(json.dumps({"patches": [{
        "patch_id": 7, "folder": "patches/patch_000", "result_wav": "result/001 - result.wav"
    }]}), encoding="utf-8")
    assert patch_import.resolve_batch_result(root / "patches" / "patch_000", 7) == target
    assert patch_import.resolve_batch_result(root / "patches" / "patch_000", 8) is None


def test_atomic_install_failure_preserves_existing_pair(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    target = tmp_path / "canonical.wav"
    _wav(source, 20)
    target.write_bytes(b"old wav")
    target.with_suffix(".timeline.json").write_text("old sidecar", encoding="utf-8")
    monkeypatch.setattr(patch_import, "_atomic_copy", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        patch_import.install_imported_wav(source, target)
    assert target.read_bytes() == b"old wav"
    assert target.with_suffix(".timeline.json").read_text(encoding="utf-8") == "old sidecar"


def test_valid_result_install_copies_canonical_pair(tmp_path):
    source = tmp_path / "result.wav"
    target = tmp_path / "canonical.wav"
    _wav(source, 3000, 100)
    timeline = {"version": 1, "sample_rate": 100, "total_frames": 3000,
                "chapters": [{"chapter_index": 1, "start_frame": 0, "start_seconds": 0, "title": "One"},
                             {"chapter_index": 2, "start_frame": 1000, "start_seconds": 10, "title": "Two"},
                             {"chapter_index": 3, "start_frame": 2000, "start_seconds": 20, "title": "Three"}]}
    source.with_suffix(".timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    patch_import.install_imported_wav(source, target)
    assert target.read_bytes() == source.read_bytes()
    assert json.loads(target.with_suffix(".timeline.json").read_text()) == timeline


def test_sidecar_replace_failure_keeps_new_wav_and_removes_stale_sidecar(tmp_path, monkeypatch):
    source = tmp_path / "result.wav"
    target = tmp_path / "canonical.wav"
    _wav(source, 3000, 100)
    timeline = {"version": 1, "sample_rate": 100, "total_frames": 3000,
                "chapters": [{"chapter_index": 1, "start_frame": 0, "start_seconds": 0, "title": "One"},
                             {"chapter_index": 2, "start_frame": 1000, "start_seconds": 10, "title": "Two"},
                             {"chapter_index": 3, "start_frame": 2000, "start_seconds": 20, "title": "Three"}]}
    source.with_suffix(".timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    target.write_bytes(b"old wav")
    target.with_suffix(".timeline.json").write_bytes(b"old sidecar")
    import os
    original = os.replace
    calls = {"n": 0}
    def fail_second(source_name, destination):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("replace failed")
        return original(source_name, destination)
    monkeypatch.setattr(os, "replace", fail_second)
    patch_import.install_imported_wav(source, target)
    assert target.read_bytes() == source.read_bytes()
    assert not target.with_suffix(".timeline.json").exists()


def test_sidecar_cleanup_refusal_does_not_fail_import(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    target = tmp_path / "canonical.wav"
    _wav(source, 1000, 100)
    stale = target.with_suffix(".timeline.json")
    stale.write_text("stale", encoding="utf-8")
    original_unlink = Path.unlink
    def refuse(path, *args, **kwargs):
        if path == stale:
            raise OSError("refused")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", refuse)
    patch_import.install_imported_wav(source, target)
    assert target.read_bytes() == source.read_bytes()


def test_installer_original_error_survives_cleanup_and_restore_errors(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    target = tmp_path / "canonical.wav"
    _wav(source, 3000, 100)
    target.write_bytes(b"old wav")
    target.with_suffix(".timeline.json").write_bytes(b"old sidecar")
    monkeypatch.setattr(patch_import, "_atomic_copy", lambda *args: (_ for _ in ()).throw(OSError("original install")))
    original_replace = patch_import.os.replace
    def bad_restore(source_name, destination):
        if str(source_name).endswith(".bak"):
            raise OSError("restore failed")
        return original_replace(source_name, destination)
    monkeypatch.setattr(patch_import.os, "replace", bad_restore)
    with pytest.raises(OSError, match="original install"):
        patch_import.install_imported_wav(source, target)


@pytest.mark.parametrize("bad", [
    {"chapter_index": True, "chapter_title": "One", "is_chapter_start": True},
    {"chapter_index": 2, "chapter_title": "One", "is_chapter_start": True},
    {"chapter_index": 1, "chapter_title": "", "is_chapter_start": True},
    {"chapter_index": 1, "chapter_title": "One", "is_chapter_start": True, "filename": "chunk_000.wav"},
])
def test_chunk_metadata_rejects_exact_schema_errors(tmp_path, bad):
    paths = []
    for index in range(3):
        path = tmp_path / f"chunk_{index:03d}.wav"
        _wav(path, 1000, 100)
        paths.append(path)
    metadata = [dict(bad), {"chapter_index": 2, "chapter_title": "Two", "is_chapter_start": True},
                {"chapter_index": 3, "chapter_title": "Three", "is_chapter_start": True}]
    assert patch_import.build_import_timeline(paths, metadata, 300) is None


def test_corrupt_existing_chunk_is_rejected(tmp_path):
    paths = []
    for index in range(3):
        path = tmp_path / f"chunk_{index:03d}.wav"
        path.write_bytes(b"bad") if index == 1 else _wav(path, 1000, 100)
        paths.append(path)
    metadata = [{"chapter_index": index, "chapter_title": str(index), "is_chapter_start": True}
                for index in range(len(paths))]
    assert patch_import.build_import_timeline(paths, metadata, 300) is None


def test_chunk_metadata_builds_timeline_with_pause(tmp_path):
    first, second = tmp_path / "chunk_000.wav", tmp_path / "chunk_001.wav"
    _wav(first, 1000, 100)
    _wav(second, 2000, 100)
    third = tmp_path / "chunk_002.wav"
    _wav(third, 2000, 100)
    metadata = [{"chapter_index": 1, "chapter_title": "One", "is_chapter_start": True},
                {"chapter_index": 2, "chapter_title": "Two", "is_chapter_start": True},
                {"chapter_index": 3, "chapter_title": "Three", "is_chapter_start": True}]
    timeline = patch_import.build_import_timeline([first, second, third], metadata, pause_ms=300)
    assert timeline["version"] == 1
    assert timeline["sample_rate"] == 100
    assert timeline["total_frames"] == 5060
    assert [c["start_frame"] for c in timeline["chapters"]] == [0, 1030, 3060]
    assert [c["chapter_index"] for c in timeline["chapters"]] == [1, 2, 3]


def test_import_timeline_preserves_noncontiguous_source_chapter_indexes(tmp_path):
    paths = []
    for index in range(2):
        path = tmp_path / f"chunk_{index:03d}.wav"
        _wav(path, 1000, 100)
        paths.append(path)
    metadata = [
        {"chapter_index": 10, "chapter_title": "Ten", "is_chapter_start": True},
        {"chapter_index": 12, "chapter_title": "Twelve", "is_chapter_start": True},
    ]
    timeline = patch_import.build_import_timeline(paths, metadata, pause_ms=300)
    assert [c["chapter_index"] for c in timeline["chapters"]] == [10, 12]


@pytest.mark.parametrize("indexes", [[10, 10], [12, 10]])
def test_import_timeline_rejects_duplicate_or_regressing_chapter_indexes(tmp_path, indexes):
    paths = []
    for index in range(2):
        path = tmp_path / f"chunk_{index:03d}.wav"
        _wav(path, 1000, 100)
        paths.append(path)
    metadata = [{"chapter_index": indexes[index], "chapter_title": str(index),
                 "is_chapter_start": True}
                for index in range(len(paths))]
    assert patch_import.build_import_timeline(paths, metadata, pause_ms=300) is None


def test_invalid_chunk_metadata_disables_timeline(tmp_path):
    chunk = tmp_path / "chunk_000.wav"
    _wav(chunk, 100, 100)
    assert patch_import.build_import_timeline([chunk], [{"chapter_index": 1}], pause_ms=300) is None


@pytest.mark.parametrize("count", [1, 2])
def test_chunk_metadata_builds_timeline_for_short_imports(tmp_path, count):
    paths = []
    metadata = []
    for index in range(count):
        path = tmp_path / f"chunk_{index:03d}.wav"
        _wav(path, 1000, 100)
        paths.append(path)
        metadata.append({"chapter_index": index + 1,
                         "chapter_title": f"Chapter {index + 1}", "is_chapter_start": True})
    assert patch_import.build_import_timeline(paths, metadata, pause_ms=300) is not None
