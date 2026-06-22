## ADDED Requirements

### Requirement: Preview text action per patch row
The system SHALL add a "Preview text" button to each row of the patch table on the book detail page. Clicking the button MUST open a section (or modal) that displays the text that would be (or was) fed to TTS for that patch - i.e. the concatenation of the patch's chapter texts with the book's active replace rules applied. The button MUST be available regardless of the patch's current status (pending/processing/done/failed).

#### Scenario: Click preview on a pending patch
- **WHEN** the user clicks "Preview text" on a pending patch spanning chapters 1–5
- **THEN** a section opens showing the joined chapter text with all active replace rules applied, prefixed by the chapter index for each block

#### Scenario: Preview on a failed patch
- **WHEN** the user clicks "Preview text" on a failed patch
- **THEN** the same text is shown (preview is independent of TTS outcome) so the user can see what the engine tried to read

### Requirement: Play audio action per patch row
The system SHALL add a "Play" button to each row of the patch table when the patch's `status = done` and `audio_path` is set. The button MUST be disabled (or absent) for patches in any other status. Clicking "Play" MUST load and play the audio file via an inline `<audio controls>` element in the same row.

#### Scenario: Play on a done patch
- **WHEN** the user clicks "Play" on a row whose status is `done`
- **THEN** an inline audio player appears in the row with the patch's `audio_path` as `src`

#### Scenario: Play button absent on pending patch
- **WHEN** a patch is `pending` or `processing`
- **THEN** the "Play" button is not rendered (or rendered as disabled with `aria-disabled="true"`)

### Requirement: Patch text and audio endpoints
The system SHALL expose `GET /books/{book_id}/patches/{patch_id}/text` returning the patch's text (chapter text with replace rules applied) as `text/plain`, and `GET /books/{book_id}/patches/{patch_id}/audio` streaming the patch's `.wav` file with `Content-Type: audio/wav`. Both endpoints MUST return 404 when the patch or book does not exist.

#### Scenario: Fetch patch text
- **WHEN** the user requests `/books/3/patches/7/text`
- **THEN** the response is the joined chapter text with replace rules applied, 200 OK, `text/plain`

#### Scenario: Fetch patch audio
- **WHEN** the user requests `/books/3/patches/7/audio` and patch 7 is done
- **THEN** the response streams the `.wav` file with `Content-Type: audio/wav`, 200 OK

#### Scenario: Audio endpoint on a non-done patch
- **WHEN** the user requests audio for a patch whose status is not `done`
- **THEN** the API returns 404 (no audio available yet)
