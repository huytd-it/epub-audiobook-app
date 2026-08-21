"""Route tests for the music editor (/music/{id}/info and /process).

Mirrors tests/test_voices_classify_process.py: the validation and bookkeeping
tests always run, the ones that actually render audio are skipped without
ffmpeg.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings
    from app.routes import music as music_routes

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    # _MUSIC_DIR is computed from settings at import time - repoint it so the
    # whitelist and the copy destination both land in the test dir.
    monkeypatch.setattr(music_routes, "_MUSIC_DIR", tmp_path / "music")
    (tmp_path / "music").mkdir(parents=True, exist_ok=True)
    with TestClient(app) as c:
        yield c


def _has_ffmpeg() -> bool:
    from app.config import settings

    return shutil.which(settings.get_ffmpeg_path()) is not None or Path(settings.get_ffmpeg_path()).exists()


needs_ffmpeg = pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg không có trên máy này")


def _music_dir() -> Path:
    from app.routes import music as music_routes

    return music_routes._MUSIC_DIR


def _db():
    from app.config import settings

    conn = sqlite3.connect(settings.db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _make_wav(dest: Path, seconds: float = 4.0) -> Path:
    from app.config import settings

    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [settings.get_ffmpeg_path(), "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"sine=frequency=440:r=44100:duration={seconds}", str(dest)],
        check=True,
    )
    return dest


def _register(client, path: Path, *, name="Track", description="", license="") -> int:
    from app import repository

    conn = client.app.state.conn
    track = repository.create_music(
        conn, name=name, file_path=str(path), duration_sec=None,
        description=description, license=license,
    )
    return track.id


def _seed_fake(client, name="fake.wav", **kwargs) -> int:
    path = _music_dir() / name
    path.write_bytes(b"RIFFfakewav")
    return _register(client, path, **kwargs)


def test_info_missing_track_404(client):
    assert client.get("/music/999/info").status_code == 404


def test_info_missing_file_404(client):
    track_id = _register(client, _music_dir() / "gone.mp3")
    assert client.get(f"/music/{track_id}/info").status_code == 404


def test_a_path_outside_the_library_is_refused(client, tmp_path):
    outside = tmp_path / "elsewhere.wav"
    outside.write_bytes(b"RIFFfakewav")
    track_id = _register(client, outside)
    assert client.get(f"/music/{track_id}/info").status_code == 403


def test_process_rejects_empty_ops(client):
    track_id = _seed_fake(client)
    resp = client.post(f"/music/{track_id}/process", json={"ops": {}})
    assert resp.status_code == 400
    assert "thao tác" in resp.json()["detail"]


@pytest.mark.parametrize("ops", [
    {"trim_start": -1},
    {"trim_start": 2, "trim_end": 1},
    {"fade_in": 999},
    {"gain_db": 60},
    {"sample_rate": 12345},
])
def test_process_rejects_bad_ops(client, ops):
    track_id = _seed_fake(client, name="bad.wav")
    assert client.post(f"/music/{track_id}/process", json={"ops": ops}).status_code == 400


def test_process_of_an_unreadable_file_leaves_the_original(client):
    track_id = _seed_fake(client, name="broken.wav")
    path = _music_dir() / "broken.wav"
    original = path.read_bytes()
    resp = client.post(f"/music/{track_id}/process", json={"ops": {"normalize": True}})
    assert resp.status_code in (400, 500)
    assert path.read_bytes() == original
    assert not any(name.startswith(".") for name in (p.name for p in _music_dir().iterdir()))


@needs_ffmpeg
def test_info_reports_the_probe(client):
    track_id = _register(client, _make_wav(_music_dir() / "a.wav", seconds=3.0), name="Nhạc nền")
    body = client.get(f"/music/{track_id}/info").json()
    assert body["name"] == "Nhạc nền"
    assert body["duration_sec"] == pytest.approx(3.0, abs=0.1)
    assert body["sample_rate"] == 44100
    assert body["size"] > 0


@needs_ffmpeg
def test_overwrite_keeps_the_id_and_refreshes_the_duration(client):
    path = _make_wav(_music_dir() / "b.wav", seconds=4.0)
    track_id = _register(client, path, name="Bản gốc")

    resp = client.post(f"/music/{track_id}/process", json={"ops": {"trim_start": 1.0, "trim_end": 2.0}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] == track_id
    assert body["duration_sec"] == pytest.approx(1.0, abs=0.1)
    # The cached duration on the row follows the file, so the library stops
    # advertising the pre-edit length.
    with _db() as conn:
        row = conn.execute("SELECT duration_sec FROM music WHERE id=?", (track_id,)).fetchone()
    assert row["duration_sec"] == pytest.approx(1.0, abs=0.1)
    # An in-place edit must not litter the library.
    assert [p.name for p in _music_dir().iterdir()] == ["b.wav"]


@needs_ffmpeg
def test_save_as_copy_registers_a_new_track_and_inherits_the_licence(client):
    path = _make_wav(_music_dir() / "c.wav", seconds=4.0)
    track_id = _register(client, path, name="Gốc", description="Nhạc nền YouTube", license="CC BY 4.0")

    resp = client.post(
        f"/music/{track_id}/process",
        json={"ops": {"trim_start": 0.5, "trim_end": 1.5}, "save_as": "copy", "new_name": "Bản ngắn"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["id"] != track_id
    assert body["name"] == "Bản ngắn"
    assert body["license"] == "CC BY 4.0"
    assert body["description"] == "Nhạc nền YouTube"
    assert body["duration_sec"] == pytest.approx(1.0, abs=0.1)

    from app import audio_process

    assert audio_process.probe(path)["duration_sec"] == pytest.approx(4.0, abs=0.1)
    with _db() as conn:
        assert conn.execute("SELECT COUNT(*) c FROM music").fetchone()["c"] == 2


@needs_ffmpeg
def test_repeated_copies_get_unique_filenames(client):
    track_id = _register(client, _make_wav(_music_dir() / "d.wav", seconds=2.0))
    for _ in range(2):
        assert client.post(
            f"/music/{track_id}/process", json={"ops": {"normalize": True}, "save_as": "copy"}
        ).status_code == 200
    names = sorted(p.name for p in _music_dir().iterdir())
    assert names == ["d.wav", "d_edited.wav", "d_edited_1.wav"]


@needs_ffmpeg
def test_process_reports_what_it_applied(client):
    track_id = _register(client, _make_wav(_music_dir() / "e.wav", seconds=2.0))
    applied = client.post(
        f"/music/{track_id}/process", json={"ops": {"fade_in": 0.5, "gain_db": -3}}
    ).json()["applied"]
    assert any("fade in" in item for item in applied)
    assert any("dB" in item for item in applied)


@needs_ffmpeg
def test_a_book_keeps_mixing_the_track_after_an_overwrite(client):
    from app import repository

    path = _make_wav(_music_dir() / "f.wav", seconds=4.0)
    track_id = _register(client, path)
    conn = client.app.state.conn
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size, status,
                             music_id, music_volume, created_at, updated_at)
           VALUES (1, 'Sách', 'a.epub', 'a.epub', 10, 'ready', ?, 0.15, ?, ?)""",
        (track_id, "2026-01-01", "2026-01-01"),
    )
    conn.commit()

    assert client.post(
        f"/music/{track_id}/process", json={"ops": {"gain_db": -2}}
    ).status_code == 200
    book = repository.get_book(conn, 1)
    assert book.music_id == track_id
    assert Path(repository.get_music(conn, track_id).file_path).is_file()
