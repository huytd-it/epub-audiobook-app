## ADDED Requirements

### Requirement: Chapter exclude toggle
The system SHALL allow the user to mark individual chapters as `excluded`. Excluded chapters SHALL NOT appear in any patch's chapter range and SHALL NOT contribute text to TTS synthesis. The default state for a newly uploaded chapter is `is_excluded = 0`. Excluding a chapter that is already inside an existing patch range MUST leave the patch definition in place but the patch MUST skip the excluded chapter when computing its text.

#### Scenario: Exclude single chapter via API
- **WHEN** the user POSTs `/books/{book_id}/chapters/{chapter_index}/exclude` for a chapter that is not currently excluded
- **THEN** the chapter's `is_excluded` is set to 1 and a 204 response is returned

#### Scenario: Re-include a previously excluded chapter
- **WHEN** the user POSTs the same endpoint with `excluded=false`
- **THEN** the chapter's `is_excluded` is set to 0

#### Scenario: Excluded chapter omitted from patch text
- **WHEN** a patch spans chapters 1–5 but chapter 3 is excluded
- **THEN** the text fed to TTS for that patch is the concatenation of chapters 1, 2, 4, 5 in that order

### Requirement: Custom patch definition
The system SHALL allow the user to define patches by submitting a list of `(chapter_start, chapter_end)` ranges that are NOT constrained to be contiguous, equal-sized, or non-overlapping. The system MUST replace the existing auto-generated patch list with the user-defined one when the user calls the rebuild endpoint. The default state on upload remains the auto-grouped patch list from `group_into_patches(patch_size)`.

#### Scenario: Rebuild patches with custom ranges
- **WHEN** the user POSTs `/books/{book_id}/patches/rebuild` with `{"ranges": [[0, 5], [10, 12], [50, 50]]}`
- **THEN** the patch table is replaced with three patches having those exact ranges; existing audio for the same ranges is preserved (or reset to `pending` if the user passes `reset_done=true`)

#### Scenario: Overlapping ranges rejected
- **WHEN** the user submits ranges that overlap (e.g. `[[0, 5], [3, 7]]`)
- **THEN** the API returns 400 with a message identifying the overlapping range pair

#### Scenario: Range references an excluded chapter
- **WHEN** a submitted range includes an excluded chapter
- **THEN** the API returns 400 and no patch list is modified

### Requirement: Patch builder UI
The system SHALL render a `patch_builder` page at `GET /books/{book_id}/patches/build` that shows every chapter with an "exclude" checkbox and a multi-range form for defining custom patches. Submitting the form MUST call the rebuild endpoint above.

#### Scenario: Render builder with current state
- **WHEN** the user opens the patch builder for a book with 20 chapters
- **THEN** the page lists all 20 chapters with their current exclude state and shows the current patch ranges in editable form fields

#### Scenario: Save changes
- **WHEN** the user excludes chapter 0, then submits ranges `[[1, 5], [10, 19]]`
- **THEN** the page redirects to book detail and the patch table reflects the new definitions with chapter 0 absent
