## Context

Currently the TTS pipeline in `worker._synthesize()` does:
1. `engine.synthesize_patch(text)` → returns `list[np.ndarray]` of audio arrays held entirely in memory
2. `audio_merge.concat_chunks_to_wav(wavs, sr, path)` → `np.concatenate` into one array, `sf.write` to patch WAV
3. Chunk arrays are discarded when `_synthesize` returns — no on-disk trace

The worker is single-threaded for TTS (GPU-bound), so file I/O during chunk writing does not contend with other synthesis. Patches are sequential within a book, so chunk directories are safe from concurrent access.

## Goals / Non-Goals

**Goals:**
- Write each chunk's audio to a temporary WAV file as soon as it finishes synthesis, so users can inspect individual chunks via the filesystem.
- Merge chunk WAV files into the final patch WAV using streamed block-by-block I/O, mirroring the existing `merge_patches_to_final` pattern.
- Delete the temporary chunk directory after the patch WAV is successfully written.
- Provide a config toggle (`tts_write_chunk_files`, default `True`) to disable the feature and fall back to the current in-memory path.

**Non-Goals:**
- No DB schema changes — chunk files are ephemeral and not tracked.
- No UI for listing/downloading individual chunk files (plain filesystem access is the intended interface).
- No incremental progress reporting to the web UI beyond the existing patch-level status.
- No chunk file retention after patch completes (they are always cleaned up).

## Decisions

### 1. Write chunk WAVs immediately after each synthesis call

**Choice:** Modify `synthesize_patch()` (or `worker._synthesize()`) to write each chunk's `np.ndarray` to disk inside the loop, rather than collecting arrays and writing them all at the end.

**Rationale:** Writing per-chunk lets the user inspect intermediate files as synthesis progresses. If a later chunk fails, the earlier chunks are already on disk for inspection (though they'll be cleaned up when the patch fails).

**Alternative considered:** Continue collecting arrays in memory and write all chunk WAVs at the end before merge. Rejected because the user would not see files until all chunks are done, defeating the purpose of progress inspection.

### 2. Chunk file naming and directory layout

**Choice:**
```
data/books/{book_id}/patches/{patch_id}_chunks/
├── chunk_000.wav
├── chunk_001.wav
├── chunk_002.wav
└── ...
```

**Rationale:** Zero-padded sequential numbering (3 digits, supporting up to 999 chunks per patch) gives the user a natural ordering while avoiding surprises from filesystem sort. The `_chunks` suffix on the directory name makes it obvious these are temporary working files, distinct from the final `{patch_id}.wav`.

**Alternative considered:** Timestamp-based names. Rejected because sequential numbering directly maps to chunk order in the text, which is more useful for quality inspection.

### 3. Merge uses streamed block-by-block I/O (reuse `merge_patches_to_final` pattern)

**Choice:** A new `merge_chunk_files_to_patch(paths, out_path)` function that reads each chunk WAV block-by-block (65536 frames) and writes to the patch WAV output, similar to `merge_patches_to_final`.

**Rationale:** Even though individual chunks are small (seconds each), this keeps memory bounded regardless of chunk count and reuses a proven pattern. The function signature differs from `concat_chunks_to_wav` (which takes in-memory arrays) — file-based is the right abstraction here.

### 4. Cleanup is always-on, never retained

**Choice:** After the merge succeeds, `shutil.rmtree(chunk_dir)` deletes the entire chunk directory. On synthesis failure, the chunk directory is also cleaned up in the `except` block.

**Rationale:** The feature is for transient inspection. Permanent retention would bloat storage. If a user wants to keep a chunk, they can copy it before the patch finishes.

**Alternative considered:** A "keep chunks" flag. Rejected as over-engineering — filesystem copy is a one-liner for the user.

### 5. Config toggle `tts_write_chunk_files`

**Choice:** A boolean `Settings` field (default `True`, overridable via `.env` as `TTS_WRITE_CHUNK_FILES=false`).

**Rationale:** Disk-constrained deployments or production runs where the overhead is undesirable can opt out globally. The default keeps the feature on so it benefits new users immediately.

## Risks / Trade-offs

- **Disk I/O overhead**: Writing and later reading each chunk WAV adds filesystem operations that `np.concatenate` didn't. Mitigation: chunk files are small (each corresponds to max 400 chars of text → a few seconds of audio at most), and the single-threaded worker means no I/O contention.
- **Disk space during synthesis**: A patch with many chunks temporarily holds both the chunk WAVs and the final patch WAV on disk. Mitigation: chunks are cleaned up immediately after merge, so peak disk = sum of all chunk WAVs + final patch WAV, which is roughly 2× the patch size — negligible for WAV files in the seconds-to-minutes range.
- **Partial write risk**: If the process crashes between writing chunk files and cleanup, stale `_chunks` directories will accumulate. Mitigation: the worker already has crash recovery (`requeue_stuck_processing`). A future enhancement could add startup cleanup of orphaned `_chunks` directories, but this is deferred — orphaned directories are harmless and small.

## Migration Plan

1. Add `tts_write_chunk_files` to `app/config.py` (default `True`).
2. Add `merge_chunk_files_to_patch()` and `cleanup_chunk_dir()` to `app/audio_merge.py`.
3. Modify `worker._synthesize()` to write chunk WAVs when the toggle is on, then merge from files.
4. Add logging for chunk file operations (`event=chunk.written`, `event=chunk.merged`, `event=chunk.cleaned`).
5. No DB migration needed. Rollback: set `TTS_WRITE_CHUNK_FILES=false` in `.env`.

## Open Questions

- Should we add a startup clean up of orphaned `_chunks` directories? → *Defer*: harmless to leave, can add later if users report clutter.
- Should chunk file sample rate/format match the patch WAV exactly (PCM_16)? → *Decision*: Yes, use the engine's native `sample_rate` and `PCM_16` (matching what `concat_chunks_to_wav` produces via `sf.write` with float32 input).
