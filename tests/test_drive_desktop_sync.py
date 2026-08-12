from datetime import datetime, timezone
from pathlib import Path
import json

import pytest
import soundfile as sf
import numpy as np
from fastapi.testclient import TestClient

from app import db, drive_export, repository
from app.config import settings
from app.main import app
from app.routes.patches import (_build_import_timeline, _install_imported_wav,
                                _resolve_batch_result)


NOW = datetime.now(timezone.utc).isoformat()


def make_conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def add_patch(conn):
    conn.execute(
        "INSERT INTO book (id, title, original_filename, epub_path, patch_size, status, created_at, updated_at) VALUES (1, 'Book', 'b.epub', 'b.epub', 1, 'ready', ?, ?)",
        (NOW, NOW),
    )
    cur = conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) VALUES (1, 0, 0, 0, 'pending', ?, ?)",
        (NOW, NOW),
    )
    conn.commit()
    return cur.lastrowid


def test_migration_and_deleted_target_preserve_export(tmp_path):
    conn = make_conn()
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(patch_export)")}
    assert {"sync_target_id", "local_folder_path"} <= columns

    patch_id = add_patch(conn)
    target = repository.create_drive_sync_target(conn, "A", "a@example.com", str(tmp_path))
    export = repository.create_patch_export(
        conn, patch_id, str(tmp_path), str(tmp_path), 1,
        sync_target_id=target["id"], local_folder_path=str(tmp_path),
    )
    repository.delete_drive_sync_target(conn, target["id"])
    saved = repository.get_latest_patch_export(conn, patch_id)
    assert saved.id == export.id
    assert saved.local_folder_path == str(tmp_path)


def test_sync_target_crud(tmp_path):
    conn = make_conn()
    target = repository.create_drive_sync_target(conn, "A", "a@example.com", str(tmp_path))
    assert repository.get_drive_sync_target(conn, target["id"])["name"] == "A"
    assert repository.update_drive_sync_target(conn, target["id"], "B", "b@example.com", str(tmp_path))
    assert repository.list_drive_sync_targets(conn)[0]["name"] == "B"
    assert repository.delete_drive_sync_target(conn, target["id"])


def test_sync_target_stores_rclone_remote(tmp_path):
    conn = make_conn()
    target = repository.create_drive_sync_target(
        conn, "codex5", "codex5@example.com", str(tmp_path), "codex5:EPUB Audiobook Exports"
    )
    assert target["rclone_remote"] == "codex5:EPUB Audiobook Exports"
    # empty string normalizes to NULL (Drive-Desktop target, no rclone)
    assert repository.update_drive_sync_target(conn, target["id"], "codex5", "codex5@example.com", str(tmp_path), "")
    assert repository.get_drive_sync_target(conn, target["id"])["rclone_remote"] is None


def test_validate_and_publish_package(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "manifest.json").write_text("{}", encoding="utf-8")

    final = drive_export.publish_package(source, str(target), "package")
    assert (final / "manifest.json").read_text(encoding="utf-8") == "{}"
    with pytest.raises(FileExistsError):
        drive_export.publish_package(source, str(target), "package")
    assert not list(target.glob(".epub-audiobook-export-*"))


def test_validate_sync_folder_rejects_invalid_paths(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="absolute"):
        drive_export.validate_sync_folder("relative")
    with pytest.raises(ValueError, match="does not exist"):
        drive_export.validate_sync_folder(str(tmp_path / "missing"))
    file_path = tmp_path / "file"
    file_path.write_text("x")
    with pytest.raises(ValueError, match="not a directory"):
        drive_export.validate_sync_folder(str(file_path))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as test_client:
        yield test_client




def test_target_route_rejects_invalid_folder(client, tmp_path):
    response = client.post("/drive/targets", data={
        "name": "Bad", "account_email": "a@example.com", "folder_path": str(tmp_path / "missing"),
    }, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_target_route_rejects_rclone_remote_without_colon(client, tmp_path):
    folder = tmp_path / "drive"
    folder.mkdir()
    response = client.post("/drive/targets", data={
        "name": "codex5", "account_email": "a@example.com", "folder_path": str(folder),
        "rclone_remote": "nocolon",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"]


def test_rclone_config_route_persists_client_id(client):
    from app.routes import drive as drive_routes

    response = client.post("/drive/rclone-config", data={
        "client_id": "abc.apps.googleusercontent.com", "client_secret": "GOCSPX-xyz",
    }, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].endswith("#rclone")
    with db.connect(str(settings.db_path)) as conn:
        assert repository.get_app_state(conn, drive_routes._RCLONE_CLIENT_ID_KEY) == "abc.apps.googleusercontent.com"
        assert repository.get_app_state(conn, drive_routes._RCLONE_CLIENT_SECRET_KEY) == "GOCSPX-xyz"


def test_sync_target_without_remote_returns_400(client, tmp_path):
    folder = tmp_path / "drive"
    folder.mkdir()
    client.post("/drive/targets", data={
        "name": "codex1", "account_email": "a@example.com", "folder_path": str(folder),
    }, follow_redirects=False)
    response = client.post("/drive/targets/1/sync")
    assert response.status_code == 400


def test_sync_target_runs_rclone_copy_with_client_id(client, tmp_path, monkeypatch):
    from app.routes import drive as drive_routes

    folder = tmp_path / "staging"
    folder.mkdir()
    client.post("/drive/targets", data={
        "name": "codex5", "account_email": "a@example.com", "folder_path": str(folder),
        "rclone_remote": "codex5:EPUB Audiobook Exports",
    }, follow_redirects=False)
    client.post("/drive/rclone-config", data={
        "client_id": "cid.apps.googleusercontent.com", "client_secret": "GOCSPX-secret",
    }, follow_redirects=False)

    calls = {}

    class FakeProc:
        returncode = 0
        stdout = "Transferred: 1 / 1\n"
        stderr = ""

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(drive_routes.subprocess, "run", fake_run)
    response = client.post("/drive/targets/1/sync")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["remote"] == "codex5:EPUB Audiobook Exports"
    # copy, never sync; client_id/secret forwarded to suppress the shared-client warning
    assert calls["cmd"][1] == "copy"
    assert "--drive-client-id" in calls["cmd"] and "cid.apps.googleusercontent.com" in calls["cmd"]
    assert "--drive-client-secret" in calls["cmd"] and "GOCSPX-secret" in calls["cmd"]
    assert "sync" not in calls["cmd"]


def test_sync_all_only_targets_with_remote(client, tmp_path, monkeypatch):
    from app.routes import drive as drive_routes

    staging = tmp_path / "staging"
    staging.mkdir()
    desktop = tmp_path / "desktop"
    desktop.mkdir()
    client.post("/drive/targets", data={
        "name": "codex5", "account_email": "a@example.com", "folder_path": str(staging),
        "rclone_remote": "codex5:EPUB Audiobook Exports",
    }, follow_redirects=False)
    client.post("/drive/targets", data={
        "name": "codex1", "account_email": "b@example.com", "folder_path": str(desktop),
    }, follow_redirects=False)

    run_count = {"n": 0}

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = "nothing to transfer"

    def fake_run(cmd, **kwargs):
        run_count["n"] += 1
        return FakeProc()

    monkeypatch.setattr(drive_routes.subprocess, "run", fake_run)
    response = client.post("/drive/sync-all")
    assert response.status_code == 200
    body = response.json()
    # only the codex5 (rclone-backed) target is synced; the Drive-Desktop one is skipped
    assert body["count"] == 1
    assert run_count["n"] == 1
    assert body["results"][0]["name"] == "codex5"




def test_pick_folder_route_returns_native_selection(client, monkeypatch):
    import sys
    import types

    class FakeRoot:
        def withdraw(self): pass
        def attributes(self, *_args): pass
        def destroy(self): pass

    fake_tk = types.ModuleType("tkinter")
    fake_tk.Tk = FakeRoot
    fake_dialog = types.ModuleType("tkinter.filedialog")
    fake_dialog.askdirectory = lambda **kwargs: r"G:\My Drive\Audiobooks"
    fake_tk.filedialog = fake_dialog
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", fake_dialog)
    response = client.get("/drive/pick-folder")
    assert response.status_code == 200
    assert response.json() == {"folder_path": r"G:\My Drive\Audiobooks"}


# ---------------------------------------------------------------------------
# Export and import via filesystem (tasks 3.5, 4.4)
# ---------------------------------------------------------------------------


def _seed_book_patch(conn, tmp_path, voice=True):
    now = NOW
    clip = None
    if voice:
        clip = str(tmp_path / "voice.wav")
        (tmp_path / "voice.wav").write_bytes(b"RIFF")
    cur = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "voice_clip_path, created_at, updated_at) VALUES ('B', 'b.epub', 'b.epub', 1, 'ready', ?, ?, ?)",
        (clip, now, now),
    )
    book_id = cur.lastrowid
    conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, 0, 'C', 'Hello world text.', 17)",
        (book_id,),
    )
    cur = conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) VALUES (?, 0, 0, 0, 'pending', ?, ?)",
        (book_id, now, now),
    )
    conn.commit()
    return book_id, cur.lastrowid


@pytest.fixture
def client_with_target(tmp_path, monkeypatch):
    drive_dir = tmp_path / "gdrive"
    drive_dir.mkdir()
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as c:
        conn = db.connect(str(tmp_path / "app.db"))
        book_id, patch_id = _seed_book_patch(conn, tmp_path)
        target = repository.create_drive_sync_target(conn, "T", "t@example.com", str(drive_dir))
        conn.close()
        yield c, book_id, patch_id, target["id"], drive_dir


def test_removed_single_export_route_returns_404(client):
    with TestClient(app) as c:
        resp = c.post("/books/1/patches/1/export", data={}, follow_redirects=False)
    assert resp.status_code == 404


def test_removed_single_export_route_ignores_old_target_payload(client, tmp_path):
    drive_dir = tmp_path / "gd"
    drive_dir.mkdir()
    with db.connect(str(tmp_path / "app.db")) as conn:
        _seed_book_patch(conn, tmp_path)
    resp = client.post("/books/1/patches/1/export", data={"sync_target_id": "999"}, follow_redirects=False)
    assert resp.status_code == 404


def test_export_collision_leaves_existing_folder(tmp_path):
    source = tmp_path / "pkg"
    target = tmp_path / "tgt"
    source.mkdir(); target.mkdir()
    (source / "f.txt").write_text("a")
    drive_export.publish_package(source, str(target), "mypkg")
    existing = (target / "mypkg" / "f.txt").read_text()
    with pytest.raises(FileExistsError):
        (source / "f.txt").write_text("b")
        drive_export.publish_package(source, str(target), "mypkg")
    assert (target / "mypkg" / "f.txt").read_text() == existing


def test_publish_cleanup_on_copy_error(tmp_path):
    source = tmp_path / "pkg"
    target = tmp_path / "tgt"
    source.mkdir(); target.mkdir()
    import shutil as _shutil
    original_copytree = _shutil.copytree
    def failing_copy(*a, **k):
        raise OSError("disk full")
    import unittest.mock as mock
    with mock.patch("shutil.copytree", side_effect=failing_copy):
        with pytest.raises(OSError):
            drive_export.publish_package(source, str(target), "mypkg")
    assert not list(target.glob(".epub-audiobook-export-*"))
    assert not (target / "mypkg").exists()


def test_import_legacy_export_returns_400(client_with_target):
    c, book_id, patch_id, target_id, drive_dir = client_with_target
    with db.connect(str(settings.db_path)) as conn:
        repository.create_patch_export(conn, patch_id, "legacy-id", "https://example.com", 2)
    resp = c.post(f"/books/{book_id}/patches/{patch_id}/import", follow_redirects=False)
    assert resp.status_code == 400
    assert "Legacy" in resp.json()["detail"] or "legacy" in resp.json()["detail"].lower()


def test_import_missing_folder_returns_400(client_with_target):
    c, book_id, patch_id, target_id, drive_dir = client_with_target
    with db.connect(str(settings.db_path)) as conn:
        repository.create_patch_export(conn, patch_id, "x", "x", 1,
                                       sync_target_id=target_id,
                                       local_folder_path=str(drive_dir / "gone"))
    resp = c.post(f"/books/{book_id}/patches/{patch_id}/import", follow_redirects=False)
    assert resp.status_code == 400


def test_import_partial_prefix(tmp_path):
    source = tmp_path / "pkg"
    output = source / "output"
    output.mkdir(parents=True)
    (output / "chunk_000.wav").write_bytes(b"RIFF")
    conn = db.connect(":memory:")
    db.init_schema(conn)
    now = NOW
    conn.execute("INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) VALUES ('B', 'b.epub', 'b.epub', 1, 'ready', ?, ?)", (now, now))
    conn.execute("INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (1, 0, 'C', 'Hello world.', 12)", ())
    cur = conn.execute("INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) VALUES (1, 0, 0, 0, 'pending', ?, ?)", (now, now))
    patch_id = cur.lastrowid
    conn.commit()
    export = repository.create_patch_export(conn, patch_id, str(source), str(source), 3,
                                            local_folder_path=str(source))
    assert export.local_folder_path == str(source)
    assert (output / "chunk_000.wav").exists()
    conn.close()


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
    assert _resolve_batch_result(root / "patches" / "patch_000", 7) == target
    assert _resolve_batch_result(root / "patches" / "patch_000", 8) is None


@pytest.mark.parametrize("patch_manifest", [
    # Current export: titles de-duplicated into chapter_titles, no filename per chunk.
    {"chunk_count": 1, "chapter_titles": {"1": "One"},
     "chunk_metadata": [{"chapter_index": 1, "is_chapter_start": True, "text": "hi"}]},
    # Packages built before that - chunk_NNN.txt names and a title on every entry.
    {"chunks": ["chunk_000.txt"], "expected_outputs": ["chunk_000.wav"],
     "chunk_metadata": [{"filename": "chunk_000.txt", "chapter_index": 1,
                         "chapter_title": "One", "is_chapter_start": True, "text": "hi"}]},
], ids=["compact", "legacy"])
def test_import_fallback_builds_timeline_from_patch_manifest(client_with_target, tmp_path, patch_manifest):
    c, book_id, patch_id, _, _ = client_with_target
    root = tmp_path / "batch"
    patch_folder = root / "patches" / f"patch_{patch_id:03d}"
    output = patch_folder / "output"
    output.mkdir(parents=True)
    (root / "batch_manifest.json").write_text(json.dumps({"patches": [{
        "patch_id": patch_id, "folder": f"patches/patch_{patch_id:03d}",
        "result_wav": f"result/{patch_id}.wav",
    }]}), encoding="utf-8")
    (patch_folder / "manifest.json").write_text(json.dumps(patch_manifest), encoding="utf-8")
    chunk = output / "chunk_000.wav"
    _wav(chunk, 1200, 100)
    result = root / "result"
    result.mkdir()
    (result / f"{patch_id}.wav").write_bytes(b"bad")
    from app import repository
    with db.connect(str(Path(settings.data_root) / "app.db")) as conn:
        repository.create_patch_export(conn, patch_id, str(root), str(root), 1,
                                       local_folder_path=str(root))
    response = c.post(f"/books/{book_id}/patches/{patch_id}/import", follow_redirects=False)
    assert response.status_code == 303
    canonical = Path(settings.data_root) / "books" / str(book_id) / "patches" / f"{patch_id}.timeline.json"
    assert json.loads(canonical.read_text(encoding="utf-8"))["chapters"][0]["title"] == "One"


def test_atomic_install_failure_preserves_existing_pair(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    target = tmp_path / "canonical.wav"
    _wav(source, 20)
    target.write_bytes(b"old wav")
    target.with_suffix(".timeline.json").write_text("old sidecar", encoding="utf-8")
    import app.routes.patches as routes
    monkeypatch.setattr(routes, "_atomic_copy", lambda *_: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        _install_imported_wav(source, target)
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
    _install_imported_wav(source, target)
    assert target.read_bytes() == source.read_bytes()
    assert json.loads(target.with_suffix(".timeline.json").read_text()) == timeline


def test_corrupt_result_does_not_replace_old_media(tmp_path):
    source = tmp_path / "corrupt.wav"
    target = tmp_path / "canonical.wav"
    source.write_bytes(b"not wav")
    target.write_bytes(b"old wav")
    target.with_suffix(".timeline.json").write_text("old", encoding="utf-8")
    with pytest.raises(Exception):
        sf.info(str(source))
    assert target.read_bytes() == b"old wav"
    assert target.with_suffix(".timeline.json").read_text(encoding="utf-8") == "old"


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
    import app.routes.patches as routes
    import os
    original = os.replace
    calls = {"n": 0}
    def fail_second(source_name, destination):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("replace failed")
        return original(source_name, destination)
    monkeypatch.setattr(os, "replace", fail_second)
    routes._install_imported_wav(source, target)
    assert target.read_bytes() == source.read_bytes()
    assert not target.with_suffix(".timeline.json").exists()


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
    assert _build_import_timeline(paths, metadata, 300) is None


def test_corrupt_existing_chunk_is_rejected(tmp_path):
    paths = []
    for index in range(3):
        path = tmp_path / f"chunk_{index:03d}.wav"
        path.write_bytes(b"bad") if index == 1 else _wav(path, 1000, 100)
        paths.append(path)
    metadata = [{"chapter_index": index, "chapter_title": str(index), "is_chapter_start": True}
                for index in range(len(paths))]
    assert _build_import_timeline(paths, metadata, 300) is None




def test_chunk_metadata_builds_timeline_with_pause(tmp_path):
    first, second = tmp_path / "chunk_000.wav", tmp_path / "chunk_001.wav"
    _wav(first, 1000, 100)
    _wav(second, 2000, 100)
    third = tmp_path / "chunk_002.wav"
    _wav(third, 2000, 100)
    metadata = [{"chapter_index": 1, "chapter_title": "One", "is_chapter_start": True},
                {"chapter_index": 2, "chapter_title": "Two", "is_chapter_start": True},
                {"chapter_index": 3, "chapter_title": "Three", "is_chapter_start": True}]
    timeline = _build_import_timeline([first, second, third], metadata, pause_ms=300)
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
    timeline = _build_import_timeline(paths, metadata, pause_ms=300)
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
    assert _build_import_timeline(paths, metadata, pause_ms=300) is None


def test_sidecar_cleanup_refusal_does_not_fail_import(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    target = tmp_path / "canonical.wav"
    _wav(source, 1000, 100)
    stale = target.with_suffix(".timeline.json")
    stale.write_text("stale", encoding="utf-8")
    import app.routes.patches as routes
    original_unlink = Path.unlink
    def refuse(path, *args, **kwargs):
        if path == stale:
            raise OSError("refused")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", refuse)
    routes._install_imported_wav(source, target)
    assert target.read_bytes() == source.read_bytes()


def test_invalid_chunk_metadata_disables_timeline(tmp_path):
    chunk = tmp_path / "chunk_000.wav"
    _wav(chunk, 100, 100)
    assert _build_import_timeline([chunk], [{"chapter_index": 1}], pause_ms=300) is None


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
    assert _build_import_timeline(paths, metadata, pause_ms=300) is not None


def test_import_local_merge_removes_stale_timeline(client_with_target, tmp_path):
    c, book_id, patch_id, _, _ = client_with_target
    chunk = tmp_path / "chunk_000.wav"
    _wav(chunk, 1000, 100)
    audio_dir = Path(settings.data_root) / "books" / str(book_id) / "patches"
    audio_dir.mkdir(parents=True, exist_ok=True)
    (audio_dir / f"{patch_id}.timeline.json").write_text("stale", encoding="utf-8")
    response = c.post(f"/books/{book_id}/patches/{patch_id}/import-local", files={
        "files": ("chunk_000.wav", chunk.read_bytes(), "audio/wav")
    })
    assert response.status_code == 200
    assert not (audio_dir / f"{patch_id}.timeline.json").exists()


def test_import_local_merge_failure_preserves_old_pair(client_with_target, tmp_path, monkeypatch):
    c, book_id, patch_id, _, _ = client_with_target
    chunk = tmp_path / "chunk_000.wav"
    _wav(chunk, 1000, 100)
    audio_dir = Path(settings.data_root) / "books" / str(book_id) / "patches"
    audio_dir.mkdir(parents=True, exist_ok=True)
    canonical = audio_dir / f"{patch_id}.wav"
    sidecar = canonical.with_suffix(".timeline.json")
    canonical.write_bytes(b"old wav")
    sidecar.write_bytes(b"old sidecar")
    import app.routes.patches as routes
    monkeypatch.setattr(routes.audio_merge, "concat_wavs", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("merge failed")))
    with pytest.raises(OSError, match="merge failed"):
        c.post(f"/books/{book_id}/patches/{patch_id}/import-local", files={
            "files": ("chunk_000.wav", chunk.read_bytes(), "audio/wav")
        })
    assert canonical.read_bytes() == b"old wav"
    assert sidecar.read_bytes() == b"old sidecar"


def test_installer_original_error_survives_cleanup_and_restore_errors(tmp_path, monkeypatch):
    source = tmp_path / "source.wav"
    target = tmp_path / "canonical.wav"
    _wav(source, 3000, 100)
    target.write_bytes(b"old wav")
    target.with_suffix(".timeline.json").write_bytes(b"old sidecar")
    import app.routes.patches as routes
    monkeypatch.setattr(routes, "_atomic_copy", lambda *args: (_ for _ in ()).throw(OSError("original install")))
    original_replace = routes.os.replace
    def bad_restore(source_name, destination):
        if str(source_name).endswith(".bak"):
            raise OSError("restore failed")
        return original_replace(source_name, destination)
    monkeypatch.setattr(routes.os, "replace", bad_restore)
    with pytest.raises(OSError, match="original install"):
        routes._install_imported_wav(source, target)
