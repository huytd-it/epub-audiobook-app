"""Result inbox: local folder as the upload channel, with archive + reason files."""
import json
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app

NOW = datetime.now(timezone.utc).isoformat()
RATE = 100
FRAMES = 1200


def _wav_bytes(frames=FRAMES):
    buf = BytesIO()
    sf.write(buf, np.zeros(frames), RATE, format="WAV")
    return buf.getvalue()


def _timeline(frames=FRAMES) -> bytes:
    return json.dumps({
        "version": 1, "sample_rate": RATE, "total_frames": frames,
        "chapters": [{"chapter_index": 1, "title": "C", "start_frame": 0, "start_seconds": 0.0}],
    }).encode("utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as c:
        conn = db.connect(str(tmp_path / "app.db"))
        book_id = conn.execute(
            "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
            "VALUES ('B', 'b.epub', 'b.epub', 1, 'ready', ?, ?)", (NOW, NOW),
        ).lastrowid
        conn.execute(
            "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) "
            "VALUES (?, 0, 'C', 'Hello world text.', 17)", (book_id,),
        )
        for patch_id, index in ((41, 0), (42, 1)):
            conn.execute(
                "INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 0, 0, 'pending', ?, ?)", (patch_id, book_id, index, NOW, NOW),
            )
        conn.commit()
        conn.close()
        yield c, book_id


def _inbox(book_id) -> Path:
    folder = Path(settings.data_root) / "books" / str(book_id)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _install(book_id, *names):
    folder = _inbox(book_id)
    for name in names:
        payload = _timeline() if name.lower().endswith(".timeline.json") else _wav_bytes()
        (folder / name).write_bytes(payload)
    return folder


def _patch_audio(book_id, patch_id) -> Path:
    return Path(settings.data_root) / "books" / str(book_id) / "patches" / f"{patch_id}.wav"


def test_inbox_status_lists_files_archives_and_patch_states(client):
    c, book_id = client
    folder = _inbox(book_id)
    (folder / "000 - ready.wav").write_bytes(_wav_bytes(frames=FRAMES * 2))
    (folder / "000 - ready.timeline.json").write_bytes(_timeline(frames=FRAMES * 2))
    (folder / "processed" / "old.wav").parent.mkdir(exist_ok=True)
    (folder / "processed" / "old.wav").write_bytes(b"x")
    (folder / "rejected" / "bad.wav").parent.mkdir(exist_ok=True)
    (folder / "rejected" / "bad.wav").write_bytes(b"x")

    body = c.get(f"/books/{book_id}/patches/result-inbox").json()
    assert body["count"] == 2
    assert body["files"] == ["000 - ready.timeline.json", "000 - ready.wav"]
    assert body["processed"] == ["old.wav"]
    assert body["rejected"] == ["bad.wav"]
    assert str(folder.resolve()) in body["path"]
    assert body["patches"] == {}


def test_inbox_status_unknown_book(client):
    assert client[0].get("/books/9999/patches/result-inbox").status_code == 404


def test_open_inbox_opens_native_folder(client, monkeypatch):
    c, book_id = client
    opened = []
    monkeypatch.setattr("app.routes.patches.sys.platform", "win32")
    monkeypatch.setattr("app.routes.patches.os.startfile", lambda p: opened.append(p))
    body = c.post(f"/books/{book_id}/patches/result-inbox/open").json()
    assert body["ok"] is True
    assert opened == [str(_inbox(book_id).resolve())]


def test_processing_inbox_installs_and_archives_files(client, monkeypatch):
    c, book_id = client
    folder = _install(book_id, "000 - tap1.wav", "000 - tap1.timeline.json")
    monkeypatch.setattr("app.routes.patches._warm_thumbnail", lambda request, patch_id: None)

    body = c.post(f"/books/{book_id}/patches/result-inbox/process").json()
    assert body["installed"] == 1
    assert body["results"][0]["status"] == "ok"
    assert sorted(body["renamed"], key=lambda r: r["to"]) == [
        {"from": "000 - tap1.timeline.json", "to": f"{book_id}_001.timeline.json"},
        {"from": "000 - tap1.wav", "to": f"{book_id}_001.wav"},
    ]
    assert sf.info(str(_patch_audio(book_id, 41))).frames == FRAMES
    assert (folder / "processed").is_dir()
    archived_names = {row["from"] for row in body["archived"]["processed"]}
    assert archived_names == {f"{book_id}_001.wav", f"{book_id}_001.timeline.json"}
    assert body["archived"]["rejected"] == []
    assert not (folder / f"{book_id}_001.wav").exists()


def test_processing_moves_bad_files_to_rejected_with_reason(client, monkeypatch):
    c, book_id = client
    folder = _inbox(book_id)
    (folder / "000 - broken.wav").write_bytes(b"not a wav at all")
    monkeypatch.setattr("app.routes.patches._warm_thumbnail", lambda request, patch_id: None)

    body = c.post(f"/books/{book_id}/patches/result-inbox/process").json()
    row = body["results"][0]
    assert row["status"] == "error"
    assert "WAV không hợp lệ" in row["detail"]
    assert len(body["archived"]["rejected"]) == 1
    rejected = folder / "rejected"
    archived = list(rejected.iterdir())
    assert len(archived) == 2
    reason_files = [p for p in archived if p.name.endswith(".reason.txt")]
    assert len(reason_files) == 1
    assert "WAV không hợp lệ" in reason_files[0].read_text(encoding="utf-8")
    assert not _patch_audio(book_id, 41).exists()


def test_processing_never_overwrites_a_colliding_target(client, monkeypatch):
    c, book_id = client
    folder = _inbox(book_id)
    (folder / f"{book_id}_001.wav").write_bytes(_wav_bytes(frames=FRAMES * 4))
    (folder / "000 - legacy.wav").write_bytes(_wav_bytes(frames=FRAMES))
    monkeypatch.setattr("app.routes.patches._warm_thumbnail", lambda request, patch_id: None)

    body = c.post(f"/books/{book_id}/patches/result-inbox/process").json()
    assert body["installed"] == 1
    renamed = [r for r in body["renamed"] if r["to"] == f"{book_id}_001.wav"]
    assert renamed == []
    statuses = sorted(r["status"] for r in body["results"])
    assert statuses == ["ok", "skipped"]
    skipped_detail = next(r["detail"] for r in body["results"] if r["status"] == "skipped")
    assert "trùng" in skipped_detail
    assert sf.info(str(_patch_audio(book_id, 41))).frames in (FRAMES, FRAMES * 4)


def test_processing_is_idempotent_across_calls(client, monkeypatch):
    c, book_id = client
    _install(book_id, "000 - tap1.wav")
    monkeypatch.setattr("app.routes.patches._warm_thumbnail", lambda request, patch_id: None)

    first = c.post(f"/books/{book_id}/patches/result-inbox/process").json()
    second = c.post(f"/books/{book_id}/patches/result-inbox/process").json()
    assert first["installed"] == 1
    assert second["installed"] == 0
    assert second["results"] == []
    assert sf.info(str(_patch_audio(book_id, 41))).frames == FRAMES


def test_processing_rejects_unknown_book(client):
    assert client[0].post("/books/9999/patches/result-inbox/process").status_code == 404