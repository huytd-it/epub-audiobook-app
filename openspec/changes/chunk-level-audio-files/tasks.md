## 1. Configuration

- [x] 1.1 Add `tts_write_chunk_files: bool = True` field to `app/config.py` Settings class, mapped from `TTS_WRITE_CHUNK_FILES` env var

## 2. Audio merge: chunk file helpers

- [x] 2.1 Add `merge_chunk_files_to_patch(chunk_paths: list[str], out_path: str) -> None` in `app/audio_merge.py` — stream each chunk WAV block-by-block (65536 frames) into the output patch WAV, reusing the `merge_patches_to_final` pattern
- [x] 2.2 Add `cleanup_chunk_dir(chunk_dir: str) -> None` in `app/audio_merge.py` — delete the `_chunks` directory and all contents via `shutil.rmtree`, log a warning on failure

## 3. Worker: per-chunk WAV writing and deferred merge

- [x] 3.1 Modify `worker._synthesize()` to create the `{patch_id}_chunks` directory when `settings.tts_write_chunk_files` is True, write each chunk's `np.ndarray` as `chunk_{index:03d}.wav` immediately after synthesis, emit `event=chunk.written` log with `patch_id`, `chunk_index`, `path`
- [x] 3.2 After all chunks synthesized (toggle on), call `merge_chunk_files_to_patch()` with sorted chunk paths to produce the final patch WAV, emit `event=chunk.merged` log
- [x] 3.3 After successful merge, call `cleanup_chunk_dir()` to delete the `_chunks` directory, emit `event=chunk.cleaned` log
- [x] 3.4 On any exception during chunk writing, merge, or cleanup, delete the `_chunks` directory (defensive cleanup) and re-raise so `mark_patch_failed` runs
- [x] 3.5 When `settings.tts_write_chunk_files` is False, use the existing in-memory `concat_chunks_to_wav` path unchanged

## 4. Tests

- [x] 4.1 Add `tests/test_chunk_files.py` with unit tests: chunk WAV write + read round-trip, merge from chunk files produces correct concatenated audio, cleanup removes directory, toggle-off falls back to in-memory path
- [x] 4.2 Add worker integration test: synthesize a small patch with toggle on, verify chunk files appear, patch WAV is correct, and `_chunks` directory is removed
- [x] 4.3 Run `python -m pytest tests/` and verify all existing tests pass plus new ones
