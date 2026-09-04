"""Text Studio: page, text editing, and the LightTTS endpoints the Book Detail
page's batch "Run Selected" drives."""
from __future__ import annotations

import json
import threading

import pytest
import soundfile as sf
from fastapi.testclient import TestClient

from app import repository
from app.config import settings
from app.epub_parser import ParsedChapter
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.joblog import JobLogger
from app.jobqueue.models import JobFatalError
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def book_and_patch(client):
    conn = client.app.state.conn
    chapters = [
        ParsedChapter(title="Chương một", text="Chương một.\n\nĐoạn văn đầu tiên của chương một."),
        ParsedChapter(title="Chương hai", text="Chương hai.\n\nĐoạn văn đầu tiên của chương hai."),
    ]
    book = repository.create_book(
        conn, title="Sách thử", original_filename="t.epub", epub_path="/tmp/t.epub",
        patch_size=2, chapters=chapters, background_image_path=None,
    )
    repository.rebuild_patches(conn, book.id, [(0, 1)])
    return book, repository.list_patches(conn, book.id)[0]


class _FakeEngine:
    """Stand-in for LightTTSEngine: writes a short silent WAV per chunk."""

    sample_rate = 16000
    calls: list[str] = []

    def __init__(self, backend="edge-tts", voice=None):
        self.backend = backend
        self.voice = voice

    def synthesize_to_wav_bytes(self, text, voice=None):
        import io

        import numpy as np

        _FakeEngine.calls.append(text)
        buf = io.BytesIO()
        sf.write(buf, np.zeros(self.sample_rate, dtype="float32"), self.sample_rate, format="WAV")
        return buf.getvalue(), self.sample_rate


@pytest.fixture()
def fake_engine(monkeypatch):
    from app.routes import text_studio

    _FakeEngine.calls = []
    monkeypatch.setattr(text_studio, "LightTTSEngine", _FakeEngine)
    return _FakeEngine


@pytest.fixture()
def synchronous_light_tts(monkeypatch, fake_engine):
    """Run queued LightTTS jobs inline while retaining the preview SSE bridge."""
    from app.jobqueue.handlers import light_tts

    monkeypatch.setattr(light_tts, "_build_engine", lambda backend, voice: fake_engine(backend, voice))
    original_enqueue = store.enqueue

    def enqueue_and_run(conn, job_type, *args, **kwargs):
        job_id = original_enqueue(conn, job_type, *args, **kwargs)
        if job_type != "light_tts":
            return job_id
        error = []

        def run():
            job = store.claim(conn, job_type, "test-light-tts")
            ctx = JobContext(job, conn, JobLogger(job_id, job_type), lambda: False)
            try:
                result = light_tts.handle(ctx)
                ctx.flush()
                store.finish(conn, job_id, result)
            except JobFatalError as exc:
                ctx.flush()
                store.fail(conn, job_id, str(exc), fatal=True)
            except Exception as exc:  # pragma: no cover - preserves runner behavior
                error.append(exc)
                ctx.flush()
                store.fail(conn, job_id, str(exc), fatal=True)
            finally:
                ctx.close()

        thread = threading.Thread(target=run)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return job_id

    monkeypatch.setattr(store, "enqueue", enqueue_and_run)


# ---------------------------------------------------------------------------
# Page + Book Detail wiring
# ---------------------------------------------------------------------------








# ---------------------------------------------------------------------------
# Editing
# ---------------------------------------------------------------------------


def test_get_patch_text_reports_unedited_then_edited(client, book_and_patch):
    book, patch = book_and_patch
    base = f"/books/{book.id}/text-studio/patches/{patch.id}"

    first = client.get(base).json()
    assert first["is_edited"] is False
    assert "Chương một" in first["text"]

    assert client.put(base, json={"text": "Nội dung đã sửa."}).status_code == 200
    second = client.get(base).json()
    assert second["is_edited"] is True
    assert second["text"] == "Nội dung đã sửa."


def test_reset_restores_derived_text(client, book_and_patch):
    book, patch = book_and_patch
    base = f"/books/{book.id}/text-studio/patches/{patch.id}"
    client.put(base, json={"text": "Nội dung đã sửa."})

    reset = client.post(f"{base}/reset")
    assert reset.status_code == 200
    assert "Chương một" in reset.json()["text"]
    assert client.get(base).json()["is_edited"] is False


def test_search_replace_persists_as_an_edit(client, book_and_patch):
    book, patch = book_and_patch
    base = f"/books/{book.id}/text-studio/patches/{patch.id}"

    response = client.post(f"{base}/replace", json={"search": "Chương", "replace": "Phần"})
    assert response.status_code == 200
    assert response.json()["replacements"] > 0
    assert "Chương" not in client.get(base).json()["text"]


def test_search_replace_rejects_invalid_regex(client, book_and_patch):
    book, patch = book_and_patch
    response = client.post(
        f"/books/{book.id}/text-studio/patches/{patch.id}/replace",
        json={"search": "[unclosed", "replace": "", "is_regex": True},
    )
    assert response.status_code == 400
    assert "regex" in response.json()["detail"].lower()


def test_book_search_replace_updates_every_patch(client, book_and_patch):
    book, _ = book_and_patch
    conn = client.app.state.conn
    repository.rebuild_patches(conn, book.id, [(0, 0), (1, 1)])

    response = client.post(
        f"/books/{book.id}/text-studio/replace",
        json={"search": "Chương", "replace": "Phần"},
    )

    assert response.status_code == 200
    assert response.json() == {"replacements": 2, "changed_patches": 2}
    for patch in repository.list_patches(conn, book.id):
        assert "Phần" in repository.get_effective_patch_text(conn, patch)


def test_analyze_stores_warnings_for_the_patch(client, book_and_patch):
    book, patch = book_and_patch
    base = f"/books/{book.id}/text-studio/patches/{patch.id}"

    response = client.post(f"{base}/analyze", json={"text": "Cô ấy [tiếng khóc] rồi bỏ đi @@rác"})
    assert response.status_code == 200
    kinds = {w["kind"] for w in response.json()["warnings"]}
    assert {"effect_marker", "junk"} & kinds
    assert client.get(base).json()["warnings"]


# ---------------------------------------------------------------------------
# Edited text is what gets spoken
# ---------------------------------------------------------------------------


def test_edited_text_replaces_the_chapter_derived_chunk_plan(client, book_and_patch):
    """The whole point of editing in Text Studio: every TTS path must speak the
    saved text, not the text derived from the EPUB chapters."""
    book, patch = book_and_patch
    conn = client.app.state.conn
    client.put(f"/books/{book.id}/text-studio/patches/{patch.id}", json={"text": "Chỉ còn đúng câu này."})

    plan = repository.build_patch_chunk_plan(conn, repository.get_patch(conn, patch.id))
    assert [item["text"] for item in plan] == ["Chỉ còn đúng câu này."]
    assert all(item["chapter_index"] is None for item in plan)


def test_chunk_texts_follow_the_saved_edit(client, book_and_patch):
    book, patch = book_and_patch
    client.put(f"/books/{book.id}/text-studio/patches/{patch.id}", json={"text": "Một câu duy nhất."})

    data = client.get(f"/books/{book.id}/patches/{patch.id}/chunk-texts").json()
    assert data["total"] == 1
    assert data["chunks"][0]["text"] == "Một câu duy nhất."


def test_chunk_texts_split_chapters_independently(client, book_and_patch):
    book, patch = book_and_patch
    data = client.get(f"/books/{book.id}/patches/{patch.id}/chunk-texts").json()
    assert data["total"] == 2   # one chunk per chapter, never merged across the boundary
    assert data["max_chars"] == settings.tts_max_chars


def test_saving_an_edit_updates_the_stored_chunk_count(client, book_and_patch):
    book, patch = book_and_patch
    conn = client.app.state.conn
    response = client.put(
        f"/books/{book.id}/text-studio/patches/{patch.id}", json={"text": "Ngắn."},
    )
    assert response.json()["chunk_count"] == 1
    assert repository.get_patch(conn, patch.id).chunk_count == 1


# ---------------------------------------------------------------------------
# LightTTS synthesis (the batch "Run Selected" path)
# ---------------------------------------------------------------------------


def _stream_events(client, url):
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        return [
            json.loads(line[len("data: "):])
            for line in response.iter_lines()
            if line.startswith("data: ")
        ]


def test_preview_stream_synthesizes_merges_and_marks_done(client, book_and_patch, fake_engine, synchronous_light_tts):
    book, patch = book_and_patch
    events = _stream_events(
        client, f"/books/{book.id}/text-studio/patches/{patch.id}/preview-stream?backend=edge-tts",
    )

    assert [e["type"] for e in events] == ["chunk", "chunk", "done"]
    assert events[-1] == {"type": "done", "saved": True, "complete": True, "ok": 2, "failed": 0}

    stored = repository.get_patch(client.app.state.conn, patch.id)
    assert stored.status == "done"
    assert sf.info(stored.audio_path).frames > 0


def test_preview_stream_writes_a_chapter_timeline(client, book_and_patch, fake_engine, synchronous_light_tts):
    from app.youtube_metadata import load_timeline

    book, patch = book_and_patch
    _stream_events(client, f"/books/{book.id}/text-studio/patches/{patch.id}/preview-stream")

    stored = repository.get_patch(client.app.state.conn, patch.id)
    timeline = load_timeline(stored.audio_path)
    assert timeline is not None
    assert [c["title"] for c in timeline["chapters"]] == ["Chương một", "Chương hai"]


def test_preview_stream_speaks_the_edited_text(client, book_and_patch, fake_engine, synchronous_light_tts):
    book, patch = book_and_patch
    client.put(f"/books/{book.id}/text-studio/patches/{patch.id}", json={"text": "Chỉ đọc câu này."})

    events = _stream_events(client, f"/books/{book.id}/text-studio/patches/{patch.id}/preview-stream")
    assert events[-1]["complete"] is True
    assert fake_engine.calls == ["Chỉ đọc câu này."]


def test_preview_stream_reuses_chunks_from_an_earlier_identical_run(client, book_and_patch, fake_engine, synchronous_light_tts):
    book, patch = book_and_patch
    url = f"/books/{book.id}/text-studio/patches/{patch.id}/preview-stream"
    _stream_events(client, url)
    assert len(fake_engine.calls) == 2

    events = _stream_events(client, url)
    assert len(fake_engine.calls) == 2   # nothing re-synthesized
    assert all(e.get("reused") for e in events if e["type"] == "chunk")


def test_preview_stream_resynthesizes_after_the_text_changes(client, book_and_patch, fake_engine, synchronous_light_tts):
    book, patch = book_and_patch
    url = f"/books/{book.id}/text-studio/patches/{patch.id}/preview-stream"
    _stream_events(client, url)
    fake_engine.calls = []

    client.put(f"/books/{book.id}/text-studio/patches/{patch.id}", json={"text": "Nội dung hoàn toàn mới."})
    _stream_events(client, url)
    assert fake_engine.calls == ["Nội dung hoàn toàn mới."]


def test_preview_stream_keeps_good_chunks_and_refuses_to_save_a_holey_patch(
    client, book_and_patch, fake_engine, monkeypatch, synchronous_light_tts
):
    book, patch = book_and_patch
    original = _FakeEngine.synthesize_to_wav_bytes

    def flaky(self, text, voice=None):
        if "hai" in text:
            raise RuntimeError("engine exploded")
        return original(self, text, voice)

    monkeypatch.setattr(_FakeEngine, "synthesize_to_wav_bytes", flaky)
    monkeypatch.setattr(settings, "light_tts_chunk_retries", 1)

    events = _stream_events(client, f"/books/{book.id}/text-studio/patches/{patch.id}/preview-stream")
    assert [e["type"] for e in events] == ["chunk", "chunk_error", "done"]
    assert events[-1] == {"type": "done", "saved": False, "complete": False, "ok": 1, "failed": 1}

    stored = repository.get_patch(client.app.state.conn, patch.id)
    assert stored.status != "done"
    assert stored.audio_path is None

    # The chunk that did succeed stays on disk, so re-running only fills the gap.
    monkeypatch.setattr(_FakeEngine, "synthesize_to_wav_bytes", original)
    fake_engine.calls = []
    events = _stream_events(client, f"/books/{book.id}/text-studio/patches/{patch.id}/preview-stream")
    assert len(fake_engine.calls) == 1
    assert events[-1]["complete"] is True


def test_preview_stream_reports_a_total_engine_failure(client, book_and_patch, fake_engine, monkeypatch, synchronous_light_tts):
    book, patch = book_and_patch
    monkeypatch.setattr(settings, "light_tts_chunk_retries", 1)
    monkeypatch.setattr(
        _FakeEngine, "synthesize_to_wav_bytes",
        lambda self, text, voice=None: (_ for _ in ()).throw(RuntimeError("engine down")),
    )

    events = _stream_events(client, f"/books/{book.id}/text-studio/patches/{patch.id}/preview-stream")
    assert events[-1]["type"] == "error"
    assert repository.get_patch(client.app.state.conn, patch.id).status != "done"


def test_preview_paragraph_returns_audio_without_saving(client, book_and_patch, fake_engine):
    book, patch = book_and_patch
    response = client.post(
        f"/books/{book.id}/text-studio/patches/{patch.id}/preview-paragraph",
        json={"text": "Nghe thử đoạn này."},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert repository.get_patch(client.app.state.conn, patch.id).audio_path is None


def test_preview_paragraph_requires_text(client, book_and_patch, fake_engine):
    book, patch = book_and_patch
    response = client.post(
        f"/books/{book.id}/text-studio/patches/{patch.id}/preview-paragraph", json={"text": "   "},
    )
    assert response.status_code == 400


def test_chunk_audio_is_served_after_a_run(client, book_and_patch, fake_engine, synchronous_light_tts):
    book, patch = book_and_patch
    _stream_events(client, f"/books/{book.id}/text-studio/patches/{patch.id}/preview-stream")

    assert client.get(f"/books/{book.id}/patches/{patch.id}/chunk-audio/0").status_code == 200
    assert client.get(f"/books/{book.id}/patches/{patch.id}/chunk-audio/99").status_code == 404


def _write_silent_wav(path):
    import io

    import numpy as np

    path.parent.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    sf.write(buf, np.zeros(1600, dtype="float32"), 16000, format="WAV")
    path.write_bytes(buf.getvalue())


def test_chunk_preview_reads_the_production_chunk_layout(client, book_and_patch):
    """Chunk do pipeline TTS chính sinh ra nằm ở audio/{book}_{episode}_chunks. Trước đây
    endpoint phát thử chỉ nhìn vào layout cũ patches/{patch_id}_chunks nên luôn báo
    'chưa có chunk' dù file có thật."""
    book, patch = book_and_patch
    chunk_dir = repository.get_patch_chunk_dir(book.id, patch.patch_index)
    _write_silent_wav(chunk_dir / "chunk_000.wav")

    assert client.get(f"/books/{book.id}/patches/{patch.id}/chunk-audio/0").status_code == 200
    preview = client.get(f"/books/{book.id}/patches/{patch.id}/completed-chunks-preview").json()
    assert [chunk["index"] for chunk in preview["chunks"]] == [0]


def test_chunk_preview_falls_back_to_the_legacy_chunk_layout(client, book_and_patch):
    """Sách cũ vẫn còn chunk ở patches/{patch_id}_chunks — vẫn phải phát thử được."""
    book, patch = book_and_patch
    legacy_dir = repository._chunk_dir_for(book.id, patch.id)
    _write_silent_wav(legacy_dir / "chunk_000.wav")

    assert client.get(f"/books/{book.id}/patches/{patch.id}/chunk-audio/0").status_code == 200
    preview = client.get(f"/books/{book.id}/patches/{patch.id}/completed-chunks-preview").json()
    assert [chunk["index"] for chunk in preview["chunks"]] == [0]


def test_light_tts_writes_into_the_current_layout(client, book_and_patch, fake_engine, synchronous_light_tts):
    """Đầu ghi (LightTTS) và đầu đọc (phát thử) phải trỏ cùng một thư mục, nếu không
    bản xem trước vừa sinh sẽ bị bản chunk cũ ở thư mục kia che mất."""
    book, patch = book_and_patch
    _stream_events(client, f"/books/{book.id}/text-studio/patches/{patch.id}/preview-stream")

    chunk_dir = repository.get_patch_chunk_dir(book.id, patch.patch_index)
    assert sorted(p.name for p in chunk_dir.glob("chunk_*.wav"))
    assert not repository._chunk_dir_for(book.id, patch.id).exists()
    saved = repository.get_patch(client.app.state.conn, patch.id).audio_path
    assert saved == str(repository.get_patch_audio_path(book.id, patch.patch_index))
