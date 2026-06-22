## ADDED Requirements

### Requirement: Book job table
The system SHALL persist book-level jobs in a `book_job` table with columns: `id` (INTEGER PRIMARY KEY AUTOINCREMENT), `book_id` (INTEGER NOT NULL, FK to `book.id` ON DELETE CASCADE), `job_type` (TEXT NOT NULL DEFAULT `'video'`), `status` (TEXT NOT NULL DEFAULT `'pending'`, one of `pending|processing|done|failed`), `attempt_count` (INTEGER NOT NULL DEFAULT 0), `error_message` (TEXT), `output_path` (TEXT), `created_at` (TEXT NOT NULL), `updated_at` (TEXT NOT NULL). The pair `(book_id, job_type)` MUST be UNIQUE so a book cannot have two in-flight jobs of the same type.

#### Scenario: Enqueue a new video job
- **WHEN** `enqueue_book_job(book_id=7, job_type='video')` is called for a book with no existing video job
- **THEN** a new `book_job` row is inserted with `status='pending'`, `job_type='video'`, `attempt_count=0`

#### Scenario: Enqueue is idempotent on existing job
- **WHEN** `enqueue_book_job(book_id=7, job_type='video')` is called and a `book_job` with `book_id=7, job_type='video'` already exists (in any status, including `processing` or `failed`)
- **THEN** the existing row is returned unchanged; no new row is inserted

#### Scenario: Enqueue of a different job_type
- **WHEN** `enqueue_book_job(book_id=7, job_type='video')` is called and a `book_job` with `book_id=7, job_type='summary'` already exists
- **THEN** a new `book_job` row of `job_type='video'` is inserted (the `UNIQUE` constraint is on `(book_id, job_type)`, not on `book_id` alone)

### Requirement: Worker drains the book job queue
The worker SHALL, after finding no pending patch in a loop iteration, attempt to claim a pending `book_job` (lowest `book_id`, then `id`) by flipping its status to `processing` and incrementing `attempt_count` in a single `BEGIN IMMEDIATE` transaction. The worker MUST process the claimed job in a thread via `asyncio.to_thread` (so the event loop is not blocked). On success, the worker sets `status='done'`, fills `output_path`, and clears `error_message`. On exception, the worker sets `status='failed'`, fills `error_message`, and continues with the next iteration.

#### Scenario: Worker processes a video book_job
- **WHEN** the queue has no pending patches and 1 pending video book_job
- **THEN** the worker claims the book_job, runs video generation in a thread, and on success marks it `done` with `output_path` set to the generated mp4 path

#### Scenario: Worker prioritizes patches over book_jobs
- **WHEN** the queue has 1 pending patch and 1 pending video book_job
- **THEN** the worker claims the patch first; the book_job is not claimed until the patch is done (i.e., one claim per iteration)

#### Scenario: Video generation fails
- **WHEN** `generate_video` raises an exception during the book_job
- **THEN** the book_job is marked `failed` with the exception message; the worker continues to the next iteration and does not crash

#### Scenario: Book job claim is atomic
- **WHEN** two workers call `claim_next_pending_book_job` concurrently (only relevant in tests; current deployment is single-worker)
- **THEN** `BEGIN IMMEDIATE` serializes the two transactions, and exactly one returns the claimed job; the other returns `None`

### Requirement: Auto-enqueue video on book finalization
The system SHALL, after a book's `final_audio_path` is set (in `PatchWorker._merge_final_audio`), automatically enqueue a `book_job` of `job_type='video'` if and only if the book has a `background_image_path`. Books with no `background_image_path` MUST NOT have a video job enqueued. The enqueue call MUST be idempotent: if a video `book_job` for the book already exists, no new row is inserted.

#### Scenario: Book with background image
- **WHEN** the worker finalizes book 5's audio and `book.background_image_path='/path/to/bg.jpg'`
- **THEN** a `book_job` of `type='video'` is enqueued for book 5 in `pending` state

#### Scenario: Book without background image
- **WHEN** the worker finalizes book 6's audio and `book.background_image_path IS NULL`
- **THEN** no `book_job` is enqueued; book 6 has no video row

#### Scenario: Refinalize an already-finalized book (idempotent)
- **WHEN** book 5 already has a video `book_job` in `done` state and the worker re-finalizes book 5's audio
- **THEN** no new `book_job` is enqueued; the existing `done` row is left as-is

### Requirement: One-shot backfill of video jobs at startup
The system's FastAPI `lifespan` startup SHALL, after schema init and `requeue_stuck_processing`, scan for books with `status='done'`, non-NULL `final_audio_path`, non-NULL `background_image_path`, and no existing `book_job` of `type='video'`. For each such book, the startup MUST insert a `pending` `book_job` of `type='video'`. The number of inserted jobs MUST be logged at INFO level (one log line, format: `event=backfill.video_jobs_inserted count=<n>`).

#### Scenario: Existing book without video job
- **WHEN** the server starts and book 3 has `status='done'`, `final_audio_path='/path/to/final.wav'`, `background_image_path='/path/to/bg.jpg'`, and no `book_job` of `type='video'`
- **THEN** a new `book_job` of `type='video'` for book 3 is inserted in `pending` state; an INFO log line records the count

#### Scenario: Existing book already has a video job
- **WHEN** the server starts and book 4 already has a `book_job` of `type='video'` (in any status)
- **THEN** no new `book_job` is inserted

#### Scenario: Existing book with no background image
- **WHEN** the server starts and book 5 has `status='done'` and `final_audio_path` set, but `background_image_path IS NULL`
- **THEN** no `book_job` is inserted (and the count logged reflects this — book 5 is excluded)

### Requirement: Video job UI
The system SHALL render, on the book detail page, a "Video" status row showing the latest `book_job` of `type='video'` for that book. The row MUST show one of: `pending (queued)`, `processing`, `done (output: <path>)`, `failed (<error_message>)`, or, when no `background_image_path` is set on the book, `no background image, video skipped`. When a `book_job` exists and is not currently `processing`, the page MUST show a "Regenerate video" form button that POSTs to `/books/{book_id}/video/regenerate`.

#### Scenario: Book with a pending video job
- **WHEN** book 5 has a `book_job` of `type='video'` with `status='pending'`
- **THEN** the book detail page shows "Video: pending (queued)" and a "Regenerate video" button

#### Scenario: Book with a done video job
- **WHEN** book 5 has a `book_job` of `type='video'` with `status='done'` and `output_path='/data/books/5/video_9.mp4'`
- **THEN** the book detail page shows "Video: done (output: /data/books/5/video_9.mp4)" and a "Regenerate video" button

#### Scenario: Book with a failed video job
- **WHEN** book 5 has a `book_job` of `type='video'` with `status='failed'` and `error_message='ffmpeg error'`
- **THEN** the book detail page shows "Video: failed (ffmpeg error)" and a "Regenerate video" button

#### Scenario: Book without background image
- **WHEN** book 5 has `background_image_path IS NULL`
- **THEN** the book detail page shows "Video: no background image, video skipped" and no "Regenerate video" button

#### Scenario: Book with a processing video job
- **WHEN** book 5 has a `book_job` of `type='video'` with `status='processing'`
- **THEN** the book detail page shows "Video: processing" and no "Regenerate video" button (it would 409)

### Requirement: Regenerate video action
The system SHALL expose `POST /books/{book_id}/video/regenerate` that, if a `book_job` of `type='video'` exists for the book and is currently `processing`, returns 409 Conflict with a clear error message; otherwise deletes the existing `book_job` of `type='video'` for the book (if any) and re-enqueues a new one in `pending` state via `enqueue_book_job`, then returns 303 redirect to `/books/{book_id}`. If no video `book_job` exists, the endpoint MUST still enqueue one (same as the first-time enqueue path).

#### Scenario: Regenerate a failed video job
- **WHEN** book 5 has a `book_job` of `type='video'` with `status='failed'`
- **THEN** POST `/books/5/video/regenerate` deletes the failed row and inserts a new `pending` row; the response is 303 to `/books/5`

#### Scenario: Regenerate while processing
- **WHEN** book 5 has a `book_job` of `type='video'` with `status='processing'`
- **THEN** POST `/books/5/video/regenerate` returns 409 with a clear error message and no DB changes

#### Scenario: Regenerate a done video job
- **WHEN** book 5 has a `book_job` of `type='video'` with `status='done'`
- **THEN** POST `/books/5/video/regenerate` deletes the `done` row and inserts a new `pending` row; the response is 303 to `/books/5`

#### Scenario: Regenerate with no existing job
- **WHEN** book 5 has no `book_job` of `type='video'`
- **THEN** POST `/books/5/video/regenerate` enqueues a new `pending` row; the response is 303 to `/books/5`

### Requirement: Video endpoint is non-blocking
The system SHALL change the behavior of `POST /books/{book_id}/video` from running `generate_video` synchronously inside the request handler to enqueueing a `book_job` of `type='video'` (via `enqueue_book_job`) and returning a 303 redirect to `/books/{book_id}`. The video generation itself MUST happen in the worker, not in the HTTP request thread. If a `book_job` of `type='video'` for the book already exists in any status, the endpoint MUST be a no-op and return 303 redirect (the existing job will be re-run only via "Regenerate video").

#### Scenario: User clicks "Generate video" while audio is being finalized
- **WHEN** book 5's audio is being finalized (status='processing') and the user POSTs to `/books/5/video`
- **THEN** the response is 303 redirect to `/books/5`; no `book_job` is enqueued by the route (the worker will auto-enqueue once the audio is done)

#### Scenario: User clicks "Generate video" after audio is done
- **WHEN** book 5's audio is `done` and no `book_job` of `type='video'` exists
- **THEN** a `book_job` of `type='video'` is enqueued in `pending` state; the response is 303 redirect to `/books/5`

#### Scenario: User clicks "Generate video" when one is already pending
- **WHEN** book 5's audio is `done` and a `book_job` of `type='video'` is in `pending` state
- **THEN** the route does nothing (the existing job stays in `pending`); the response is 303 redirect to `/books/5`
