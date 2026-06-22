## ADDED Requirements

### Requirement: Config toggle for chunk file writing
The system SHALL expose a `tts_write_chunk_files` boolean configuration setting (default `True`) in `app/config.py`, overridable via the `TTS_WRITE_CHUNK_FILES` environment variable. When `False`, the system SHALL use the existing in-memory-only synthesis path with no chunk files written to disk.

#### Scenario: Toggle enabled (default)
- **WHEN** `TTS_WRITE_CHUNK_FILES` is not set (or set to `true`)
- **THEN** each chunk's audio is written as an individual WAV file during patch synthesis

#### Scenario: Toggle disabled via env
- **WHEN** `TTS_WRITE_CHUNK_FILES=false` is set in `.env`
- **THEN** patch synthesis behaves as before — all chunks held in memory, no temporary WAV files created

### Requirement: Chunk audio files written per-chunk during synthesis
The system SHALL, when `tts_write_chunk_files` is `True`, write each TTS chunk's audio as a WAV file to `data/books/{book_id}/patches/{patch_id}_chunks/chunk_{index:03d}.wav` immediately after the synthesis call for that chunk returns, where `{index}` is the zero-based chunk position within the patch (e.g., `chunk_000.wav`, `chunk_001.wav`). The directory `{patch_id}_chunks` SHALL be created before writing the first chunk.

#### Scenario: First chunk of a patch
- **WHEN** the first chunk of patch 5 for book 3 is synthesized
- **THEN** directory `data/books/3/patches/5_chunks/` is created and `chunk_000.wav` is written with that chunk's audio

#### Scenario: Subsequent chunks in same patch
- **WHEN** the third chunk (index 2) of patch 5 is synthesized
- **THEN** `data/books/3/patches/5_chunks/chunk_002.wav` is written; the directory already exists from the first chunk

#### Scenario: Chunk writing fails
- **WHEN** a chunk's audio cannot be written to disk (e.g., disk full)
- **THEN** the patch synthesis fails with an error, and any chunk files already written for that patch SHALL be cleaned up (the `_chunks` directory deleted)

### Requirement: Patch audio merged from chunk files
The system SHALL, after all chunks for a patch are synthesized and written to disk, merge the chunk WAV files into the final patch WAV at `data/books/{book_id}/patches/{patch_id}.wav` using block-by-block streaming I/O (65536 frames per block), matching the pattern used by `merge_patches_to_final`. Chunk files SHALL be merged in sequential index order.

#### Scenario: Successful merge from chunk files
- **WHEN** all 3 chunks for patch 5 are written to `5_chunks/chunk_000.wav`, `chunk_001.wav`, `chunk_002.wav`
- **THEN** `5.wav` is produced as the concatenation of all three chunk files in order

#### Scenario: Patch with a single chunk
- **WHEN** a patch has only 1 chunk (chunk_000.wav)
- **THEN** the patch WAV is a copy of chunk_000.wav (no concatenation needed)

### Requirement: Chunk directory cleanup after merge
The system SHALL delete the `{patch_id}_chunks` directory and all its contents immediately after the patch WAV is successfully written. The final patch WAV at `data/books/{book_id}/patches/{patch_id}.wav` SHALL be the only audio file remaining in the `patches/` directory for that patch.

#### Scenario: Cleanup after successful merge
- **WHEN** patch 5's chunk files have been merged into `5.wav`
- **THEN** the `5_chunks/` directory no longer exists, and only `5.wav` remains

#### Scenario: Cleanup after merge failure
- **WHEN** the merge step fails (e.g., corrupt chunk file)
- **THEN** the `_chunks` directory is still deleted so orphaned files do not accumulate

### Requirement: Logging for chunk file operations
The system SHALL emit structured log lines (match existing `key=value` format) for chunk file lifecycle events: `event=chunk.written` (after each chunk WAV is written), `event=chunk.merged` (after all chunks are merged into the patch WAV), and `event=chunk.cleaned` (after the chunk directory is deleted). The `event=chunk.written` line SHALL include `patch_id`, `chunk_index`, and `path`.

#### Scenario: Chunk written log
- **WHEN** chunk 2 of patch 5 is written to disk
- **THEN** a log line `event=chunk.written patch_id=5 chunk_index=2 path=data/books/3/patches/5_chunks/chunk_002.wav` is emitted at INFO level

#### Scenario: Merge and cleanup logs
- **WHEN** all chunks of patch 5 are merged and cleaned up
- **THEN** log lines `event=chunk.merged patch_id=5` and `event=chunk.cleaned patch_id=5` are emitted at INFO level
