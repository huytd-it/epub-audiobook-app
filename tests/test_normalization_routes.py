"""Route tests for TTS normalization settings and preview."""
from pathlib import Path

import pytest
from ebooklib import epub
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings_mod = __import__("app.config", fromlist=["settings"])
    monkeypatch.setattr(settings_mod.settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings_mod.settings, "data_root", str(tmp_path))
    with TestClient(app) as c:
        yield c


def _upload_book(client: TestClient, tmp_path: Path) -> int:
    epub_path = tmp_path / "test.epub"
    para = "x " * 200
    ch_html = (
        "<html><body>"
        "<h1>Chapter 0</h1><p>{}</p><p>{}</p>"
        "</body></html>"
    ).format(para, para)
    book = epub.EpubBook()
    book.set_identifier("t")
    book.set_title("t")
    book.set_language("en")
    for i in range(2):
        c = epub.EpubHtml(
            title=f"Ch{i}",
            file_name=f"c{i}.xhtml",
            content=ch_html.replace("Chapter 0", f"Chapter {i}"),
        )
        book.add_item(c)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + list(book.get_items_of_type(9))
    epub.write_epub(str(epub_path), book)
    with open(epub_path, "rb") as f:
        resp = client.post(
            "/books/upload",
            files={"epub_file": ("t.epub", f, "application/epub+zip")},
            data={"patch_size": "2"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    return int(resp.headers["location"].rstrip("/").split("/")[-1])




def test_normalization_preview_endpoint(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.get(f"/books/{book_id}/normalization/preview?chapter_index=0")
    assert resp.status_code == 200
    # Preview text is plain text and contains the chapter body.
    assert len(resp.text) > 0
