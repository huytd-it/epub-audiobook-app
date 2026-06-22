## 1. TOC detection in parser

- [x] 1.1 Add `_is_toc_chapter(chapter: ParsedChapter) -> bool` helper in `app/epub_parser.py` implementing the mean-line-length + short-line-ratio heuristic from design.md (decision 1)
- [x] 1.2 Update `parse_epub()` signature to accept `skip_toc: bool = True` and apply the filter to the first chapter only, with the "empty-result guard" returning the original list if the filter would zero it out
- [x] 1.3 Add `logging.getLogger(__name__).info(...)` call when a TOC chapter is skipped, including the original title
- [x] 1.4 Add unit test `tests/test_toc_filter.py` covering: TOC at index 0 with mean line length < 40, real chapter with long paragraphs, single-chapter book (guard), and `skip_toc=False` opt-out

## 2. Repository helpers for batch fetch

- [x] 2.1 Add `repository.get_chapters_by_indices(conn, book_id, indices: list[int]) -> list[Chapter]` returning chapters in ascending `chapter_index` order, silently skipping unknown indices
- [x] 2.2 Add `repository.get_chapter_text(conn, book_id, chapter_index) -> str | None` returning full `text` for one chapter, or `None` if not found

## 3. Batch preview API endpoints

- [x] 3.1 Add `GET /books/{book_id}/chapters/preview` in `app/routes/books.py` accepting `ids` (comma-separated) and `preview_chars` (int, default 500) query params; returns JSON list of `{chapter_index, title, char_count, text_excerpt}`
- [x] 3.2 Add `GET /books/{book_id}/chapters/{chapter_index}/text` returning raw text with `text/plain` content type
- [x] 3.3 Add `GET /books/{book_id}/chapters/preview-ui` (HTML, server-rendered) accepting `ids` and optional `range_start`/`range_end` query params; resolves range to indices, fetches full text per chapter, renders Jinja template
- [x] 3.4 Add 404 handling when `book_id` does not exist and 400 when `ids` is missing on the JSON endpoint

## 4. UI templates and styling

- [x] 4.1 Create `app/templates/chapter_preview.html` extending `base.html`; form with range inputs (start/end), checkbox grid for individual selection, and stacked chapter blocks with index + title + `<pre>` text
- [x] 4.2 Add CSS rules in `app/static/style.css` for `.chapter-preview-block`, `.chapter-preview-meta`, and the checkbox grid
- [x] 4.3 Update `app/templates/book_detail.html` to add a "Preview chapters" link/button visible on every book regardless of TTS state

## 5. Tests and verification

- [x] 5.1 Add `scripts/test_toc_and_preview.py` that parses a sample EPUB and prints: which chapter was skipped (if any), the chapter list with char counts, and a quick JSON call to the new preview API
- [x] 5.2 Run `python -m pytest tests/` and ensure existing tests still pass (no DB schema changes, no breaking changes to `parse_epub()` signature except new kwarg with default)
- [x] 5.3 Manual smoke test: upload an EPUB with TOC, verify the TOC chapter is skipped (check `chapter` table count and patch ranges), open the new preview UI from book detail, verify range + checkbox selection both work
