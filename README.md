# EPUB → Audiobook → Video

Upload a (possibly very large) `.epub`, automatically split it into chapters and ~10-chapter
"patches", synthesize each patch with VoxCPM2 TTS, merge all patches into one final audio file
in chapter order, and optionally mux that audio with a static background image into a simple
video. All patch work is tracked in SQLite so it survives app restarts, and any patch can be
regenerated independently.

## Setup

Requires Python ≥3.10, <3.13 (VoxCPM's constraint). On this machine that's `py -3.11`.

```bash
py -3.11 -m venv .venv
./.venv/Scripts/python.exe -m pip install -e .
```

### Installing the TTS engine

VoxCPM2 needs torch+CUDA installed first (match your CUDA version), then:

```bash
./.venv/Scripts/python.exe -m pip install voxcpm
```

**VRAM note:** VoxCPM2 wants ~8GB VRAM. If your GPU has less (e.g. a 4GB laptop GPU), either
run on CPU (slow) or use a smaller model variant — see the VoxCPM repo for options. The app's
`tts_engine.py` only needs `model_id` changed to point at a different checkpoint.

## Running

```bash
./.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

Open http://localhost:8000/books, upload an epub, watch patches process, regenerate any patch
that fails or needs a redo, then download the final audio/video once all patches are done.

## Phase-by-phase test scripts

Each phase has a standalone CLI script in `scripts/`, useful for debugging without the web UI:

- `scripts/test_epub_parse.py <epub>` — chapter extraction sanity check
- `scripts/test_repo_and_chunker.py <epub>` — DB + patch/chunk generation
- `scripts/test_tts_single_patch.py <epub> [--real]` — TTS wiring (stub by default; `--real` uses
  the actual VoxCPM2 model, requires it to be installed)
- `scripts/test_merge_and_video.py` — audio merge + ffmpeg video muxing
- `scripts/test_worker.py` — full queue worker lifecycle (sequential processing, crash/resume,
  regenerate guard) using a stub TTS engine

## Known limitations (v1)

- `tts_max_chars` (config.py, default 400) is an untested assumption about VoxCPM2's practical
  per-call input length — adjust after real-model testing.
- Progress is tracked per-patch, not per-chunk within a patch.
- A spine document split across multiple chapters via headings is supported; a single logical
  chapter spread across multiple spine files is not — it will appear as multiple chapters.
