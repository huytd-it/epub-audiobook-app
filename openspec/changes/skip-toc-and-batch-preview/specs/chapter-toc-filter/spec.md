## ADDED Requirements

### Requirement: TOC chapter detection
The system SHALL detect when a parsed chapter is a table of contents (TOC) page using heuristic signals (very short average line length, high ratio of short lines, or presence of many internal `href` anchors pointing to other chapters in the same book). The detection MUST run during `parse_epub()` and only inspect the candidate chapter, not require any other context.

#### Scenario: First chapter is a TOC page
- **WHEN** `parse_epub()` returns a list whose first chapter contains mostly short lines (e.g. mean line length < 40 chars) and < 10% of the text is non-whitespace prose
- **THEN** the first chapter is removed from the returned list and the remaining chapters keep their original `chapter_index` (not re-numbered)

#### Scenario: First chapter is real content
- **WHEN** the first chapter has long-form paragraphs and does not look like an index
- **THEN** no chapter is removed and the returned list is unchanged

#### Scenario: TOC chapter appears in middle of book
- **WHEN** any non-first chapter matches the TOC heuristic
- **THEN** the chapter is kept as-is (only the leading TOC, when it is the first chapter, is filtered)

### Requirement: TOC filter observability
The system MUST log how many chapters were skipped by the TOC filter at INFO level so the user can verify the behavior during upload.

#### Scenario: One TOC chapter skipped
- **WHEN** the parser removes exactly one TOC chapter
- **THEN** a log line of the form `toc-filter: skipped 1 chapter (was 'Table of Contents')` is emitted

#### Scenario: No TOC chapter detected
- **WHEN** no chapter matches the TOC heuristic
- **THEN** no log line is emitted and the returned list is unchanged

### Requirement: Filter is opt-in by default but configurable
The system MUST skip the leading TOC chapter by default, and MUST allow disabling the filter via a constructor/option for tests and edge cases. The default behavior MUST be safe: never remove the only chapter of a book (if the result would be empty, return the original list).

#### Scenario: Skip disabled in tests
- **WHEN** `parse_epub(path, skip_toc=False)` is called
- **THEN** no TOC filtering is applied and the original list is returned

#### Scenario: Filter would empty the book
- **WHEN** the only chapter of a book matches the TOC heuristic
- **THEN** the chapter is kept and an empty-result guard returns the original list
