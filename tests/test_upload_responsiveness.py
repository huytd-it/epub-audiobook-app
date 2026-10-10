from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routes import books
from app.youtube_metadata import get_book_youtube_config


@pytest.mark.parametrize("path", ["/books/parse-epub", "/books/upload"])
def test_epub_processing_does_not_block_http(path, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    monkeypatch.setattr(books.youtube, "is_configured", lambda: False)
    entered, release = Event(), Event()

    def slow_parse(_path):
        entered.set()
        assert release.wait(5), "test did not release the EPUB parser"
        return []

    monkeypatch.setattr(books, "parse_epub", slow_parse)
    with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as executor:
        upload = executor.submit(client.post, path,
                                 files={"epub_file": ("test.epub", b"test", "application/epub+zip")},
                                 data={"book_title": "Sample story", "book_subjects": "Fantasy"},
                                 follow_redirects=False)
        try:
            assert entered.wait(2)
            probe = executor.submit(client.get, "/api/live")
            assert probe.result(timeout=2).status_code == 200
        finally:
            release.set()
        response = upload.result(timeout=5)
        assert response.status_code == (303 if path.endswith("upload") else 200)
        if path.endswith("upload"):
            book_id = int(response.headers["location"].rsplit("/", 1)[1])
            config = get_book_youtube_config(client.app.state.conn, book_id)
            assert config["genre_tags"] == "Fantasy"
            assert config["description_extra"]["story_title"] == "Sample story"
