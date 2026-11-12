"""Nạp thư mục chunk theo chương: định dạng đường dẫn, cài vào kho, ghép patch từ kho."""
from __future__ import annotations

import io
from contextlib import nullcontext

import numpy as np
import pytest
import soundfile as sf

from app import chapter_chunk_upload as upload, chunk_store, repository
from app.config import settings
from app.production_defaults import get_effective_normalization_options

from test_drive_export import conn  # noqa: F401 - shared book/patch fixture

RATE = 24000
LIMIT = 18


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "data"))
    return tmp_path / "data"


def _wav(rate=RATE, stereo=False) -> io.BytesIO:
    tone = 0.2 * np.sin(np.linspace(0, 440 * 2 * np.pi, rate // 4)).astype(np.float32)
    if stereo:
        tone = np.stack([tone, tone], axis=1)
    buffer = io.BytesIO()
    sf.write(buffer, tone, rate, format="WAV")
    buffer.seek(0)
    return buffer


def _inputs(connection, book, chapter_index):
    chapter = repository.get_chapter(connection, book.id, chapter_index)
    opts = get_effective_normalization_options(connection, book)
    rules = repository.list_replace_rules(connection, book.id)
    return chapter, opts, rules


@pytest.mark.parametrize("name, expected", [
    ("root/0001/chunk_000.wav", (0, 0)),
    ("0012 - Chương 12/chunk_007.WAV", (11, 7)),
    (r"a\b\3\5.wav", (2, 5)),
])
def test_parse_upload_path_accepts_the_documented_layout(name, expected):
    assert upload.parse_upload_path(name) == expected


@pytest.mark.parametrize("name", ["chunk_000.wav", "root/intro/chunk_000.wav", "root/0001/chunk_a.wav",
                                  "root/0000/chunk_000.wav", "root/0001/chunk_000.mp3"])
def test_parse_upload_path_rejects_other_layouts(name):
    assert isinstance(upload.parse_upload_path(name), str)


def test_full_chapter_upload_completes_the_store_and_merges_the_patch(conn, data_root, monkeypatch):
    connection, book, patch = conn
    # Loudness normalization shells out to ffmpeg; the merge itself is what is tested here.
    from app.jobqueue.handlers import audiobook_tts
    monkeypatch.setattr(audiobook_tts, "normalize_wav_in_place", lambda path: None)
    reports = []
    for chapter_index in (0, 1):
        chapter, opts, rules = _inputs(connection, book, chapter_index)
        count = len(upload.chapter_plan(chapter, opts, rules, LIMIT))
        files = {i: (f"x/{chapter_index + 1:04d}/chunk_{i:03d}.wav", _wav(stereo=i == 0)) for i in range(count)}
        reports.append(upload.install_chapter_chunks(book.id, chapter, opts, rules, LIMIT, files, replace=False))
    assert all(r["status"] == "ok" and r["complete"] for r in reports)
    store = chunk_store.ChunkStore(chunk_store.get_chapter_chunk_dir(book.id, 0))
    assert store.manifest()["engine"] == upload.UPLOAD_ENGINE
    assert store.is_complete(repository.chapter_tts_source_key(
        *_inputs(connection, book, 0), LIMIT))

    audio_path, total = upload.assemble_patch_from_stores(lambda: nullcontext(connection), patch, LIMIT)
    assert total == sum(r["expected"] for r in reports)
    assert sf.info(audio_path).frames > total * (RATE // 4)


def test_partial_upload_reports_missing_and_refuses_the_merge(conn, data_root):
    connection, book, patch = conn
    chapter, opts, rules = _inputs(connection, book, 0)
    count = len(upload.chapter_plan(chapter, opts, rules, LIMIT))
    assert count > 1
    files = {0: ("x/0001/chunk_000.wav", _wav()), count + 3: ("x/0001/chunk_099.wav", _wav())}
    report = upload.install_chapter_chunks(book.id, chapter, opts, rules, LIMIT, files, replace=False)
    assert report["status"] == "partial"
    assert report["installed"] == 1
    assert report["missing"] == list(range(1, count))
    assert report["rejected"][0]["filename"] == "x/0001/chunk_099.wav"

    with pytest.raises(upload.ChapterChunkUploadError, match="thiếu"):
        upload.assemble_patch_from_stores(lambda: nullcontext(connection), patch, LIMIT)


def test_sample_rate_mismatch_leaves_the_store_untouched(conn, data_root):
    connection, book, _ = conn
    chapter, opts, rules = _inputs(connection, book, 0)
    files = {0: ("x/0001/chunk_000.wav", _wav()), 1: ("x/0001/chunk_001.wav", _wav(rate=16000))}
    report = upload.install_chapter_chunks(book.id, chapter, opts, rules, LIMIT, files, replace=False)
    assert report["status"] == "error"
    assert "sample rate" in report["detail"]
    assert not chunk_store.get_chapter_chunk_dir(book.id, 0).exists()


def test_replace_clears_old_chunks_with_another_rate(conn, data_root):
    connection, book, _ = conn
    chapter, opts, rules = _inputs(connection, book, 0)
    upload.install_chapter_chunks(book.id, chapter, opts, rules, LIMIT,
                                  {1: ("x/0001/chunk_001.wav", _wav(rate=16000))}, replace=False)
    files = {0: ("x/0001/chunk_000.wav", _wav())}
    kept = upload.install_chapter_chunks(book.id, chapter, opts, rules, LIMIT, files, replace=False)
    assert kept["status"] == "error"
    files = {0: ("x/0001/chunk_000.wav", _wav())}
    replaced = upload.install_chapter_chunks(book.id, chapter, opts, rules, LIMIT, files, replace=True)
    assert replaced["installed"] == 1
    store = chunk_store.ChunkStore(chunk_store.get_chapter_chunk_dir(book.id, 0))
    assert not store.path(1).exists()


# --- endpoints ------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.jobqueue.handlers import audiobook_tts
    from app.epub_parser import ParsedChapter
    from app.main import app

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "api.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    monkeypatch.setattr(audiobook_tts, "normalize_wav_in_place", lambda path: None)
    with TestClient(app) as c:
        conn = c.app.state.conn
        book = repository.create_book(
            conn, title="Sách", original_filename="a.epub", epub_path=str(tmp_path / "a.epub"),
            patch_size=2, chapters=[ParsedChapter(title="Chương 1", text="Câu một. Câu hai."),
                      ParsedChapter(title="Chương 2", text="Câu ba.")],
            background_image_path=None,
        )
        repository.rebuild_patches(conn, book.id, [(0, 1)])
        yield c


def test_upload_endpoint_installs_reports_and_merges(client):
    conn = client.app.state.conn
    book = repository.get_book(conn, 1)
    counts = {}
    for chapter_index in (0, 1):
        chapter, opts, rules = _inputs(conn, book, chapter_index)
        counts[chapter_index] = len(upload.chapter_plan(chapter, opts, rules, 400))
    files = [
        ("files", (f"batch/{ci + 1:04d}/chunk_{i:03d}.wav", _wav().getvalue(), "audio/wav"))
        for ci, n in counts.items() for i in range(n)
    ] + [("files", ("batch/notes/chunk_000.wav", _wav().getvalue(), "audio/wav"))]
    response = client.post("/books/1/chapter-audio/upload", files=files, data={"replace": "false"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [r["status"] for r in body["chapters"]] == ["ok", "ok"]
    assert body["rejected"][0]["filename"] == "batch/notes/chunk_000.wav"
    assert body["affected_patches"][0]["patch_index"] == 0
    assert client.get("/books/1/chapter-audio").json()["chapters"]["0"]["ready"] is True

    merged = client.post("/books/1/chapter-audio/merge-patches", json={"chapter_indexes": [0, 1]}).json()
    assert merged["merged"] == 1
    patch = repository.list_patches(conn, 1)[0]
    assert patch.status == "done" and patch.audio_path
