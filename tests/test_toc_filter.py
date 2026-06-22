"""Unit tests for the TOC-filter heuristic in app.epub_parser.

The heuristic runs in pure Python over ParsedChapter objects, so we test it directly
without needing real EPUB fixtures.
"""
from ebooklib import epub

from app.epub_parser import ParsedChapter, _is_toc_chapter, parse_epub


def _toc_like_text(n_lines: int = 20) -> str:
    """Simulate a TOC page: one short line per chapter (title + page number)."""
    return "\n".join(f"Chapter {i} ............ {i * 10 + 3}" for i in range(1, n_lines + 1))


def _prose_text() -> str:
    """A few long-form paragraphs with realistic prose line lengths."""
    paragraph = (
        "It was a bright cold day in April, and the clocks were striking thirteen. "
        "Winston Smith, his chin nuzzled into his breast in an effort to escape the "
        "vile wind, slipped quickly through the glass doors of Victory Mansions, "
        "though not quickly enough to prevent a swirl of gritty dust from entering "
        "along with him."
    )
    return "\n\n".join(paragraph for _ in range(6))


def test_toc_chapter_detected_when_mean_line_length_is_low():
    chapter = ParsedChapter(title="Table of Contents", text=_toc_like_text(20))
    assert _is_toc_chapter(chapter) is True


def test_real_chapter_with_long_paragraphs_is_not_detected_as_toc():
    chapter = ParsedChapter(title="Chapter 1", text=_prose_text())
    assert _is_toc_chapter(chapter) is False


def test_chapter_with_very_few_lines_is_not_classified_as_toc():
    chapter = ParsedChapter(title="Front Matter", text="Short line one.\nShort line two.")
    assert _is_toc_chapter(chapter) is False


def test_mixed_short_and_long_lines_above_short_ratio_threshold():
    """90% short lines triggers even with some long lines mixed in."""
    lines = ["Chapter i ......... 1"] * 9 + ["x" * 200]
    chapter = ParsedChapter(title="Contents", text="\n".join(lines))
    assert _is_toc_chapter(chapter) is True


# --- parse_epub() integration tests (minimal in-memory EPUBs) ---

def _write_epub(toc_html: str, chapter_html: str, path: str) -> None:
    book = epub.EpubBook()
    book.set_identifier("test")
    book.set_title("test")
    book.set_language("en")
    c1 = epub.EpubHtml(title="TOC", file_name="toc.xhtml", content=toc_html)
    c2 = epub.EpubHtml(title="Chapter 1", file_name="c1.xhtml", content=chapter_html)
    book.add_item(c1)
    book.add_item(c2)
    book.toc = ()
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", c1, c2]
    epub.write_epub(path, book)


def test_parse_epub_skips_toc_chapter_by_default(tmp_path):
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
    long_para = "x " * 200
    chapter_html = (
        "<html><body>"
        "<h1>Chapter 1</h1>"
        f"<p>{long_para}</p>\n"
        f"<p>{long_para}</p>\n"
        f"<p>{long_para}</p>\n"
        f"<p>{long_para}</p>\n"
        "</body></html>"
    )
    epub_path = tmp_path / "book.epub"
    _write_epub(toc_html, chapter_html, str(epub_path))

    chapters = parse_epub(str(epub_path))
    assert len(chapters) == 1
    assert chapters[0].title == "Chapter 1"


def test_parse_epub_keeps_toc_chapter_when_skip_disabled(tmp_path):
    toc_html = (
        "<html><body>"
        "<h1>Table of Contents</h1>"
        "<p>Chapter 1 ............ 3</p>\n"
        "<p>Chapter 2 ............ 25</p>\n"
        "<p>Chapter 3 ............ 47</p>\n"
        "<p>Chapter 4 ............ 69</p>\n"
        "<p>Chapter 5 ............ 91</p>\n"
        "</body></html>"
    )
    long_para = "x " * 200
    chapter_html = (
        "<html><body>"
        "<h1>Chapter 1</h1>"
        f"<p>{long_para}</p>\n"
        f"<p>{long_para}</p>\n"
        f"<p>{long_para}</p>\n"
        f"<p>{long_para}</p>\n"
        "</body></html>"
    )
    epub_path = tmp_path / "book.epub"
    _write_epub(toc_html, chapter_html, str(epub_path))

    chapters = parse_epub(str(epub_path), skip_toc=False)
    assert len(chapters) == 2
    assert chapters[0].title == "Table of Contents"
    assert chapters[1].title == "Chapter 1"


def test_parse_epub_does_not_empty_a_single_chapter_book(tmp_path):
    """A book whose only chapter looks like a TOC must not be emptied by the filter."""
    toc_html = (
        "<html><body>"
        "<h1>Contents</h1>"
        "<p>Part 1 ............ 1</p>\n"
        "<p>Part 2 ............ 2</p>\n"
        "<p>Part 3 ............ 3</p>\n"
        "<p>Part 4 ............ 4</p>\n"
        "<p>Part 5 ............ 5</p>\n"
        "<p>Part 6 ............ 6</p>\n"
        "</body></html>"
    )
    epub_path = tmp_path / "single.epub"
    book = epub.EpubBook()
    book.set_identifier("test")
    book.set_title("test")
    book.set_language("en")
    c1 = epub.EpubHtml(title="Contents", file_name="c1.xhtml", content=toc_html)
    book.add_item(c1)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", c1]
    epub.write_epub(str(epub_path), book)

    chapters = parse_epub(str(epub_path))
    assert len(chapters) == 1  # empty-result guard kept the only chapter
    assert chapters[0].title == "Contents"

