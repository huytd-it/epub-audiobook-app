"""Kho chunk TTS theo chương (app/chunk_store.py): chunk sống ở books/{id}/chapters/NNNN,
độc lập với patch, nên build lại / reset patch chỉ còn là ghép lại."""
from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import chunk_store, db, repository
from app.config import settings
from app.epub_parser import ParsedChapter
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.handlers import audiobook_tts
from app.jobqueue.joblog import JobLogger


class _FakeEngine:
    def __init__(self, cfg="a"):
        self.calls: list[str] = []
        self.cfg = cfg
        self.sample_rate_reads = 0

    @property
    def sample_rate(self):
        self.sample_rate_reads += 1
        return 24000

    def config_fingerprint(self):
        return self.cfg

    def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None):
        self.calls.append(text)
        return np.full(2400, 0.1, dtype="float32")


CHAPTERS = [
    ParsedChapter(title="Chương 1", text="Câu một của chương một. Câu hai của chương một."),
    ParsedChapter(title="Chương 2", text="Câu một của chương hai. Câu hai của chương hai."),
]


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "a.db"))
    monkeypatch.setattr(settings, "tts_write_chunk_files", True)
    connection = db.connect(str(tmp_path / "a.db"))
    db.init_schema(connection)
    book = repository.create_book(
        connection, title="Sách", original_filename="a.epub", epub_path=str(tmp_path / "a.epub"),
        patch_size=2, chapters=CHAPTERS, background_image_path=None,
    )
    repository.rebuild_patches(connection, book.id, [(0, 1)])
    return connection


def _patch(conn):
    return repository.list_patches(conn, 1)[0]


def _run(conn, engine, monkeypatch):
    monkeypatch.setattr(audiobook_tts, "get_engine", lambda *a, **k: engine)
    job_id = store.enqueue(conn, "audiobook_tts", payload={"patch_id": _patch(conn).id, "max_chars": 30}, book_id=1)
    job = store.claim(conn, "audiobook_tts", "w")
    result = audiobook_tts.handle(JobContext(job, conn, JobLogger(job_id, "audiobook_tts"), lambda: False))
    # handle() không tự đóng job (việc của queue runner): dọn đi để lần chạy sau claim được.
    conn.execute("DELETE FROM job WHERE job_type='audiobook_tts'")
    conn.commit()
    return result


def _chapter_wavs(index):
    return sorted(p.name for p in chunk_store.get_chapter_chunk_dir(1, index).glob("chunk_*.wav"))


def test_chunks_land_in_per_chapter_folders_with_a_manifest(conn, monkeypatch):
    engine = _FakeEngine()
    result = _run(conn, engine, monkeypatch)

    assert chunk_store.get_chapter_chunk_dir(1, 0).name == "0001"
    assert _chapter_wavs(0) and _chapter_wavs(1)
    manifest = chunk_store.ChunkStore(chunk_store.get_chapter_chunk_dir(1, 0)).manifest()
    assert manifest["max_chars"] == 30 and manifest["sample_rate"] == 24000
    assert all(entry["text"] in engine.calls for entry in manifest["chunks"].values())
    assert not list(repository.get_patch_chunk_dir(1, 0).glob("chunk_*.wav"))
    assert sf.info(result["audio_path"]).frames > 0


def test_rebuilding_patches_keeps_chapter_chunks_and_rerun_only_merges(conn, monkeypatch):
    _run(conn, _FakeEngine(), monkeypatch)
    before = _chapter_wavs(0) + _chapter_wavs(1)

    repository.rebuild_patches(conn, 1, [(0, 1)])
    assert _chapter_wavs(0) + _chapter_wavs(1) == before

    engine = _FakeEngine()
    result = _run(conn, engine, monkeypatch)
    assert engine.calls == []
    # Ghép-only không được nạp model chỉ để hỏi sample rate.
    assert engine.sample_rate_reads == 0
    assert sf.info(result["audio_path"]).samplerate == 24000
    assert repository.get_patch(conn, _patch(conn).id).status == "done"


def test_deleting_one_chunk_resynthesizes_only_that_chunk(conn, monkeypatch):
    _run(conn, _FakeEngine(), monkeypatch)
    cache = chunk_store.ChunkStore(chunk_store.get_chapter_chunk_dir(1, 1))
    deleted_text = cache.entry(0)["text"]
    assert cache.delete(0)

    repository.reset_patch(conn, _patch(conn).id)
    engine = _FakeEngine()
    _run(conn, engine, monkeypatch)
    assert engine.calls == [deleted_text]


def test_editing_a_chapter_resynthesizes_only_that_chapter(conn, monkeypatch):
    _run(conn, _FakeEngine(), monkeypatch)
    chapter = repository.get_chapter(conn, 1, 1)
    conn.execute("UPDATE chapter SET text=? WHERE id=?", ("Một câu hoàn toàn mới.", chapter.id))
    conn.commit()

    repository.reset_patch(conn, _patch(conn).id)
    engine = _FakeEngine()
    _run(conn, engine, monkeypatch)
    assert engine.calls and all("chương một" not in text for text in engine.calls)
    # Chương bị tách lại ít chunk hơn: file thừa bị dọn.
    cache = chunk_store.ChunkStore(chunk_store.get_chapter_chunk_dir(1, 1))
    assert len(_chapter_wavs(1)) == len(cache.manifest()["chunks"])


def test_changing_the_voice_invalidates_every_chunk(conn, monkeypatch):
    first = _FakeEngine("voice-a")
    _run(conn, first, monkeypatch)

    repository.reset_patch(conn, _patch(conn).id)
    second = _FakeEngine("voice-b")
    _run(conn, second, monkeypatch)
    assert sorted(second.calls) == sorted(first.calls)


def test_purging_book_content_drops_the_chapter_store(conn, monkeypatch, tmp_path):
    _run(conn, _FakeEngine(), monkeypatch)
    assert chunk_store.get_book_chapters_dir(1).is_dir()
    repository._purge_book_content_files(repository.get_book(conn, 1), str(tmp_path), drop_epub=False)
    assert not chunk_store.get_book_chapters_dir(1).exists()


def _ready(conn):
    book = repository.get_book(conn, 1)
    from app.production_defaults import get_effective_normalization_options
    summary = repository.list_chapter_audio_summary(
        1, repository.list_chapters(conn, 1), get_effective_normalization_options(conn, book),
        repository.list_replace_rules(conn, 1))
    return {index: item["ready"] for index, item in summary.items()}


def test_chapter_is_ready_only_while_its_text_and_chunks_match(conn, monkeypatch):
    _run(conn, _FakeEngine(), monkeypatch)
    assert _ready(conn) == {0: True, 1: True}

    chunk_store.ChunkStore(chunk_store.get_chapter_chunk_dir(1, 0)).delete(0)
    chapter = repository.get_chapter(conn, 1, 1)
    conn.execute("UPDATE chapter SET text=? WHERE id=?", ("Văn bản đã sửa.", chapter.id))
    conn.commit()
    assert _ready(conn) == {0: False, 1: False}

    repository.reset_patch(conn, _patch(conn).id)
    _run(conn, _FakeEngine(), monkeypatch)
    assert _ready(conn) == {0: True, 1: True}


def test_reimport_keeps_chunks_of_unchanged_chapters_under_their_new_number(conn, monkeypatch, tmp_path):
    _run(conn, _FakeEngine(), monkeypatch)
    kept_manifest = chunk_store.ChunkStore(chunk_store.get_chapter_chunk_dir(1, 0)).manifest()

    reparsed = [
        ParsedChapter(title="Lời tựa", text="Một chương mới chèn lên đầu."),
        CHAPTERS[0],
        ParsedChapter(title="Chương 2", text="Chương hai đã được viết lại hoàn toàn."),
    ]
    repository.reimport_book_chapters(conn, 1, str(tmp_path), reparsed)

    # Chương 1 không đổi: kho của nó dời từ 0001 sang 0002, nguyên chunk.
    moved = chunk_store.ChunkStore(chunk_store.get_chapter_chunk_dir(1, 1))
    assert moved.manifest()["chunks"] == kept_manifest["chunks"]
    # Chương 2 đổi nội dung và chương mới: không có kho nào.
    assert not chunk_store.get_chapter_chunk_dir(1, 0).exists()
    assert not chunk_store.get_chapter_chunk_dir(1, 2).exists()

    repository.rebuild_patches(conn, 1, [(0, 2)])
    engine = _FakeEngine()
    _run(conn, engine, monkeypatch)
    assert engine.calls and all("chương một" not in text for text in engine.calls)


def test_incremental_update_clears_only_the_changed_chapter(conn, monkeypatch):
    _run(conn, _FakeEngine(), monkeypatch)
    # Chưa có patch nào "done" bảo vệ chương: bỏ audio patch để cập nhật được.
    repository.reset_patch(conn, _patch(conn).id)
    reparsed = [CHAPTERS[0], ParsedChapter(title="Chương 2", text="Chương hai bản mới.")]
    result = repository.append_new_chapters(conn, 1, reparsed, update_changed=True)

    assert result["updated"] == 1
    assert _chapter_wavs(0)
    assert not _chapter_wavs(1)


def test_resolve_plan_chunks_numbers_within_each_chapter(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    plan = [{"text": "a", "chapter_index": 2}, {"text": "b", "chapter_index": 2},
            {"text": "c", "chapter_index": 3}, {"text": "d", "chapter_index": None}]
    targets = chunk_store.resolve_plan_chunks(1, tmp_path / "patch", plan)
    assert [(t[0].dir.name, t[1]) for t in targets] == [("0003", 0), ("0003", 1), ("0004", 0), ("patch", 0)]


# --- endpoints ------------------------------------------------------------------


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app.main import app

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "api.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    monkeypatch.setattr(settings, "tts_write_chunk_files", True)
    with TestClient(app) as c:
        conn = c.app.state.conn
        book = repository.create_book(
            conn, title="Sách", original_filename="a.epub", epub_path=str(tmp_path / "a.epub"),
            patch_size=2, chapters=CHAPTERS, background_image_path=None,
        )
        repository.rebuild_patches(conn, book.id, [(0, 1)])
        yield c


def _synthesize_via(client, monkeypatch):
    conn = client.app.state.conn
    _run(conn, _FakeEngine(), monkeypatch)


def test_chapter_audio_endpoints_report_play_and_delete(client, monkeypatch):
    _synthesize_via(client, monkeypatch)

    summary = client.get("/books/1/chapter-audio").json()["chapters"]
    assert summary["0"]["chunks"] > 0 and summary["0"]["duration"] > 0

    detail = client.get("/books/1/chapter-audio/0").json()
    assert detail["chunks"] and {c["status"] for c in detail["chunks"]} == {"done"}
    assert detail["patches"][0]["patch_index"] == 0
    assert client.get(detail["chunks"][0]["url"]).status_code == 200

    response = client.delete("/books/1/chapter-audio/0/chunks/0")
    assert response.status_code == 200 and response.json()["affected_patches"]
    detail = client.get("/books/1/chapter-audio/0").json()
    assert detail["chunks"][0]["status"] == "missing"
    assert client.delete("/books/1/chapter-audio/0/chunks/0").status_code == 404

    deleted = client.post("/books/1/chapter-audio/delete", json={"chapter_indexes": [0, 1]}).json()
    assert deleted["deleted"] > 0
    assert client.get("/books/1/chapter-audio").json()["chapters"] == {}


def test_chapter_detail_flags_stale_chunks_after_a_text_edit(client, monkeypatch):
    _synthesize_via(client, monkeypatch)
    conn = client.app.state.conn
    chapter = repository.get_chapter(conn, 1, 0)
    conn.execute("UPDATE chapter SET text=? WHERE id=?", ("Một câu khác hẳn.", chapter.id))
    conn.commit()
    statuses = {c["status"] for c in client.get("/books/1/chapter-audio/0").json()["chunks"]}
    assert "stale" in statuses


def test_rebuild_patches_resets_and_queues_covering_patches(client, monkeypatch):
    _synthesize_via(client, monkeypatch)
    conn = client.app.state.conn
    patch = _patch(conn)
    assert patch.status == "done"

    result = client.post("/books/1/chapter-audio/rebuild-patches", json={"chapter_indexes": [1]}).json()
    assert result["queued"] == 1 and result["patches"][0]["patch_id"] == patch.id
    assert repository.get_patch(conn, patch.id).status == "pending"
    assert _chapter_wavs(0) and _chapter_wavs(1)


def test_chunk_endpoints_reject_unknown_chapters(client):
    assert client.get("/books/1/chapter-audio/99").status_code == 404
    assert client.get("/books/1/chapter-audio/0/chunks/0").status_code == 404
    assert client.delete("/books/1/chapter-audio/99").status_code == 404
