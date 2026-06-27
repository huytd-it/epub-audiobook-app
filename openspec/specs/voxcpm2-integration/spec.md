# VoxCPM2 TTS Engine — Integration Spec

## Overview

The application uses **VoxCPM2** (`openbmb/VoxCPM2`) as its text-to-speech engine via the `voxcpm` PyPI package. The engine is wrapped in a lazy-loaded singleton (`VoxCPMEngine`) that is created once at app startup and passed to the background worker. All TTS synthesis runs sequentially in a worker thread to keep GPU access single-threaded.

---

## Architecture

### Dependency chain

```
pyproject.toml          # voxcpm in [project.optional-dependencies] tts
     ↓
app/tts_engine.py       # VoxCPMEngine — wraps VoxCPM.from_pretrained()
     ↓
app/worker.py           # PatchWorker._synthesize() — calls engine.synthesize_patch()
     ↓
app/chunker.py          # split_into_tts_chunks() — splits patch text into ~400-char chunks
     ↓
app/audio_merge.py      # concat_chunks_to_wav() / merge_chunk_files_to_patch() — assemble final WAV
```

### Engine lifecycle (app/main.py:72-83)

```
lifespan startup
  └─ VoxCPMEngine()                  ← constructor only stores config, doesn't load model
  └─ PatchWorker(conn, engine, ...)  ← worker takes ownership
  └─ asyncio.create_task(run_forever)
       └─ _process(patch)
            └─ asyncio.to_thread(_synthesize, patch)
                 └─ engine.synthesize_patch(text, ...)
                      └─ split_into_tts_chunks(text)
                      └─ for each chunk → engine.synthesize_chunk(chunk, ...)
                           └─ _ensure_loaded() ← downloads model weights on first call
                           └─ self._model.generate(text, ...) ← actual GPU inference
```

---

## Requirement: Python version constraint

VoxCPM2 requires **Python >= 3.10, < 3.13**. The project MUST enforce this via `pyproject.toml`:

```toml
requires-python = ">=3.10,<3.13"
```

#### Scenario: Wrong Python version
- **WHEN** the app is started with Python 3.13+
- **THEN** `pip install -e .` MUST fail with a Python version mismatch error (PEP 440)

---

## Requirement: Lazy model loading

`VoxCPMEngine` MUST NOT call `VoxCPM.from_pretrained()` in the constructor. The model MUST be loaded on the first call to any method that interacts with the model (`synthesize_chunk`, `sample_rate`). The heavy import (`from voxcpm import VoxCPM`) MUST also be deferred to `_ensure_loaded()`.

```python
# tts_engine.py — correct pattern
class VoxCPMEngine:
    def __init__(self, ...):
        self._model = None          # no model load

    def _ensure_loaded(self):
        if self._model is None:
            from voxcpm import VoxCPM     # deferred import
            self._model = VoxCPM.from_pretrained(...)
```

#### Scenario: Engine created but never used (worker disabled)
- **WHEN** `ENABLE_WORKER=false` and the server starts
- **THEN** `VoxCPMEngine()` is constructed but `_ensure_loaded()` is never called; no model weights are downloaded, no GPU memory is consumed

#### Scenario: First synthesis triggers model load
- **WHEN** the first patch chunk is synthesized
- **THEN** `_ensure_loaded()` downloads the model weights (if not cached) and loads them into GPU memory; subsequent calls reuse the loaded model

#### Scenario: Model download failure
- **WHEN** `VoxCPM.from_pretrained()` raises (e.g., network error, disk full, CUDA OOM)
- **THEN** the exception propagates to `_synthesize()` in `worker.py`, which catches it and marks the patch as `failed`; the worker does not crash and continues to the next patch

---

## Requirement: Sequential single-thread GPU access

The `VoxCPMEngine` instance is a **process-level singleton** — only one instance is created per app process. All synthesis calls MUST run in the worker thread (via `asyncio.to_thread`) so that GPU access is strictly sequential. The FastAPI event loop MUST NOT call `synthesize_chunk` or `synthesize_patch` directly.

#### Scenario: Simultaneous patches
- **WHEN** two patches are pending in the queue
- **THEN** the worker synthesizes them one at a time; the second patch starts only after the first is fully done (written to WAV and marked `done` in DB)

#### Scenario: HTTP request during synthesis
- **WHEN** the worker is synthesizing a patch and a user visits the web UI
- **THEN** the page loads normally; the event loop is not blocked because synthesis runs in a thread

---

## Requirement: Voice consistency via reference audio

Without a reference clip, VoxCPM2 samples a **fresh random voice** per `generate()` call. To produce a consistent narrator voice across all chunks of all patches for a book, the same `reference_wav_path` and `prompt_text` MUST be passed to every `synthesize_chunk` call.

### Book upload flow (`app/routes/books.py:44-110`)

```
upload form:
  ├─ epub_file (required)
  ├─ patch_size (default 10)
  ├─ background_image (optional)
  ├─ voice_clip (optional, .wav file)
  └─ voice_transcript (optional, text of the voice_clip)
```

The uploaded voice clip and transcript are stored in the `book` table as `voice_clip_path` and `voice_transcript`. The worker reads them in `_synthesize()` and passes them to every chunk:

```python
# worker.py:209-210
ref_wav = book.voice_clip_path if book else None
ref_text = book.voice_transcript if book else None
```

When both `reference_wav_path` and `prompt_text` are provided, the engine uses **Ultimate Cloning** mode: the same clip is passed as both reference and prompt, which yields closer timbre and prosody matching than `reference_wav_path` alone (`tts_engine.py:46-50`).

#### Scenario: Voice clip uploaded
- **WHEN** a book is uploaded with a voice clip and transcript
- **THEN** every chunk in every patch of that book is synthesized with `reference_wav_path=voice_clip_path` and `prompt_text=voice_transcript`; the narrator voice is consistent end-to-end

#### Scenario: No voice clip
- **WHEN** a book is uploaded without a voice clip
- **THEN** every chunk is synthesized without reference audio; VoxCPM2 samples a random voice per chunk, resulting in inconsistent narration

#### Scenario: Voice clip without transcript
- **WHEN** a book is uploaded with a voice clip but no transcript
- **THEN** `prompt_text` is `None`; the engine uses `reference_wav_path` alone (no Ultimate Cloning); voice consistency is preserved but timbre matching is weaker

---

## Requirement: Chunk-level text splitting

Patch text (which may be thousands of characters) MUST be split into TTS-sized chunks before synthesis. The chunker (`app/chunker.py:split_into_tts_chunks`) uses a greedy algorithm:

1. Split text on double-newlines into paragraphs.
2. Paragraphs under `max_chars` are kept whole; longer paragraphs are split on sentence boundaries (`[.!?…]\s+`).
3. Pieces are packed sequentially into chunks such that no chunk exceeds `max_chars`.

The maximum chunk size is configured by `tts_max_chars` in `Settings` (default 400). This value is an **untested assumption** about VoxCPM2's practical per-call input limit.

```python
# chunker.py:26-51
def split_into_tts_chunks(text: str, max_chars: int = 400) -> list[str]:
    ...
```

#### Scenario: Short patch text
- **WHEN** the patch text is 150 characters
- **THEN** `split_into_tts_chunks` returns a single chunk with the full text

#### Scenario: Long patch text
- **WHEN** the patch text is 1,200 characters with `max_chars=400`
- **THEN** the chunker produces 3-4 chunks, each ≤400 characters; no sentence is split across chunks

#### Scenario: Single very long paragraph
- **WHEN** a paragraph is 2,000 characters without sentence breaks
- **THEN** the paragraph is treated as one piece, which will exceed `max_chars`; the chunker packs it into a single oversized chunk (this is a known limitation — sentences cannot be split mid-sentence, and the paragraph has no sentence boundaries)

---

## Requirement: Two synthesis output modes

The system supports two modes controlled by `Settings.tts_write_chunk_files` (default `True`):

### Mode A: In-memory (tts_write_chunk_files=False)

All chunks are synthesized in memory, concatenated, and written directly to the final patch WAV:

```python
wavs = engine.synthesize_patch(patch_text, ...)
audio_merge.concat_chunks_to_wav(wavs, engine.sample_rate, audio_path)
```

`concat_chunks_to_wav` uses `numpy.concatenate` and writes a single WAV via `soundfile.write`. This is safe because patch chunks are small (seconds each).

### Mode B: Per-chunk WAV files (tts_write_chunk_files=True, default)

Each chunk is written as a separate WAV file, then merged:

```python
chunk_dir = book_dir / f"{patch.id}_chunks"
chunk_dir.mkdir(parents=True, exist_ok=True)
for i, chunk_text in enumerate(chunks):
    arr = engine.synthesize_chunk(chunk_text, ...)
    chunk_path = chunk_dir / f"chunk_{i:03d}.wav"
    sf.write(chunk_path, arr, engine.sample_rate)
# Then merge all chunk WAVs into the patch WAV
audio_merge.merge_chunk_files_to_patch(chunk_paths, audio_path)
# Finally delete the chunk directory
audio_merge.cleanup_chunk_dir(str(chunk_dir))
```

#### Scenario: In-memory mode
- **WHEN** `TTS_WRITE_CHUNK_FILES=false`
- **THEN** no chunk WAV files are written to disk; the final patch WAV is produced entirely in memory

#### Scenario: Chunk file mode (default)
- **WHEN** `TTS_WRITE_CHUNK_FILES=true` (or unset)
- **THEN** each chunk produces a file at `data/books/{book_id}/patches/{patch_id}_chunks/chunk_{index:03d}.wav`; after synthesis, all chunk files are merged into the patch WAV and the chunk directory is deleted

#### Scenario: Synthesis failure in chunk file mode
- **WHEN** chunk 3 of a 5-chunk patch fails to synthesize
- **THEN** the exception propagates; the `finally` block in the worker catches it and calls `cleanup_chunk_dir`; already-written chunks 0-2 are deleted

---

## Requirement: Final audio merge

When all patches for a book are done (`all_patches_done=True`), the worker automatically merges all patch WAV files into a single `final.wav`:

```python
# worker.py:262-276
def _merge_final_audio(self, book_id):
    patch_wav_paths = [p.audio_path for p in patches if p.audio_path]
    if len(patch_wav_paths) != len(patches):
        return  # defensive guard
    final_path = book_dir / "final.wav"
    audio_merge.merge_patches_to_final(patch_wav_paths, final_path)
    repository.set_book_final_audio(conn, book_id, final_path)
```

The merge uses **block-by-block streaming I/O** (65536 frames per block) via `soundfile.SoundFile` to avoid loading the entire audiobook into memory.

#### Scenario: Full book completes
- **WHEN** all patches for a book are marked `done`
- **THEN** the worker merges all patch WAVs into `data/books/{book_id}/final.wav` in chapter order (patch_index order)

#### Scenario: Patch count mismatch
- **WHEN** a patch has no `audio_path` despite being `done` (should not happen, defensive guard)
- **THEN** `_merge_final_audio` returns early; the book is not finalized; the error is visible via the patch's status in the UI

---

## Requirement: CUDA memory management

VoxCPM2 requires approximately **8 GB VRAM** for inference. The system MUST NOT attempt to run multiple model instances concurrently. The model MUST remain loaded in GPU memory for the lifetime of the worker.

#### Scenario: Insufficient VRAM
- **WHEN** the GPU has <8 GB VRAM and `load_denoiser=True`
- **THEN** `VoxCPM.from_pretrained()` may raise a CUDA out-of-memory error; the patch fails; the worker continues to the next patch
- **MITIGATION:** The app defaults to `load_denoiser=False` to reduce VRAM usage; users with limited VRAM can also change `model_id` in `tts_engine.py:13` to use a smaller variant

#### Scenario: GPU driver or CUDA error
- **WHEN** the CUDA runtime returns an error (e.g., `CUDA_ERROR_ILLEGAL_ADDRESS`) during `model.generate()`
- **THEN** the exception propagates to `_synthesize()`; the patch is marked `failed`; the worker continues; the model remains loaded (if recoverable) or subsequent patches also fail

---

## Requirement: Configurable model parameters

`VoxCPMEngine` exposes the following tunables in its constructor (`tts_engine.py:12-16`):

| Parameter | Default | Description |
|-----------|---------|-------------|
| `model_id` | `"openbmb/VoxCPM2"` | HuggingFace model ID; can be changed to a smaller variant |
| `load_denoiser` | `False` | Load denoiser submodule (disabled to save VRAM) |
| `cfg_value` | `2.0` | Classifier-free guidance scale |
| `inference_timesteps` | `10` | Number of diffusion inference steps (fewer = faster, lower quality) |

These are currently **hardcoded at construction time** in `app/main.py:73` and not exposed via environment variables.

---

## Requirement: Unchanged (inherited) behaviors

These requirements from the existing codebase are not modified by this spec:

1. **Patch text assembly** (`repository.py:531-540`): patch text is built by concatenating included chapter texts with replace rules applied.
2. **Replace rules** (`repository.py:345-433`): text find/replace rules (plain or regex) are applied per-book before TTS synthesis.
3. **Chapter exclusion** (`repository.py:319-327`): excluded chapters are skipped when building patch text.
4. **Queue system** (`worker.py:104-164`): patches are claimed one at a time; book_jobs (video) run only when no patches are pending.
5. **Error recovery** (`repository.py:200-208`): patches left in `processing` on restart are requeued to `pending`.
6. **Video generation** (`video_gen.py`): after final audio is merged and the book has a background image, a `video` book_job is auto-enqueued.
7. **Health endpoint** exposes worker state (idle/busy/paused) and heartbeat.
8. **Queue stats endpoint** shows per-status patch/book_job counts and last errors.

---

## Known pitfalls (do not repeat)

1. **Random voice drift** — Every call to `VoxCPM.generate()` without reference audio produces a different random speaker identity. Always pass `reference_wav_path` (and ideally `prompt_text`) to every chunk of every patch.

2. **tts_max_chars assumption** — The default 400-char limit is untested against the real model. If chunks are too long, VoxCPM2 may truncate or error on input. Test with real model and adjust `tts_max_chars` accordingly.

3. **Synchronous blocking** — `model.generate()` is a synchronous CUDA call. Running it on the asyncio event loop would freeze HTTP responses. It MUST always run via `asyncio.to_thread()`.

4. **Singleton GPU ownership** — Creating a second `VoxCPMEngine` instance would load the model twice, doubling VRAM usage and causing CUDA OOM on consumer GPUs. The lifespan creates exactly one instance.

5. **Chunk file cleanup on failure** — In chunk-file mode, if synthesis or merge fails, the temporary `_chunks` directory must be deleted to avoid orphaned files accumulating on disk. The `finally` block in `_synthesize()` (line 251-253) handles this.

6. **Surrogate characters** — Text containing surrogate pairs or control characters can crash the tokenizer. The `clean_surrogate_characters` fix was applied to sanitize input before chunking (see commit `8485728`).

7. **No subprocess isolation** — VoxCPM2 runs in-process (same Python process). A crash in the CUDA extension can bring down the entire server. The `asyncio.to_thread` approach does not protect against this — it only keeps the event loop responsive. Future work may consider subprocess isolation.
