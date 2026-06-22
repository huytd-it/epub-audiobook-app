## 1. DB schema and model

- [x] 1.1 Add `chapter.is_excluded INTEGER NOT NULL DEFAULT 0` column with migration in `app/db.py:_migrate()`
- [x] 1.2 Add `text_replace_rule` table schema in `app/db.py` (id, book_id, find, replace, is_regex, position; FK to book ON DELETE CASCADE)
- [x] 1.3 Add `TextReplaceRule` dataclass in `app/models.py`

## 2. Chapter exclude repository

- [x] 2.1 Add `repository.set_chapter_excluded(conn, book_id, chapter_index, excluded: bool) -> bool` in `app/repository.py`
- [x] 2.2 Add `repository.list_included_chapters(conn, book_id) -> list[Chapter]` helper

## 3. Replace rules repository

- [x] 3.1 Add `repository.list_replace_rules(conn, book_id) -> list[TextReplaceRule]` (ORDER BY position, id)
- [x] 3.2 Add `repository.create_replace_rule(conn, book_id, find, replace, is_regex, position) -> TextReplaceRule` (validate regex compile, raise ValueError on failure)
- [x] 3.3 Add `repository.update_replace_rule(conn, rule_id, ...) -> TextReplaceRule | None` (partial update + revalidate regex)
- [x] 3.4 Add `repository.delete_replace_rule(conn, rule_id) -> bool`
- [x] 3.5 Add `repository.apply_replace_rules(text: str, rules: list[TextReplaceRule]) -> str` pure function in `app/repository.py` (or new `app/text_transform.py`) that iterates rules in position order, applies literal or regex (using `re.sub` with compiled pattern)
- [x] 3.6 Add `repository.reset_done_patches_for_book(conn, book_id) -> int` that sets done patches back to pending, clears audio_path/error_message/final_audio_path (used by rule mutation handlers)

## 4. Custom patch rebuild

- [x] 4.1 Add `repository.rebuild_patches(conn, book_id, ranges: list[tuple[int, int]], reset_done: bool) -> list[Patch]` that validates (no overlap, no excluded chapter in any range, range valid), deletes existing patches, inserts new ones in submitted order, resets book state
- [x] 4.2 Add `repository.get_patch(conn, patch_id) -> Patch | None` if not already present (or reuse existing)

## 5. Patch text builder

- [x] 5.1 Add `repository.build_patch_text(conn, patch: Patch) -> str` in `app/repository.py` that fetches chapters in range, sorts by chapter_index, filters out `is_excluded=1`, joins with `\n\n`, then calls `apply_replace_rules` with the book's rules
- [x] 5.2 Update `app/worker.py` to call `build_patch_text` instead of `get_chapters_in_range` + manual join

## 6. API endpoints

- [x] 6.1 Add `POST /books/{book_id}/chapters/{chapter_index}/exclude` (form: `excluded=true|false`) returning 204
- [x] 6.2 Add CRUD endpoints for replace rules: `GET/POST /books/{book_id}/replace-rules`, `PUT/DELETE /books/{book_id}/replace-rules/{rule_id}`; each mutation calls `reset_done_patches_for_book`
- [x] 6.3 Add `POST /books/{book_id}/patches/rebuild` accepting `{"ranges": [[start, end], ...], "reset_done": bool}`; returns updated patch list as JSON
- [x] 6.4 Add `GET /books/{book_id}/patches/{patch_id}/text` returning `text/plain` from `build_patch_text`
- [x] 6.5 Add `GET /books/{book_id}/patches/{patch_id}/audio` returning the patch's `.wav` file via `FileResponse` (404 if status != done or file missing)
- [x] 6.6 Add `GET /books/{book_id}/patches/build` rendering `patch_builder.html` with current chapter list and patch ranges
- [x] 6.7 Add `POST /books/{book_id}/patches/build` form handler that parses exclude checkboxes + range fields and calls rebuild_patches

## 7. Templates and UI

- [x] 7.1 Create `app/templates/patch_builder.html` extending `base.html`: list of chapters with exclude checkboxes, multi-range form (add/remove range rows), submit button
- [x] 7.2 Update `app/templates/book_detail.html`: add "Patch builder" link, "Text replace rules" section (form to add rule + table of existing rules with delete buttons), and in the patch table add "Preview text" (`<details>`) and "Play" (`<audio controls>` only for done patches)
- [x] 7.3 Add CSS for `.replace-rule-table`, `.patch-preview-details`, `.patch-audio-player` in `app/static/style.css`

## 8. Tests and verification

- [x] 8.1 Add `tests/test_chapter_exclude.py`: unit tests for set_chapter_excluded, list_included_chapters
- [x] 8.2 Add `tests/test_replace_rules.py`: tests for create/update/delete with validation (invalid regex, empty find), apply_replace_rules (literal, regex, ordering, ties)
- [x] 8.3 Add `tests/test_patch_rebuild.py`: tests for rebuild_patches (valid ranges, overlap rejection, excluded chapter rejection), build_patch_text (excluded chapters skipped, rules applied)
- [x] 8.4 Add `tests/test_patch_preview_actions.py`: route tests for /patches/{id}/text, /patches/{id}/audio (done vs not-done), /patches/build GET/POST, exclude endpoint
- [x] 8.5 Run `python -m pytest tests/` and ensure all tests pass (existing + new); verify worker still functions (smoke test of the full upload → patch → synthesize loop with stub TTS)
