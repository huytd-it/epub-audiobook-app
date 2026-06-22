## Why

The current background queue (`app/worker.py`) is a single in-process `PatchWorker` that polls SQLite every 2s and processes patches sequentially. Three gaps make it hard to operate:

1. **No observability.** There is no `/health` endpoint, no queue-depth metric, no ETA, no last-error summary in the UI. When something stalls, the only signal is "the book detail page still says `processing`" — the operator has to log into the box and read SQLite.
2. **No admin controls.** There is no way to pause the queue for maintenance, no way to see *why* a patch is `failed` from the UI, and the existing `regenerate` action only resets one patch at a time. Server restart can also leave a half-written wav because `worker.stop()` cancels the in-flight patch mid-synthesis.
3. **Video generation runs synchronously in the request handler** (`POST /books/{id}/video` in `app/routes/books.py:121-135`). It blocks the HTTP worker for the entire encode, which can be many minutes for a long audiobook. This is also inconsistent with how TTS is handled — a small admin action ("generate video") quietly freezes the UI for that user.

This change is the **observability + admin** slice of "better queue management": health/stats endpoints, structured logging, pause/resume, graceful shutdown, retry-failed UI, last-error surfacing, and moving video generation into the same queue as TTS.

## What Changes

- **`GET /health` endpoint**: returns `{status, worker_state, current_patch_id, queue_depth, last_heartbeat_at}` so an external monitor (or a human via `curl`) can detect a stuck worker.
- **`GET /queue/stats` endpoint**: JSON with per-status counts (pending/processing/done/failed) for both `patch` and `book_job`, oldest pending age, and the last 5 error messages with `entity`, `id`, `book_id`, and `updated_at`.
- **Structured worker logging**: one log line per event (`patch.claimed`, `patch.started`, `patch.done`, `patch.failed`, `book_job.claimed`, `book_job.started`, `book_job.done`, `book_job.failed`, `queue.paused`, `queue.resumed`, `worker.heartbeat`) with `event=… patch_id=… book_id=… attempt=…` key=value pairs at INFO level.
- **Last-error summary on book detail page**: show the most recent error message for a book plus a "Retry all failed patches" button (POSTs to a new endpoint that resets every failed patch of the book in one call).
- **Pause/resume queue**: global flag persisted in a small key-value `app_state` table. `POST /queue/pause` and `POST /queue/resume` flip it. While paused, the worker drains in-flight work to completion but does not claim new patches or new `book_job`s. State survives restarts.
- **Graceful worker shutdown**: `worker.stop()` sets a flag; the loop exits at the top of the next iteration *only if* no patch is in flight. The FastAPI `lifespan` shutdown waits up to `worker_shutdown_timeout_seconds` (default 300) for the worker to finish; on timeout a WARNING is logged and the task is cancelled (the patch is rescued by `requeue_stuck_processing` on next boot).
- **Book video as a queue job**: new `book_job` table holds book-level jobs (`type='video'`, status, attempt_count, error_message, output_path, created_at, updated_at) with `UNIQUE(book_id, job_type)`. After the worker finalizes a book's merged audio, it auto-enqueues a `video` `book_job` *if and only if* the book has a `background_image_path`. The same `PatchWorker` loop drains the `book_job` queue after the `patch` queue is empty (so patches always win priority, but videos get a free ride on the same worker).
- **Video job UI**: book detail page shows a "Video" status row (`pending`/`processing`/`done`/`failed`) and a "Regenerate video" button that deletes the existing video job and re-enqueues a new one in `pending` state.
- **Remove the synchronous `generate_video` call from the request handler**: `POST /books/{id}/video` now enqueues a video `book_job` and 303-redirects, freeing the HTTP worker.
- **One-shot backfill at startup**: on `lifespan` start, find books with `status='done'`, non-NULL `final_audio_path` and `background_image_path`, and no existing `book_job` of `type='video'`. Insert a `pending` job for each. Logged at INFO.

## Capabilities

### New Capabilities
- `queue-observability-and-admin`: health endpoint, queue stats, structured worker logging, last-error summary on book detail, pause/resume queue, graceful shutdown, "retry all failed patches" action.
- `book-video-job`: `book_job` table + worker drain + auto-enqueue on book finalization + backfill at startup + UI status row + regenerate action + non-blocking video endpoint.

### Modified Capabilities
- (none — this is the first change adding queue-related specs to `openspec/specs/`)

## Impact

- **Code**:
  - `app/worker.py`: structured logging helpers, pause-flag check, dual-queue drain (patches first, then `book_job`), `book_job` auto-enqueue on finalization, graceful shutdown with deadline, `last_heartbeat_at` updated each iteration.
  - `app/main.py`: shutdown waits for in-flight patch via `asyncio.wait_for(worker_task, timeout=settings.worker_shutdown_timeout_seconds)`; one-shot backfill at startup.
  - `app/repository.py`: add `book_job` and `app_state` CRUD; `get_queue_stats()`, `get_last_error_for_book()`, `retry_all_failed_patches_for_book()`, `enqueue_book_job()` (idempotent on `(book_id, job_type)`).
  - `app/db.py`: add `book_job` and `app_state` tables to schema (purely additive — `CREATE TABLE IF NOT EXISTS`).
  - `app/models.py`: add `BookJob` and `AppStateKey` dataclasses.
  - `app/routes/queue.py` (new): `/health`, `/queue/stats`, `/queue/pause`, `/queue/resume`, `POST /books/{id}/patches/retry-failed`, `POST /books/{id}/video/regenerate`.
  - `app/routes/books.py`: convert `POST /books/{id}/video` to enqueue + redirect.
  - `app/templates/book_detail.html`: add "Last error" block and "Video" status row with "Regenerate video" button.
  - `app/templates/book_list.html`: show total pending patch count.
  - `app/static/style.css`: minor styling for new blocks (reuse existing `.error`/`.meta` classes where possible).
  - `app/config.py`: add `worker_shutdown_timeout_seconds: float = 300.0`.
- **DB**: two new tables (`book_job`, `app_state`). **No change** to `book` / `chapter` / `patch` schema — existing data is fully preserved.
- **Backward compat**:
  - Existing books with audio + background image but no `book_job` row get one auto-enqueued by the startup backfill.
  - Books without a background image never get a video job; UI shows "no background image, video skipped" instead of an error.
  - The pause flag persists across restarts; the default is un-paused.
- **Tests**: new `tests/test_queue_stats.py`, `tests/test_retry_failed.py`, `tests/test_pause_flag.py`, `tests/test_video_job.py`. Existing test suite continues to pass (no changes to `patch` table or `PatchWorker` claim/finalize logic).
- **Migration**: none needed — both new tables are created idempotently by `init_schema`. Users on an existing DB just see two new empty tables on first start.
