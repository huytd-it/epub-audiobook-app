"""Route tests for the voice manager (/voices)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    with TestClient(app) as c:
        yield c


def _voices_dir(client) -> Path:
    from app.config import settings

    d = Path(settings.data_root) / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db(client) -> sqlite3.Connection:
    from app.config import settings

    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    return c


def _seed_voice(client, name: str) -> Path:
    p = _voices_dir(client) / name
    p.write_bytes(b"RIFFfakewav")
    return p


def _seed_book_with_voice(client, voice_path: Path) -> None:
    db = _db(client)
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "voice_clip_path, created_at, updated_at) "
        "VALUES ('b', 'b.epub', 'b.epub', 10, 'ready', ?, ?, ?)",
        (str(voice_path), now, now),
    )
    db.commit()
    db.close()




def test_voice_upload_creates_file(client, tmp_path):
    clip = tmp_path / "up.wav"
    clip.write_bytes(b"RIFFfakewav")
    with open(clip, "rb") as f:
        resp = client.post(
            "/voices/upload",
            files={"files": ("up.wav", f, "audio/wav")},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    names = [p.name for p in _voices_dir(client).iterdir()]
    assert any(n.endswith("up.wav") for n in names)


def test_voice_upload_skips_bad_extension(client, tmp_path):
    bad = tmp_path / "evil.exe"
    bad.write_bytes(b"MZ")
    with open(bad, "rb") as f:
        resp = client.post(
            "/voices/upload",
            files={"files": ("evil.exe", f, "application/octet-stream")},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    names = [p.name for p in _voices_dir(client).iterdir()]
    assert not any(n.endswith("evil.exe") for n in names)


def test_voice_upload_multiple_files(client, tmp_path):
    clip1 = tmp_path / "up1.wav"
    clip1.write_bytes(b"RIFFfakewav1")
    clip2 = tmp_path / "up2.mp3"
    clip2.write_bytes(b"ID3fakemp3")
    with open(clip1, "rb") as f1, open(clip2, "rb") as f2:
        resp = client.post(
            "/voices/upload",
            files=[
                ("files", ("up1.wav", f1, "audio/wav")),
                ("files", ("up2.mp3", f2, "audio/mpeg")),
            ],
            follow_redirects=False,
        )
    assert resp.status_code == 303
    names = [p.name for p in _voices_dir(client).iterdir()]
    assert any(n.endswith("up1.wav") for n in names)
    assert any(n.endswith("up2.mp3") for n in names)


def test_voice_rename_renames_file_and_book_reference(client):
    old = _seed_voice(client, "old.wav")
    _seed_book_with_voice(client, old)

    resp = client.post(
        "/voices/rename",
        data={"old_name": "old.wav", "new_name": "narrator"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert not old.exists()
    new = _voices_dir(client) / "narrator.wav"
    assert new.exists()

    db = _db(client)
    row = db.execute("SELECT voice_clip_path FROM book").fetchone()
    assert row["voice_clip_path"] == str(new)
    db.close()


def test_voice_rename_follows_the_audio_settings_voice_id(client):
    """The voice id of a cloning model IS this filename, so a rename that left it behind
    would fail the next TTS job with "unknown reference voice"."""
    _seed_voice(client, "old.wav")
    db = _db(client)
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "tts_voice_id, export_tts_voice_id, created_at, updated_at) "
        "VALUES ('b', 'b.epub', 'b.epub', 10, 'ready', 'old.wav', 'old.wav', ?, ?)",
        (now, now),
    )
    db.commit()
    db.close()
    client.post("/production-settings", json={"audio": {"model_id": "voxcpm2", "voice_id": "old.wav"}})

    resp = client.post(
        "/voices/rename",
        data={"old_name": "old.wav", "new_name": "narrator"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    db = _db(client)
    row = db.execute("SELECT tts_voice_id, export_tts_voice_id FROM book").fetchone()
    assert (row["tts_voice_id"], row["export_tts_voice_id"]) == ("narrator.wav", "narrator.wav")
    db.close()
    defaults = client.get("/production-settings").json()["defaults"]
    assert defaults["audio"]["voice_id"] == "narrator.wav"


def test_voice_delete_clears_the_audio_settings_voice_id(client):
    p = _seed_voice(client, "gone.wav")
    db = _db(client)
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "tts_voice_id, created_at, updated_at) "
        "VALUES ('b', 'b.epub', 'b.epub', 10, 'ready', 'gone.wav', ?, ?)",
        (now, now),
    )
    db.commit()
    db.close()
    client.post("/production-settings", json={"audio": {"model_id": "voxcpm2", "voice_id": "gone.wav"}})

    assert client.post("/voices/delete", data={"name": "gone.wav"},
                       follow_redirects=False).status_code == 303
    assert not p.exists()

    db = _db(client)
    assert db.execute("SELECT tts_voice_id FROM book").fetchone()["tts_voice_id"] is None
    db.close()
    defaults = client.get("/production-settings").json()["defaults"]
    assert defaults["audio"]["voice_id"] == ""


def test_voice_rename_rejects_traversal(client):
    _seed_voice(client, "ok.wav")
    for old_name, new_name in [
        ("../secret.wav", "x"),
        ("ok.wav", "../evil"),
        ("ok.wav", "a/b"),
        ("ok.wav", ""),
    ]:
        resp = client.post(
            "/voices/rename",
            data={"old_name": old_name, "new_name": new_name},
            follow_redirects=False,
        )
        assert resp.status_code == 400, (old_name, new_name)


def test_voice_rename_missing_file_404(client):
    resp = client.post(
        "/voices/rename",
        data={"old_name": "nope.wav", "new_name": "x"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_voice_rename_refuses_overwrite(client):
    _seed_voice(client, "a.wav")
    _seed_voice(client, "b.wav")
    resp = client.post(
        "/voices/rename",
        data={"old_name": "a.wav", "new_name": "b"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_voice_delete_removes_file_and_clears_book_reference(client):
    p = _seed_voice(client, "gone.wav")
    _seed_book_with_voice(client, p)

    resp = client.post(
        "/voices/delete", data={"name": "gone.wav"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert not p.exists()
    db = _db(client)
    row = db.execute("SELECT voice_clip_path FROM book").fetchone()
    assert row["voice_clip_path"] is None
    db.close()


def test_voice_file_served(client):
    _seed_voice(client, "clip.wav")
    resp = client.get("/voices/file/clip.wav")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
