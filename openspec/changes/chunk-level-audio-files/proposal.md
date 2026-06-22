## Why

Currently when the worker synthesizes a patch, each TTS chunk's audio is held as a NumPy array in memory and all chunks are merged directly into the final patch WAV — there is no way to inspect individual chunk audio files. Users (and developers) cannot verify synthesis quality per chunk nor monitor detailed processing progress. Saving each chunk as a temporary WAV file during synthesis and cleaning them up after merge gives visibility into the TTS pipeline without permanently bloating storage.

## What Changes

- **Chunk-level WAV files**: During patch synthesis, write each chunk's NumPy array to a temporary WAV file inside `data/books/{book_id}/patches/{patch_id}_chunks/` (e.g., `chunk_000.wav`, `chunk_001.wav`) immediately after the TTS generator returns.
- **Deferred merge**: After all chunks for a patch are synthesized, merge the chunk WAV files into the patch WAV using streaming block-by-block merge (reuse the existing `merge_patches_to_final` pattern), then delete the temporary chunk directory.
- **No schema changes**: The chunk files are ephemeral — no DB columns, no tracking. They exist only from the moment each chunk finishes synthesis until the patch merge completes.
- **Preserved in-memory path for fast path**: The `synthesize_patch()` method may optionally skip writing chunk files when a flag is off (e.g., production or disk-space-constrained), defaulting to writing them.

## Capabilities

### New Capabilities
- `chunk-level-audio-files`: Temporarily persist each TTS chunk as an individual WAV file under the patch's working directory during synthesis, then clean them up after the patch WAV is merged. Exposed via a config toggle and observable as files on disk for quality inspection.

### Modified Capabilities
- _(none — no existing spec requirement changes)_

## Impact

- **app/tts_engine.py**: `synthesize_patch()` adjusted to emit chunk WAV file paths alongside the audio arrays (or instead of them), controlled by a new `write_chunk_files` parameter.
- **app/audio_merge.py**: New `merge_chunk_files_to_patch()` function that streams chunk WAV files into a single patch WAV (mirrors `merge_patches_to_final`), plus a `cleanup_chunk_dir()` helper to delete the temporary directory.
- **app/worker.py**: `_synthesize()` calls the new chunk-file-aware synthesis path; after merge, cleans up the chunk directory.
- **app/config.py**: New `tts_write_chunk_files` boolean setting (default `True`) to toggle the feature.
