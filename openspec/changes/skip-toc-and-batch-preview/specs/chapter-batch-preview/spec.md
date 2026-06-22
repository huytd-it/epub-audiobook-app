## ADDED Requirements

### Requirement: Batch chapter preview API
The system SHALL expose a `GET /books/{book_id}/chapters/preview` endpoint that accepts an `ids` query parameter (comma-separated chapter indices) and returns the matching chapters with their `chapter_index`, `title`, `char_count`, and the first N characters of `text` (default N=500, configurable via `preview_chars`). The endpoint MUST require a valid `book_id` and return 404 if the book does not exist.

#### Scenario: Preview specific chapters
- **WHEN** the user requests `/books/3/chapters/preview?ids=0,2,4&preview_chars=300`
- **THEN** the response is a JSON array of three objects, each with `chapter_index`, `title`, `char_count`, and a 300-char `text` excerpt in original reading order

#### Scenario: No ids provided
- **WHEN** the user requests `/books/3/chapters/preview` without `ids`
- **THEN** the response is 400 with a clear error message indicating that `ids` is required

#### Scenario: Unknown chapter index
- **WHEN** the user requests an `ids` list containing an index that does not exist for that book
- **THEN** unknown indices are silently skipped and only valid chapters are returned

### Requirement: Batch preview UI page
The system SHALL render a preview page at `GET /books/{book_id}/chapters/preview-ui` (or inline section on the book detail page) that lets the user select a contiguous chapter range (start + end inputs) OR a set of individual chapter checkboxes, then shows the selected chapters' text stacked vertically. Each chapter block MUST display the chapter index and title, and the full text (not truncated, unlike the API).

#### Scenario: Range selection
- **WHEN** the user enters start=1 and end=5 and submits
- **THEN** chapters 1 through 5 are shown in order with their full text and titles

#### Scenario: Individual checkbox selection
- **WHEN** the user checks chapters 0, 3, 7
- **THEN** exactly those three chapters are shown, in `chapter_index` ascending order

#### Scenario: Preview of an empty selection
- **WHEN** the user submits with no chapters selected
- **THEN** the page shows a "No chapters selected" message and does not call the API

### Requirement: Preview button on book detail page
The system SHALL add a "Preview chapters" link/button on the book detail page (`book_detail.html`) that navigates to the preview UI for that book, regardless of whether the book has started TTS processing or not.

#### Scenario: Book with no patches yet
- **WHEN** the book has just been uploaded and no patches exist
- **THEN** the "Preview chapters" button is visible and links to the preview UI

#### Scenario: Book with completed audio
- **WHEN** the book is fully processed and final audio exists
- **THEN** the "Preview chapters" button is still visible (text is independent of TTS state)
