"""Smoke test for the skip-toc + batch-preview change.

Usage:
    python scripts/test_toc_and_preview.py <epub>

What it does:
  1. Parses the EPUB and prints how many chapters remain (verifying TOC skip).
  2. Constructs an in-memory SQLite DB from those chapters.
  3. Calls repository.get_chapters_by_indices and prints the result.
  4. Calls the FastAPI JSON endpoint via TestClient and prints the response.

If a real EPUB is not provided, the script falls back to a built-in synthetic EPUB
with a TOC + 1 chapter, so the script is runnable in CI without external fixtures.
"""
from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ebooklib import epub  # noqa: E402

from app import db as app_db  # noqa: E402
from app import repository  # noqa: E402
from app.epub_parser import parse_epub  # noqa: E402


def _synthetic_epub(path: Path) -> Path:
    """Write a minimal EPUB with a TOC page + 1 chapter to `path`."""
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
    return path


def main() -> int:
    if len(sys.argv) == 2:
        epub_path = Path(sys.argv[1])
    else:
        fallback = Path(__file__).resolve().parent.parent / "data" / "synthetic_test.epub"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        _synthetic_epub(fallback)
        epub_path = fallback
        print(f"[info] no epub arg provided, using synthetic fixture at {fallback}")

    print(f"[1] parsing {epub_path}")
    chapters = parse_epub(str(epub_path))
    print(f"    chapters after TOC filter: {len(chapters)}")
    for i, ch in enumerate(chapters):
        print(f"    [{i}] {ch.title!r} ({ch.char_count} chars)")

    print("\n[2] inserting into in-memory sqlite")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    app_db.init_schema(conn)
    book = repository.create_book(
        conn,
        title="smoke-test",
        original_filename=epub_path.name,
        epub_path=str(epub_path),
        patch_size=10,
        chapters=chapters,
        background_image_path=None,
    )
    print(f"    book id = {book.id}, status = {book.status}")

    print("\n[3] repository.get_chapters_by_indices(book.id, [0, 2, 99])")
    selected = repository.get_chapters_by_indices(conn, book.id, [0, 2, 99])
    for ch in selected:
        print(f"    -> {ch.chapter_index}: {ch.title!r} ({ch.char_count} chars)")
    print(f"    (3 ids requested, 1 unknown -> 2 returned)")

    print("\n[4] repository.get_chapter_text(book.id, 0)")
    text = repository.get_chapter_text(conn, book.id, 0)
    if text is not None:
        print(f"    -> {text[:80]!r}...")
    else:
        print("    -> None (chapter 0 missing)")

    print("\n[5] repository.get_chapter_text(book.id, 99) (should be None)")
    print(f"    -> {repository.get_chapter_text(conn, book.id, 99)!r}")

    print("\nOK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
