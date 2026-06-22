"""End-to-end smoke test for the batch-preview routes.

Boots the FastAPI app on top of a temp SQLite DB, uploads a synthetic EPUB (which has a
TOC + 1 chapter), then exercises the new preview routes and asserts the expected
behavior. The app's PatchWorker is bypassed by uploading a book whose status is 'ready'
but not actually triggering TTS.
"""
from __future__ import annotations

import io
import sqlite3
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ebooklib import epub

from app import db as app_db
from app.main import app
from app import repository


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    settings_mod = __import__("app.config", fromlist=["settings"])
    monkeypatch.setattr(settings_mod.settings, "db_path", str(db_path))
    monkeypatch.setattr(settings_mod.settings, "data_root", str(tmp_path))
    with TestClient(app) as c:
        yield c


def _synthetic_epub(path: Path) -> None:
    toc_html = (
        "<html><body>"
        "<h1>Table of Contents</h1>"
        "<p>Chapter 1 ............ 3</p>\n"
        "<p>Chapter 2 ............ 25</p>\n"
        "<p>Chapter 3 ............ 47</p>\n"
        "<p>Chapter 4 ............ 69</p>\n"
        "<p>Chapter 5 ............ 91</p>\n"
        "<p>Chapter 6 ............ 113</p>\n"
        "</body></html>"
    )
    long_para = ("It was a bright cold day in April. " * 30).strip()
    chapter_html = (
        "<html><body>"
        "<h1>Chapter 1</h1>"
        f"<p>{long_para}</p>\n"
        f"<p>{long_para}</p>\n"
        f"<p>{long_para}</p>\n"
        f"<p>{long_para}</p>\n"
        "</body></html>"
    )
    book = epub.EpubBook()
    book.set_identifier("synthetic")
    book.set_title("synthetic")
    book.set_language("en")
    c1 = epub.EpubHtml(title="TOC", file_name="toc.xhtml", content=toc_html)
    c2 = epub.EpubHtml(title="Chapter 1", file_name="c1.xhtml", content=chapter_html)
    book.add_item(c1)
    book.add_item(c2)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", c1, c2]
    epub.write_epub(str(path), book)


def test_upload_skips_toc_chapter_and_preview_routes_work(client, tmp_path):
    epub_path = tmp_path / "book.epub"
    _synthetic_epub(epub_path)

    with open(epub_path, "rb") as f:
        resp = client.post(
            "/books/upload",
            files={"epub_file": ("book.epub", f, "application/epub+zip")},
            data={"patch_size": "10"},
            follow_redirects=False,
        )
    assert resp.status_code == 303, resp.text
    location = resp.headers["location"]
    book_id = int(location.rstrip("/").split("/")[-1])

    db_path = client.app.state.conn.execute("PRAGMA database_list").fetchone()["file"]
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT chapter_index, title FROM chapter WHERE book_id = ? ORDER BY chapter_index",
        (book_id,),
    ).fetchall()
    assert [r["title"] for r in rows] == ["Chapter 1"], f"TOC was not skipped: {rows}"
    conn.close()

    resp = client.get(f"/books/{book_id}/chapters/preview?ids=0")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload) == 1
    assert payload[0]["chapter_index"] == 0
    assert payload[0]["title"] == "Chapter 1"
    assert payload[0]["char_count"] > 0
    assert "bright cold day" in payload[0]["text_excerpt"]

    resp = client.get(f"/books/{book_id}/chapters/preview")
    assert resp.status_code == 400
    assert "ids" in resp.json()["detail"].lower()

    resp = client.get(f"/books/{book_id}/chapters/preview?ids=0,99")
    assert resp.status_code == 200
    payload = resp.json()
    assert len(payload) == 1  # unknown 99 silently skipped
    assert payload[0]["chapter_index"] == 0

    resp = client.get(f"/books/{book_id}/chapters/0/text")
    assert resp.status_code == 200
    assert "bright cold day" in resp.text

    resp = client.get(f"/books/{book_id}/chapters/99/text")
    assert resp.status_code == 404

    resp = client.get(f"/books/9999/chapters/preview?ids=0")
    assert resp.status_code == 404

    resp = client.get(f"/books/{book_id}/chapters/preview-ui?ids=0")
    assert resp.status_code == 200
    assert "Chapter 1" in resp.text

    resp = client.get(f"/books/{book_id}/chapters/preview-ui?range_start=0&range_end=0")
    assert resp.status_code == 200
    assert "Chapter 1" in resp.text

    resp = client.get(f"/books/{book_id}/chapters/preview-ui")
    assert resp.status_code == 200
    assert "No chapters selected" in resp.text
