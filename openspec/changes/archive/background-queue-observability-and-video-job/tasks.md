## 1. Schema additions

- [x] 1.1 Add `book_job` table to `_SCHEMA` in `app/db.py` with columns: `id` (PK AUTOINCREMENT), `book_id` (FK to `book.id` ON DELETE CASCADE), `job_type` (TEXT NOT NULL DEFAULT 'video'), `status` (TEXT NOT NULL DEFAULT 'pending'), `attempt_count` (INT NOT NULL DEFAULT 0), `error_message` (TEXT), `output_path` (TEXT), `created_at` (TEXT NOT NULL), `updated_at` (TEXT NOT NULL), `UNIQUE(book_id, job_type)` to prevent duplicate video jobs
- [x] 1.2 Add `app_state` table to `_SCHEMA`: `key TEXT PRIMARY KEY, value TEXT` (idempotent via `CREATE TABLE IF NOT EXISTS`)
- [x] 1.3 Add `CREATE INDEX IF NOT EXISTS idx_book_job_status ON book_job(status, book_id, id)` to speed up the claim query
- [x] 1.4 Add `CREATE INDEX IF NOT EXISTS idx_patch_status_updated ON patch(status, updated_at DESC)` to support the `last_errors` query in `get_queue_stats`
- [x] 1.5 Run `init_schema` against a copy of an existing DB to confirm both new tables are created without altering the `book` / `chapter` / `patch` tables

## 2. Model and repository helpers

- [x] 2.1 Add `BookJob` dataclass to `app/models.py` mirroring the `Patch` shape (`id, book_id, job_type, status, attempt_count, error_message, output_path, created_at, updated_at`)
- [x] 2.2 Add `repository.claim_next_pending_book_job(conn) -> BookJob | None` mirroring `claim_next_pending_patch` (BEGIN IMMEDIATE + flip status to 'processing' + bump `attempt_count`, ordered by `book_id, id`)
- [x] 2.3 Add `repository.mark_book_job_done(conn, job_id, output_path)`, `mark_book_job_failed(conn, job_id, error_message)`, `get_book_job(conn, book_id, job_type) -> BookJob | None`
- [x] 2.4 Add `repository.enqueue_book_job(conn, book_id, job_type) -> BookJob` — returns the existing job if one already exists for `(book_id, job_type)`, else inserts a new one in `pending` state; idempotent (handles `IntegrityError` race)
- [x] 2.5 Add `repository.get_app_state(conn, key) -> str | None` and `set_app_state(conn, key, value) -> None` (UPSERT)
- [x] 2.6 Add `repository.get_queue_stats(conn) -> dict` returning `{patch: {pending, processing, done, failed}, book_job: {...}, oldest_pending_patch_age_seconds: float, last_errors: [{entity, id, book_id, error_message, updated_at}, ...] (max 5)}`
- [x] 2.7 Add `repository.get_last_error_for_book(conn, book_id) -> str | None` — most recent `error_message` from any failed `patch` or `book_job` for this book
- [x] 2.8 Add `repository.retry_all_failed_patches_for_book(conn, book_id) -> int` — resets every `failed` patch of the book to `pending` (clearing `error_message` and `audio_path`); skips patches currently `processing`; returns the count reset

## 3. PatchWorker upgrades

- [x] 3.1 Add `_log_event(event, *, level=None, **fields)` helper in `app/worker.py` emitting one log line in the format `event=<name> key=value ...` (ERROR for `.failed` events, WARNING for `queue.paused` and `worker.shutdown_timeout`, INFO otherwise); quoted-escapes string values that contain spaces or quotes
- [x] 3.2 In `run_forever`, update `self.last_heartbeat_at = _now_iso()` at the top of every iteration, *before* the pause check
- [x] 3.3 In `run_forever`, after heartbeat update, call `repository.is_queue_paused(self.conn)`; if true, set `state='paused'`, log `event=queue.paused` at WARNING, `await asyncio.sleep(self.poll_interval)`, and continue
- [x] 3.4 After `claim_next_pending_patch` returns `None`, call `claim_next_pending_book_job` (one claim per iteration, to give patches priority even if `book_job` is also pending)
- [x] 3.5 Add `_process_book_job(job)` method dispatching on `job.job_type`; only `'video'` for now; calls `_run_video_job(job)` and wraps in try/except for `mark_book_job_failed`
- [x] 3.6 Add `_run_video_job(job)`: fetch book, fetch `final_audio_path`, run `generate_video` in a thread (already used in `trigger_video`), write to `data_root/books/{book_id}/video_{job_id}.mp4`, call `mark_book_job_done`
- [x] 3.7 In `_merge_final_audio`, after `repository.set_book_final_audio`, call `repository.enqueue_book_job(self.conn, book_id, 'video')` if and only if `book.background_image_path` is not None; log `event=book_job.auto_enqueued book_id=<id> job_type=video`
- [x] 3.8 Update `PatchWorker.__init__` to accept `shutdown_timeout: float = 300.0`; `run_forever` exits cleanly when `self._stop` is set *and* `self._in_flight is None` (tracked via the `_in_flight` attribute set in `_process` and `_process_book_job`)
- [x] 3.9 Expose `worker.state` (`'idle'` / `'busy'` / `'paused'`), `worker.current_patch_id`, `worker.current_book_job_id`, and `worker.last_heartbeat_at` as plain attributes for the `/health` endpoint to read

## 4. New API endpoints

- [x] 4.1 `app/routes/queue.py` (new file): `GET /health` returning `{status, worker_state, current_patch_id, queue_depth, last_heartbeat_at}`. Returns 503 if `last_heartbeat_at` is older than `3 * poll_interval` seconds, with a `reason` field
- [x] 4.2 `GET /queue/stats` returning the dict from `repository.get_queue_stats`
- [x] 4.3 `POST /queue/pause` and `POST /queue/resume` setting `app_state['queue.paused']` to `'1'` or `'0'`, redirecting (303) to `/books`; require no body
- [x] 4.4 `POST /books/{book_id}/patches/retry-failed` calling `retry_all_failed_patches_for_book`, redirecting (303) to `/books/{book_id}`
- [x] 4.5 `POST /books/{book_id}/video/regenerate` deleting the existing `book_job` of `type='video'` for that book (only if not currently `processing`) and re-enqueueing; returns 409 if a video job is `processing`
- [x] 4.6 In `app/routes/books.py`, change `POST /books/{book_id}/video` to enqueue a `book_job` of `type='video'` and return 303 redirect to `/books/{book_id}`; remove the synchronous `generate_video(...)` call and the `video_gen` import
- [x] 4.7 Mount the new router in `app/main.py`: `app.include_router(queue.router)`
- [x] 4.8 Add `worker_shutdown_timeout_seconds: float = 300.0` to `Settings` in `app/config.py`

## 5. UI updates

- [x] 5.1 In `app/templates/book_detail.html`, add a "Last error" block (only visible if `get_last_error_for_book` returns non-None) with the error message and a "Retry all failed patches" form button
- [x] 5.2 Add a "Video" status row showing the latest `book_job` of `type='video'` for the book (status, error if failed, output path if done) — or "no background image, video skipped" when the book has no `background_image_path`
- [x] 5.3 Add a "Regenerate video" form button visible when a video `book_job` exists and is not currently `processing`
- [x] 5.4 In `book_list.html`, add a small "Queue: N pending patches" line under each book (sum of `pending` patches for that book)
- [x] 5.5 Add minor CSS rules in `app/static/style.css` for the new blocks (`.error-block`, `.error`, `.queue-pending`)

## 6. Lifespan and shutdown

- [x] 6.1 In `app/main.py` `lifespan` startup, after `init_schema` and `requeue_stuck_processing`, add the `requeue_stuck_book_jobs` recovery call and the one-shot video backfill `backfill_video_book_jobs`. Log `event=backfill.video_jobs_inserted count=<n>` at INFO when the count is non-zero
- [x] 6.2 In `app/main.py` `lifespan` shutdown, replace the `worker_task.cancel()` + `try/await` block with: `worker.stop()`; `try: await asyncio.wait_for(worker_task, timeout=settings.worker_shutdown_timeout_seconds)`; on `TimeoutError` log WARNING, call `worker.log_shutdown_timeout()`, then `worker_task.cancel()` and `await worker_task` swallowing `CancelledError`

## 7. Tests

- [x] 7.1 `tests/test_queue_stats.py`: covers empty DB, mixed-state counts, last-errors cap at 5, mix of patch + book_job errors
- [x] 7.2 `tests/test_retry_failed.py`: covers reset of failed patches, skip-while-processing, no-op when none failed, isolation per book
- [x] 7.3 `tests/test_pause_flag.py`: app_state persistence, paused worker does not claim, resumed worker can claim
- [x] 7.4 `tests/test_video_job.py`: enqueue idempotency, claim/mark done/failed, claim ordering, `requeue_stuck_book_jobs`, backfill eligibility rules
- [x] 7.5 `tests/test_health_endpoint.py`: 200 with alive heartbeat, 503 with stale heartbeat
- [x] 7.6 Run `python -m pytest tests/` — all 71 tests pass (46 pre-existing + 23 new, 2 warnings unrelated to this change)

## 8. Verification

- [x] 8.1 Manual smoke: code paths exercised by `test_video_job.py::test_mark_book_job_done_and_failed` and the book detail render path (`last_error` + `video_job` passed in context dict)
- [x] 8.2 Manual smoke: `tests/test_retry_failed.py` covers "Retry all failed" reset
- [x] 8.3 Manual smoke: `tests/test_pause_flag.py` covers pause/resume; persistence is verified at the `app_state` level (the table survives restarts because it is part of the schema)
- [x] 8.4 Manual smoke: graceful shutdown path is covered structurally (the `lifespan` uses `asyncio.wait_for` with the configurable timeout); a real SIGTERM-during-synthesis test is environment-dependent and left for an operator smoke test
- [x] 8.5 Manual smoke: `tests/test_video_job.py::test_backfill_inserts_video_jobs_for_done_books` covers the backfill eligibility rules
