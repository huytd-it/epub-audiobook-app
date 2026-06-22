## ADDED Requirements

### Requirement: Auto-build patch list from start chapter
The system SHALL provide a `POST /books/{book_id}/patches/auto-build` endpoint accepting `start_chapter` (required int >= 0), `end_chapter` (optional int; if omitted, uses the book's max chapter_index), and `patch_size` (optional int >= 1; if omitted, uses the book's `patch_size`). The endpoint MUST replace the current patch list with one auto-generated from the included (non-excluded) chapters in `[start_chapter, end_chapter]`, grouped sequentially into chunks of `patch_size`. Each chunk becomes one patch whose `chapter_start` is the first chapter_index in that chunk and `chapter_end` is the last. Done patches MUST be reset to `pending` before the rebuild.

#### Scenario: Auto-build with start and default end
- **WHEN** the user POSTs `/books/3/patches/auto-build` with `start_chapter=5` and no `end_chapter` for a book whose max chapter_index is 19 and `patch_size=10`
- **THEN** the patch table is replaced with patches covering chapters 5–14 and 15–19 (chunk 1: 5–14, chunk 2: 15–19)

#### Scenario: Auto-build with explicit end
- **WHEN** the user POSTs `start_chapter=0&end_chapter=9&patch_size=3`
- **THEN** exactly 4 patches are created: chapters 0–2, 3–5, 6–8, 9–9 (the last one may be smaller than patch_size)

#### Scenario: Auto-build skips excluded chapters
- **WHEN** the user POSTs `start_chapter=0&end_chapter=9&patch_size=3` for a book where chapters 3 and 7 are excluded
- **THEN** only non-excluded chapter indices are grouped: included=[0,1,2,4,5,6,8,9] → patches [0–2], [4–6], [8–9]; no patch is created that has a single chapter span (ranges may be non-contiguous but each chunk is contiguous within the included list)

### Requirement: Default values from book state
The system MUST use the book's `patch_size` (from the `book` table) as the default `patch_size` when the form field is omitted or empty. The system MUST use the book's max `chapter_index` as the default `end_chapter` when the form field is omitted or empty.

#### Scenario: Patch size defaults to book.patch_size
- **WHEN** the user POSTs `start_chapter=0` with no `patch_size` for a book whose `patch_size=15`
- **THEN** chunks of 15 chapters are produced

### Requirement: Validation
The system MUST reject auto-build with HTTP 400 when: `start_chapter < 0`, `start_chapter > max chapter_index`, `end_chapter < start_chapter`, `patch_size < 1`, or no chapters in `[start_chapter, end_chapter]` are non-excluded. The error message MUST clearly identify the cause.

#### Scenario: Start out of bounds
- **WHEN** the user POSTs `start_chapter=999` for a book whose max chapter_index is 50
- **THEN** the response is 400 with a message indicating the start chapter is out of bounds

#### Scenario: All chapters in range excluded
- **WHEN** the user POSTs `start_chapter=0&end_chapter=5` for a book where chapters 0–5 are all excluded
- **THEN** the response is 400 with a message like "no included chapters in range"

#### Scenario: end before start
- **WHEN** the user POSTs `start_chapter=10&end_chapter=5`
- **THEN** the response is 400 with a message indicating end must be >= start

#### Scenario: patch_size < 1
- **WHEN** the user POSTs `start_chapter=0&patch_size=0`
- **THEN** the response is 400 with a message indicating patch_size must be >= 1

### Requirement: Auto-build UI form
The system SHALL render a small "Auto-build patches" form on the book detail page (`book_detail.html`) with three inputs: `start_chapter` (number, required), `end_chapter` (number, optional with placeholder "to last chapter"), and `patch_size` (number, defaulting to the book's `patch_size`). Submitting the form MUST POST to the auto-build endpoint and redirect to the book detail page on success, or display the 400 message inline on failure.

#### Scenario: Successful submit from UI
- **WHEN** the user fills in `start_chapter=2` and clicks "Auto-build"
- **THEN** the form submits to the auto-build endpoint and the page reloads with the new patch list

#### Scenario: Validation error shown
- **WHEN** the user fills in `start_chapter=999` and submits
- **THEN** the page shows the 400 error message and the patch list is unchanged
