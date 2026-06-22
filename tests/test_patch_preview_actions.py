"""Route tests for patch preview actions, patch builder, and chapter exclude."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from ebooklib import epub

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings_mod = __import__("app.config", fromlist=["settings"])
    monkeypatch.setattr(settings_mod.settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings_mod.settings, "data_root", str(tmp_path))
    with TestClient(app) as c:
        yield c


def _upload_book(client: TestClient, tmp_path: Path) -> int:
    """Upload a synthetic EPUB and return the book id."""
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
    for i in range(5):
        c = epub.EpubHtml(title=f"Ch{i}", file_name=f"c{i}.xhtml",
                          content=ch_html.replace("Chapter 0", f"Chapter {i}"))
        book.add_item(c)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav"] + list(book.get_items_of_type(9))  # ITEM_DOCUMENT=9
    epub.write_epub(str(epub_path), book)
    with open(epub_path, "rb") as f:
        resp = client.post(
            "/books/upload",
            files={"epub_file": ("t.epub", f, "application/epub+zip")},
            data={"patch_size": "5"},
            follow_redirects=False,
        )
    assert resp.status_code == 303
    return int(resp.headers["location"].rstrip("/").split("/")[-1])


def test_chapter_exclude_endpoint(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.post(
        f"/books/{book_id}/chapters/0/exclude",
        data={"excluded": "true"},
    )
    assert resp.status_code == 204
    resp = client.post(
        f"/books/{book_id}/chapters/0/exclude",
        data={"excluded": "false"},
    )
    assert resp.status_code == 204
    resp = client.post(
        f"/books/{book_id}/chapters/99/exclude",
        data={"excluded": "true"},
    )
    assert resp.status_code == 204  # silently OK if chapter not found


def test_replace_rules_endpoints(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.post(
        f"/books/{book_id}/replace-rules",
        data={"find": "AI", "replace": "A.I.", "is_regex": "false", "position": "0"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    resp = client.get(f"/books/{book_id}/replace-rules")
    assert resp.status_code == 200
    rules = resp.json()
    assert len(rules) == 1
    assert rules[0]["find"] == "AI"

    rule_id = rules[0]["id"]
    resp = client.post(
        f"/books/{book_id}/replace-rules/{rule_id}/delete",
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_invalid_regex_rejected(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.post(
        f"/books/{book_id}/replace-rules",
        data={"find": "[invalid", "replace": "", "is_regex": "true", "position": "0"},
    )
    assert resp.status_code == 400
    assert "regex" in resp.json()["detail"].lower()


def test_patch_text_endpoint(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.get(f"/books/{book_id}/patches/1/text")
    assert resp.status_code == 200
    assert "Chapter" in resp.text


def test_patch_audio_not_available(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.get(f"/books/{book_id}/patches/1/audio")
    assert resp.status_code == 404


def test_patch_builder_page(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.get(f"/books/{book_id}/patches/build")
    assert resp.status_code == 200
    assert "Patch builder" in resp.text
    assert "Chapter" in resp.text


def test_book_detail_shows_preview_link(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.get(f"/books/{book_id}")
    assert resp.status_code == 200
    assert "Preview text" in resp.text
    assert "Patch builder" in resp.text
    assert "Text replace rules" in resp.text
    assert "Auto-build" in resp.text


def test_auto_build_success_redirect(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.post(
        f"/books/{book_id}/patches/auto-build",
        data={"start_chapter": "0", "patch_size": "2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert f"/books/{book_id}" in resp.headers["location"]


def test_auto_build_start_missing_returns_400(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.post(
        f"/books/{book_id}/patches/auto-build",
        data={"patch_size": "5"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_auto_build_start_out_of_bounds_returns_400(client, tmp_path):
    book_id = _upload_book(client, tmp_path)
    resp = client.post(
        f"/books/{book_id}/patches/auto-build",
        data={"start_chapter": "999", "patch_size": "5"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "out of bounds" in resp.json()["detail"]
