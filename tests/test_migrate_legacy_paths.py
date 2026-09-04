"""Tests cho scripts/migrate_legacy_paths.py: file moves + remap DB + orphan/conflict/gate."""
import importlib.util
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import db
from app.config import settings

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load_module():
    path = SCRIPTS_DIR / "migrate_legacy_paths.py"
    spec = importlib.util.spec_from_file_location("migrate_legacy_paths", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_legacy_paths"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def mod():
    return _load_module()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _db(tmp_path):
    conn = db.connect(str(tmp_path / "app.db"))
    db.init_schema(conn)
    return conn


def _book_patch(conn, *, book_id=1, patch_id=7, patch_index=2, status="pending"):
    now = _now()
    conn.execute(
        "INSERT INTO book (id, title, original_filename, epub_path, patch_size, status, created_at, updated_at)"
        " VALUES (?, 'B', 'b.epub', 'b.epub', 10, 'ready', ?, ?)",
        (book_id, now, now),
    )
    conn.execute(
        "INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status, audio_path, created_at, updated_at)"
        " VALUES (?, ?, ?, 0, 1, ?, NULL, ?, ?)",
        (patch_id, book_id, patch_index, status, now, now),
    )
    conn.commit()


def test_build_plan_moves_and_remaps_db(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn = _db(tmp_path)
    _book_patch(conn)
    m = mod

    legacy_dir = tmp_path / "books" / "1" / "patches"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "7.wav").write_bytes(b"wav")
    (legacy_dir / "7.ass").write_bytes(b"ass")
    chunks = legacy_dir / "7_chunks"
    chunks.mkdir()
    (chunks / "chunk_000.wav").write_bytes(b"c")
    legacy_videos = tmp_path / "books" / "1" / "patch_videos"
    legacy_videos.mkdir(parents=True)
    (legacy_videos / "7.mp4").write_bytes(b"v")

    old_audio = str(legacy_dir / "7.wav")
    old_video = str(legacy_videos / "7.mp4")
    media = {
        "patch_id": 7,
        "audio_path": old_audio,
        "render_snapshot": {
            "audio_path": old_audio,
            "audio_fingerprint": f"{old_audio}:123:abc",
            "thumbnail_path": str(tmp_path / "books" / "1" / "patch_overlays" / "1_003.png"),
        },
    }
    now = _now()
    conn.execute(
        "INSERT INTO patch_pipeline (patch_id, stage, config_snapshot, media_snapshot, created_at, updated_at)"
        " VALUES (7, 'video', '{}', ?, ?, ?)",
        (json.dumps(media), now, now),
    )
    conn.execute(
        "INSERT INTO videos (filename, file_path, source_audio, created_at, updated_at)"
        " VALUES ('7.mp4', ?, ?, ?, ?)",
        (old_video, old_audio, now, now),
    )
    conn.execute(
        "INSERT INTO job (job_type, status, payload_json, created_at, updated_at)"
        " VALUES ('patch_video', 'done', ?, ?, ?)",
        (json.dumps({"patch_id": 7, "audio_path": old_audio}), now, now),
    )
    conn.commit()

    plan = m.build_plan(conn, book_id=None, relink_audio=False)

    kinds = sorted(move.kind for move in plan.moves)
    assert kinds == ["audio", "audio", "chunks", "video"]
    assert not plan.orphans
    assert not plan.conflicts

    new_audio = str(tmp_path / "books" / "1" / "audio" / "1_003.wav")
    cols = {(e.table, e.column) for e in plan.db_edits}
    assert ("patch_pipeline", "media_snapshot") in cols
    assert ("videos", "file_path") in cols
    assert ("videos", "source_audio") in cols
    assert ("job", "payload_json") in cols

    snap_edit = next(e for e in plan.db_edits
                     if e.table == "patch_pipeline" and e.column == "media_snapshot")
    new_tree = json.loads(snap_edit.new)
    # path đổi, fingerprint giữ size/hash -> so sánh staleness sau migration vẫn đúng
    assert new_tree["audio_path"] == new_audio
    assert new_tree["render_snapshot"]["audio_fingerprint"] == f"{new_audio}:123:abc"
    assert new_tree["render_snapshot"]["thumbnail_path"].endswith("1_003.png")

    # apply: file đi đúng đích, DB trỏ đúng chỗ
    moved = m.apply_moves(plan.moves)
    assert moved == 4
    assert Path(new_audio).is_file()
    assert (tmp_path / "books" / "1" / "audio" / "1_003_chunks" / "chunk_000.wav").is_file()
    assert (tmp_path / "books" / "1" / "videos" / "1_003.mp4").is_file()
    m.apply_db_edits(conn, plan)
    row = conn.execute("SELECT media_snapshot FROM patch_pipeline WHERE patch_id=7").fetchone()
    assert json.loads(row[0])["audio_path"] == new_audio
    vrow = conn.execute("SELECT file_path, filename FROM videos").fetchone()
    assert vrow["file_path"] == str(tmp_path / "books" / "1" / "videos" / "1_003.mp4")
    assert vrow["filename"] == "1_003.mp4"


def test_orphan_when_patch_deleted_and_conflict(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn = _db(tmp_path)
    _book_patch(conn)
    m = mod

    legacy_dir = tmp_path / "books" / "1" / "patches"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "999.wav").write_bytes(b"orphan")
    (legacy_dir / "7.wav").write_bytes(b"live")
    # đích đã tồn tại -> conflict, không ghi đè
    dst = tmp_path / "books" / "1" / "audio" / "1_003.wav"
    dst.parent.mkdir(parents=True)
    dst.write_bytes(b"existing")

    plan = m.build_plan(conn, book_id=None, relink_audio=False)
    assert [(str(o.path), o.reason) for o in plan.orphans] == [
        (str(legacy_dir / "999.wav"), "patch row đã bị xoá")
    ]
    assert plan.conflicts == [(legacy_dir / "7.wav", dst)]
    assert not [mv for mv in plan.moves if mv.src.name == "7.wav"]


def test_active_jobs_block_apply(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(settings, "db_path", str(db_path))
    conn = _db(tmp_path)
    _book_patch(conn)
    legacy_dir = tmp_path / "books" / "1" / "patches"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "7.wav").write_bytes(b"wav")
    now = _now()
    conn.execute(
        "INSERT INTO job (job_type, status, payload_json, book_id, patch_id, created_at, updated_at)"
        " VALUES ('audiobook_tts', 'running', '{}', 1, 7, ?, ?)",
        (now, now),
    )
    conn.commit()
    conn.close()

    assert mod.main(["--apply"]) == 1


def test_dry_run_writes_nothing(mod, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    db_path = tmp_path / "app.db"
    monkeypatch.setattr(settings, "db_path", str(db_path))
    conn = _db(tmp_path)
    _book_patch(conn)
    legacy_dir = tmp_path / "books" / "1" / "patches"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "7.wav").write_bytes(b"wav")
    conn.close()

    assert mod.main([]) == 0
    out = capsys.readouterr().out
    assert "[dry-run]" in out
    # file còn nguyên, DB chưa backup
    assert (legacy_dir / "7.wav").is_file()
    assert list(tmp_path.glob("app.bak_*")) == []


def test_archive_orphan_updates_videos_row(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn = _db(tmp_path)
    _book_patch(conn)
    m = mod

    legacy_videos = tmp_path / "books" / "1" / "patch_videos"
    legacy_videos.mkdir(parents=True)
    orphan = legacy_videos / "555.mp4"
    orphan.write_bytes(b"v")
    now = _now()
    conn.execute(
        "INSERT INTO videos (filename, file_path, created_at, updated_at) VALUES ('555.mp4', ?, ?, ?)",
        (str(orphan), now, now),
    )
    conn.commit()

    plan = m.build_plan(conn, book_id=None, relink_audio=False)
    assert len(plan.orphans) == 1
    archived = m.apply_orphan_archive(conn, plan)
    assert archived == 1
    dst = tmp_path / "books" / "1" / "_legacy_archive" / "patch_videos" / "555.mp4"
    assert dst.is_file()
    vrow = conn.execute("SELECT file_path, filename FROM videos").fetchone()
    assert vrow["file_path"] == str(dst)
    assert vrow["filename"] == "555.mp4"


def test_video_link_recovered_from_new_path(mod, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn = _db(tmp_path)
    _book_patch(conn)
    m = mod

    new_video = tmp_path / "books" / "1" / "videos" / "1_003.mp4"
    new_video.parent.mkdir(parents=True)
    new_video.write_bytes(b"v")
    now = _now()
    conn.execute(
        "INSERT INTO videos (filename, file_path, patch_id, created_at, updated_at)"
        " VALUES ('1_003.mp4', ?, NULL, ?, ?)",
        (str(new_video), now, now),
    )
    conn.commit()

    plan = m.build_plan(conn, book_id=None, relink_audio=False)
    assert plan.video_links == [(1, 7)]
