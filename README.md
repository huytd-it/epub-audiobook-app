# EPUB → Audiobook → Video

Upload `.epub` files, automatically split into chapters and patches, synthesize with VoxCPM2 TTS, merge into audio, optionally generate video with background images, and auto-upload to YouTube. All work is tracked in SQLite for crash recovery.

## Features

- **EPUB Parsing** — Extract chapters from EPUB files with smart chapter detection
- **Patch System** — Split books into manageable patches for processing
- **TTS Synthesis** — VoxCPM2-based text-to-speech with per-chunk WAV output
- **Audio Merge** — Combine patches into full audiobook files
- **Video Generation** — Create videos with custom backgrounds per patch/chapter
- **YouTube Upload** — Auto-upload generated videos to YouTube
- **Modern UI** — Dark mode, drag-and-drop upload, image preview
- **Batch Processing** — Upload multiple books, generate videos in bulk
- **Background Worker** — Non-blocking queue processing with admin controls
- **Crash Recovery** — SQLite tracking survives restarts

## Setup

Requires Python ≥3.10, <3.13.

```bash
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -e .
```

### TTS Engine

```bash
./.venv/Scripts/python.exe -m pip install voxcpm
```

**VRAM:** VoxCPM2 needs ~8GB VRAM. Use CPU mode or smaller model for lower VRAM GPUs.

### Environment

Copy `.env.example` to `.env` and configure:

```bash
cp .env.example .env
```

Key settings:
- `DATA_ROOT` — Storage path for uploads and generated files
- `ENABLE_WORKER` — Toggle background processing (`true`/`false`)
- `USE_NVENC` — Hardware-accelerated video encoding
- `YOUTUBE_*` — YouTube upload credentials and defaults

### ffmpeg/ffprobe

Binary tracked via Git LFS. Place `ffmpeg.exe` and `ffprobe.exe` in `assets/bin/`.

## Running

```bash
./.venv/Scripts/python.exe -m uvicorn app.main:app --reload
```

Open http://localhost:8000

### Pages

- `/books` — Upload EPUBs, view library
- `/books/{id}` — Book details, chapter management, patch controls
- `/queue` — Real-time processing queue monitor
- `/video` — Standalone video creator (upload audio + background)
- `/youtube` — YouTube upload management
- `/logs` — Application logs

## Architecture

```
app/
├── main.py           # FastAPI app, routes, lifespan
├── config.py         # Pydantic settings
├── models.py         # SQLAlchemy models
├── db.py             # Database setup
├── repository.py     # Data access layer
├── epub_parser.py    # EPUB extraction
├── chunker.py        # Text chunking & patch building
├── tts_engine.py     # VoxCPM2 TTS wrapper
├── audio_merge.py    # Patch/chunk merging
├── video_gen.py      # ffmpeg video generation
├── ffmpeg.py         # ffmpeg/ffprobe utilities
├── youtube.py        # YouTube API client
├── worker.py         # Background queue processor
├── routes/           # API endpoints
│   ├── books.py      # Book CRUD & upload
│   ├── patches.py    # Patch management
│   ├── queue.py      # Queue status & controls
│   ├── video.py      # Video generation
│   ├── downloads.py  # File downloads
│   ├── youtube.py    # YouTube upload
│   └── logs.py       # Log streaming
├── templates/        # Jinja2 HTML
└── static/           # CSS, JS, images
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/books` | Upload EPUB |
| `GET` | `/api/books` | List all books |
| `GET` | `/api/books/{id}` | Book details |
| `DELETE` | `/api/books/{id}` | Delete book |
| `POST` | `/api/books/{id}/chapters/{ch}/exclude` | Toggle chapter exclude |
| `POST` | `/api/books/{id}/patches/build` | Build custom patches |
| `POST` | `/api/patches/{id}/regenerate` | Regenerate patch |
| `POST` | `/api/patches/{id}/replace` | Text replacement rules |
| `GET` | `/api/queue` | Queue status |
| `POST` | `/api/queue/pause` | Pause worker |
| `POST` | `/api/queue/resume` | Resume worker |
| `POST` | `/api/video/generate` | Generate video from audio |
| `POST` | `/api/youtube/upload/{book_id}` | Upload to YouTube |

## CLI Scripts

```bash
# Test EPUB parsing
python scripts/test_epub_parse.py <epub>

# Test patch/chunk generation
python scripts/test_repo_and_chunker.py <epub>

# Test TTS (stub unless --real)
python scripts/test_tts_single_patch.py <epub> --real

# Test audio merge + video
python scripts/test_merge_and_video.py

# Test full worker lifecycle
python scripts/test_worker.py
```

## Configuration Reference

| Setting | Default | Description |
|---------|---------|-------------|
| `DATA_ROOT` | `./data` | Storage root |
| `DEFAULT_PATCH_SIZE` | `10` | Chapters per patch |
| `TTS_MAX_CHARS` | `400` | Max chars per TTS call |
| `USE_NVENC` | `false` | Hardware video encoding |
| `ENABLE_WORKER` | `true` | Background processing |
| `WORKER_POLL_INTERVAL` | `2.0` | Queue poll interval (sec) |
| `YOUTUBE_AUTO_UPLOAD` | `true` | Auto-upload to YouTube |
| `YOUTUBE_DEFAULT_PRIVACY` | `private` | Video privacy |
| `RESET_ALL_JOBS_ON_STARTUP` | `false` | Dev-only DB reset |

## Known Limitations

- `TTS_MAX_CHARS=400` is untested — adjust after real-model testing
- Progress tracked per-patch, not per-chunk
- Single chapter across multiple spine files appears as multiple chapters
- Video generation requires ffmpeg in PATH or `assets/bin/`
