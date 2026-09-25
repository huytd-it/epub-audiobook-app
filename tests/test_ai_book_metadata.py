"""AI book metadata + extensible provider/task registries.

Covers the feature: upload saves enough book facts (OPF metadata + cover)
that AI generation is grounded in the real book, and new AI backends/tasks
plug in via register_* without touching routes.
"""
import io
import json

import pytest
from ebooklib import epub

from app import ai_content, ai_providers, db, repository
from app.config import settings
from app.epub_parser import extract_epub_cover, extract_epub_metadata


def _write_epub(path, *, with_cover: bool = False) -> None:
    book = epub.EpubBook()
    book.set_identifier("id-123")
    book.set_title("Truyện Kiếm Hiệp")
    book.add_author("Kim Dung")
    book.set_language("vi")
    book.add_metadata("DC", "publisher", "NXB Văn Học")
    book.add_metadata("DC", "description", "Giang hồ dậy sóng.")
    book.add_metadata("DC", "subject", "kiếm hiệp, tiên hiệp")
    c1 = epub.EpubHtml(title="Chương 1", file_name="c1.xhtml",
                       content="<html><body><h1>Chương 1: Gặp gỡ</h1><p>" + "Ngày xửa ngày xưa. " * 60 + "</p></body></html>")
    book.add_item(c1)
    if with_cover:
        try:
            from PIL import Image
            buf = io.BytesIO()
            Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, "PNG")
            png = buf.getvalue()
        except Exception:
            png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 200
        book.add_item(epub.EpubItem(uid="cover", file_name="cover.png",
                                    media_type="image/png", content=png))
    book.toc = ()
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", c1]
    epub.write_epub(str(path), book)


def test_extract_epub_metadata_reads_opf(tmp_path):
    p = tmp_path / "b.epub"
    _write_epub(p)
    meta = extract_epub_metadata(str(p))
    assert meta.title == "Truyện Kiếm Hiệp"
    assert meta.creator == "Kim Dung"
    assert meta.language == "vi"
    assert meta.publisher == "NXB Văn Học"
    assert "Giang hồ" in meta.description
    assert "kiếm hiệp" in meta.subjects and "tiên hiệp" in meta.subjects


def test_extract_epub_metadata_never_raises(tmp_path):
    meta = extract_epub_metadata(str(tmp_path / "missing.epub"))
    assert meta.title == "" and meta.subjects == []


def test_extract_epub_cover_none_without_images(tmp_path):
    p = tmp_path / "b.epub"
    _write_epub(p)
    assert extract_epub_cover(str(p)) is None


def test_extract_epub_cover_finds_image(tmp_path):
    p = tmp_path / "b.epub"
    _write_epub(p, with_cover=True)
    found = extract_epub_cover(str(p))
    assert found is not None and len(found[0]) > 50 and found[1] == ".png"


def _memdb():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def test_create_book_persists_metadata_and_cover(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    conn = _memdb()
    from app.epub_parser import ParsedChapter
    book = repository.create_book(
        conn, title="T", original_filename="t.epub", epub_path="x",
        patch_size=10, chapters=[ParsedChapter(title="Chương 1", text="abc " * 30)],
        background_image_path=None, author="Kim Dung", description="syn",
        language="vi", publisher="NXB", subjects="kiếm hiệp, tiên hiệp",
    )
    assert book.author == "Kim Dung" and book.language == "vi"
    assert book.subjects == "kiếm hiệp, tiên hiệp"
    dest = repository.save_book_cover(conn, book.id, b"fakepng" * 100, ".png")
    assert dest.endswith(".png")
    assert repository.get_book(conn, book.id).cover_image_path == dest

    repository.update_book_metadata(conn, book.id, author="Cổ Long", subjects=["a", "b"])
    got = repository.get_book(conn, book.id)
    assert got.author == "Cổ Long" and got.subjects == "a, b"
    conn.close()


def test_book_context_grounded_in_saved_metadata(tmp_path):
    conn = _memdb()
    from app.epub_parser import ParsedChapter
    book = repository.create_book(
        conn, title="Truyện Kiếm Hiệp", original_filename="t.epub", epub_path="x",
        patch_size=10, chapters=[ParsedChapter(title="Chương 1: Gặp gỡ", text="Ngày xửa. " * 100)],
        background_image_path=None, author="Kim Dung", description="Giang hồ.",
        language="vi", subjects="kiếm hiệp",
    )
    ctx = ai_content.build_book_context(conn, book)
    assert ctx["author"] == "Kim Dung" and ctx["subjects"] == ["kiếm hiệp"]
    assert ctx["chapter_count"] == 1 and ctx["excerpt_chapter_1"]
    brief = ai_content.context_to_brief(ctx)
    assert "Kim Dung" in brief and "Chương 1" in brief
    conn.close()


def test_unknown_task_raises_with_available_names():
    with pytest.raises(ValueError, match="youtube_content"):
        ai_content.get_task("nope")


def test_register_custom_task_extends_without_route_changes(monkeypatch):
    ai_content.register_task(
        "shout", "Shout",
        lambda ctx, params: {"system": "s", "user": f"say {ctx['title']}",
                             "max_tokens": 10, "response_format": "text"},
    )
    assert "shout" in {t["name"] for t in ai_content.list_tasks()}
    monkeypatch.setattr(ai_providers, "generate_text", lambda **kw: "hello")
    conn = _memdb()
    from app.epub_parser import ParsedChapter
    book = repository.create_book(
        conn, title="T", original_filename="t.epub", epub_path="x", patch_size=10,
        chapters=[ParsedChapter(title="C1", text="x " * 40)], background_image_path=None)
    out = ai_content.run_task(conn, book, "shout", params={"save": False})
    assert out["result"] == {"text": "hello"}
    conn.close()


def test_run_youtube_content_saves_and_prefills(monkeypatch):
    fake = {"playlist_title": "P", "playlist_description": "D",
            "video_title_template": "{book_title} - {episode_number}",
            "video_description": "Mô tả", "genre_tags": "a, b",
            "thumbnail_prompt": "an epic cover"}
    monkeypatch.setattr(ai_providers, "generate_text", lambda **kw: json.dumps(fake))
    conn = _memdb()
    from app.epub_parser import ParsedChapter
    book = repository.create_book(
        conn, title="T", original_filename="t.epub", epub_path="x", patch_size=10,
        chapters=[ParsedChapter(title="C1", text="x " * 40)], background_image_path=None)
    out = ai_content.run_task(conn, book, "youtube_content")
    assert out["result"]["playlist_title"] == "P"
    saved = json.loads(repository.get_book(conn, book.id).ai_content_json)
    assert saved["genre_tags"] == "a, b"
    from app.youtube_metadata import get_book_youtube_config
    assert get_book_youtube_config(conn, book.id)["genre_tags"] == "a, b"
    conn.close()


def test_generate_book_thumbnail_saves_file(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(ai_providers, "generate_image_bytes",
                        lambda **kw: b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048)
    conn = _memdb()
    from app.epub_parser import ParsedChapter
    book = repository.create_book(
        conn, title="T", original_filename="t.epub", epub_path="x", patch_size=10,
        chapters=[ParsedChapter(title="C1", text="x " * 40)], background_image_path=None)
    out = ai_content.generate_book_thumbnail(conn, book, prompt="a mountain, no text")
    assert out["prompt_source"] == "override" and out["saved"] is True
    assert repository.get_book(conn, book.id).ai_thumbnail_path == out["path"]
    conn.close()


def test_register_custom_adapter(monkeypatch):
    calls = {}

    class Dummy(ai_providers.AIProvider):
        def generate_text(self, cfg, **kw):
            calls["model"] = cfg.text_model
            return "dummy-ok"

        def generate_image(self, cfg, **kw):
            return b"img"

    ai_providers.register_ai_provider("dummy", Dummy)
    monkeypatch.setattr(ai_providers, "resolve_provider",
                        lambda _pid=None: ai_providers.ResolvedAIProvider(
                            id="d", adapter="dummy", api_key="k", text_model="m"))
    assert ai_providers.generate_text(system="s", user="u") == "dummy-ok"
    assert calls["model"] == "m"


def test_is_configured_false_without_key(monkeypatch):
    import os
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AI_API_KEY", raising=False)
    monkeypatch.setattr(settings, "ai_api_providers", "")
    monkeypatch.setattr(settings, "ai_base_url", "")
    assert ai_providers.is_configured("openai") is False
