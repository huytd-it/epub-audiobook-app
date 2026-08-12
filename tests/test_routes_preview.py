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


def _book_detail_html(client) -> str:
    conn = client.app.state.conn
    book = repository.create_book(
        conn,
        title="UI test book",
        original_filename="ui-test.epub",
        epub_path="",
        patch_size=10,
        chapters=[],
        background_image_path=None,
    )
    response = client.get(f"/books/{book.id}")
    assert response.status_code == 200
    return response.text
