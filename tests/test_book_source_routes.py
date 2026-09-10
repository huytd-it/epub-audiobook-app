import json
from datetime import datetime, timezone

from ebooklib import epub
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app


def _write_epub(path, title: str) -> None:
    book = epub.EpubBook()
    book.set_identifier("replacement-book")
    book.set_title(title)
    book.set_language("vi")
    chapter = epub.EpubHtml(title="Chương 1", file_name="chapter-1.xhtml", lang="vi")
    chapter.content = f"<h1>Chương 1</h1><p>{'Nội dung sách mới. ' * 30}</p>"
    book.add_item(chapter)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.toc = (epub.Link("chapter-1.xhtml", "Chương 1", "chapter-1"),)
    book.spine = ["nav", chapter]
    epub.write_epub(str(path), book)


def test_delete_and_replace_book_source_preserves_configuration_and_thumbnail(tmp_path, monkeypatch):
    data_root = tmp_path / "data"
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(data_root))
    monkeypatch.setattr(settings, "enable_worker", False)

    original_epub = data_root / "uploads" / "1.epub"
    original_epub.parent.mkdir(parents=True)
    original_epub.write_bytes(b"old epub")
    background = data_root / "background.png"
    background.write_bytes(b"background")
    audio = data_root / "books" / "1" / "audio" / "1_001.wav"
    video = data_root / "books" / "1" / "videos" / "1_001.mp4"
    overlay = data_root / "books" / "1" / "patch_overlays" / "1_001.png"
    podcast_cover = data_root / "books" / "1" / "podcast_cover.png"
    for path in (audio, video, overlay, podcast_cover):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(path.name.encode())

    now = datetime.now(timezone.utc).isoformat()
    automation_config = json.dumps({"audio": {"chunk_pause_ms": 700}, "branding": {"enabled": True}})
    overlay_config = json.dumps({"overlays": [{"text": "{book_title}"}]})

    with TestClient(app) as client:
        conn = client.app.state.conn
        conn.execute(
            """INSERT INTO book
                      (id, title, original_filename, epub_path, patch_size, status,
                       final_audio_path, final_video_path, background_image_path,
                       overlay_config, automation_config, tts_model, created_at, updated_at)
               VALUES (1, 'Tên đã chỉnh', 'old.epub', ?, 10, 'done', ?, ?, ?, ?, ?,
                       'edge-tts', ?, ?)""",
            (
                str(original_epub), str(audio), str(video), str(background),
                overlay_config, automation_config, now, now,
            ),
        )
        conn.execute(
            """INSERT INTO chapter (book_id, chapter_index, title, text, char_count)
               VALUES (1, 0, 'Chương cũ', 'Nội dung cũ', 12)"""
        )
        conn.execute(
            """INSERT INTO patch
                      (book_id, patch_index, chapter_start, chapter_end, status,
                       audio_path, created_at, updated_at)
               VALUES (1, 0, 0, 0, 'done', ?, ?, ?)""",
            (str(audio), now, now),
        )
        conn.commit()

        deleted = client.post("/books/1/source/delete")
        assert deleted.status_code == 200
        assert not original_epub.exists()
        assert not audio.exists()
        assert not video.exists()
        assert overlay.exists()
        assert podcast_cover.exists()
        assert background.exists()
        assert conn.execute("SELECT COUNT(*) FROM chapter WHERE book_id=1").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM patch WHERE book_id=1").fetchone()[0] == 0

        detached = conn.execute("SELECT * FROM book WHERE id=1").fetchone()
        assert detached["epub_path"] == ""
        assert detached["automation_config"] == automation_config
        assert detached["overlay_config"] == overlay_config
        assert detached["background_image_path"] == str(background)
        assert detached["tts_model"] == "edge-tts"

        replacement = tmp_path / "replacement.epub"
        _write_epub(replacement, "Tên trong EPUB mới")
        with replacement.open("rb") as handle:
            uploaded = client.post(
                "/books/1/source",
                files={"epub_file": ("new-book.epub", handle, "application/epub+zip")},
            )

        assert uploaded.status_code == 200
        assert uploaded.json()["chapters"] == 1
        restored = conn.execute("SELECT * FROM book WHERE id=1").fetchone()
        assert restored["title"] == "Tên đã chỉnh"
        assert restored["original_filename"] == "new-book.epub"
        assert restored["epub_path"]
        assert restored["automation_config"] == automation_config
        assert restored["overlay_config"] == overlay_config
        assert conn.execute("SELECT title FROM chapter WHERE book_id=1").fetchone()[0] == "Chương 1"
        assert conn.execute("SELECT COUNT(*) FROM patch WHERE book_id=1").fetchone()[0] == 0


def test_upload_book_source_requires_detached_book(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "data"))
    monkeypatch.setattr(settings, "enable_worker", False)
    replacement = tmp_path / "replacement.epub"
    _write_epub(replacement, "Replacement")
    now = datetime.now(timezone.utc).isoformat()

    with TestClient(app) as client:
        conn = client.app.state.conn
        conn.execute(
            """INSERT INTO book
                      (id, title, original_filename, epub_path, patch_size, status, created_at, updated_at)
               VALUES (1, 'Book', 'old.epub', 'old.epub', 10, 'ready', ?, ?)""",
            (now, now),
        )
        conn.commit()
        with replacement.open("rb") as handle:
            response = client.post(
                "/books/1/source",
                files={"epub_file": ("replacement.epub", handle, "application/epub+zip")},
            )

    assert response.status_code == 409
    assert "xóa EPUB gốc" in response.json()["detail"]
