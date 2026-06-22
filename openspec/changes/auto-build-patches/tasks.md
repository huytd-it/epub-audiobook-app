## 1. Repository: auto-build function

- [x] 1.1 Add `repository.auto_build_patches(conn, book_id, start_chapter, end_chapter=None, patch_size=None) -> list[Patch]` in `app/repository.py`. Resolve `end_chapter` to `MAX(chapter_index)` if None, `patch_size` to `book.patch_size` if None. Build the list of included chapter indices in `[start, end]` with `is_excluded=0`, chunk into ranges of `patch_size`, then delegate to `rebuild_patches(conn, book_id, ranges, reset_done=True)`. Raise `ValueError` with specific messages for: `start_chapter < 0`, `start_chapter > max`, `end_chapter < start_chapter`, `patch_size < 1`, no included chapters in range.

## 2. API endpoint

- [x] 2.1 Add `POST /books/{book_id}/patches/auto-build` in `app/routes/books.py` accepting form fields `start_chapter` (int, required), `end_chapter` (int, optional), `patch_size` (int, optional). Catch `ValueError` from repository and raise `HTTPException(400, detail=str(exc))`. Return `RedirectResponse` to `/books/{book_id}` on success. Use `from None` to preserve the chain.

## 3. UI form

- [x] 3.1 Add a new section "Auto-build patches" in `app/templates/book_detail.html` between the "Patch builder" link and the patches table. Form has 3 inputs: `start_chapter` (number, required), `end_chapter` (number, optional, placeholder="to last chapter"), `patch_size` (number, default = `book.patch_size`). Submit button "Auto-build". Method POST, action `/books/{book.id}/patches/auto-build`.
- [x] 3.2 Add CSS for `.auto-build-form` in `app/static/style.css` (flex row, gap, matching the existing replace-rule-form style).

## 4. Tests

- [x] 4.1 Add `tests/test_auto_build.py` with unit tests for `auto_build_patches` covering: basic chunking, default end_chapter and patch_size, exclude-skip, end < start rejected, start out of bounds, all-excluded range, patch_size < 1, last chunk smaller than patch_size.
- [x] 4.2 Add route test in `tests/test_patch_preview_actions.py` (or new file) covering: POST to `/books/{id}/patches/auto-build` with form data, success redirect (303), validation error returns 400.
- [x] 4.3 Run `python -m pytest tests/` and ensure all 35+ existing tests still pass plus the new ones.
