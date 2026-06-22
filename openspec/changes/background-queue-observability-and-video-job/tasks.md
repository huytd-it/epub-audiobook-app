## 1. Schema additions

- [ ] 1.1 Add `book_job` table to `_SCHEMA` in `app/db.py` with columns: `id` (PK AUTOINCREMENT), `book_id` (FK to `book.id` ON DELETE CASCADE), `job_type` (TEXT NOT NULL DEFAULT 'video'), `status` (TEXT NOT NULL DEFAULT 'pending'), `attempt_count` (INT NOT NULL DEFAULT 0), `error_message` (TEXT), `output_path` (TEXT), `created_at` (TEXT NOT NULL), `updated_at` (TEXT NOT NULL), `UNIQUE(book_id, job_type)` to prevent duplicate video jobs
- [ ] 1.2 Add `app_state` table to `_SCHEMA`: `key TEXT PRIMARY KEY, value TEXT` (idempotent via `CREATE TABLE IF NOT EXISTS`)
- [ ] 1.3 Add `CREATE INDEX IF NOT EXISTS idx_book_job_status ON book_job(status, book_id, id)` to speed up the claim query
- [ ] 1.4 Add `CREATE INDEX IF NOT EXISTS idx_patch_status_updated ON patch(status, updated_at DESC)` to support the `last_errors` query in `get_queue_stats`
- [ ] 1.5 Run `init_schema` against a copy of an existing DB to confirm both new tables are created without altering the `book` / `chapter` / `patch` tables

## 2. Model and repository helpers

- [ ] 2.1 Add `BookJob` dataclass to `app/models.py` mirroring the `Patch` shape (`id, book_id, job_type, status, attempt_count, error_message, output_path, created_at, updated_at`)
- [ ] 2.2 Add `repository.claim_next_pending_book_job(conn) -> BookJob | None` mirroring `claim_next_pending_patch` (BEGIN IMMEDIATE + flip status to 'processing' + bump `attempt_count`, ordered by `book_id, id`)
- [ ] 2.3 Add `repository.mark_book_job_done(conn, job_id, output_path)`, `mark_book_job_failed(conn, job_id, error_message)`, `get_book_job(conn, book_id, job_type) -> BookJob | None`
- [ ] 2.4 Add `repository.enqueue_book_job(conn, book_id, job_type) -> BookJob` — returns the existing job if one already exists for `(book_id, job_type)`, else inserts a new one in `pending` state; idempotent
- [ ] 2.5 Add `repository.get_app_state(conn, key) -> str | None` and `set_app_state(conn, key, value) -> None`
- [ ] 2.6 Add `repository.get_queue_stats(conn) -> dict` returning `{patch: {pending, processing, done, failed}, book_job: {...}, oldest_pending_patch_age_seconds: float, last_errors: [{entity, id, book_id, error_message, updated_at}, ...] (max 5)}`
- [ ] 2.7 Add `repository.get_last_error_for_book(conn, book_id) -> str | None` — most recent `error_message` from any failed `patch` or `book_job` for this book
- [ ] 2.8 Add `repository.retry_all_failed_patches_for_book(conn, book_id) -> int` — resets every `failed` patch of the book to `pending` (clearing `error_message` and `audio_path`); skips patches currently `processing`; returns the count reset

## 3. PatchWorker upgrades

- [ ] 3.1 Add `_log_event(event, **fields)` helper in `app/worker.py` emitting one INFO log line in the format `event=<name> patch_id=<id> book_id=<id> attempt=<n> [error="<msg>"]` (ERROR level for failure events)
- [ ] 3.2 In `run_forever`, update `self.last_heartbeat_at = _now()` at the top of every iteration, *before* the pause check
- [ ] 3.3 In `run_forever`, after the in-flight patch finishes (or when idle), call `repository.get_app_state(self.conn, 'queue.paused')`; if `'1'`, log `event=worker.paused_sleeping` and `await asyncio.sleep(self.poll_interval)` without claiming
- [ ] 3.4 After `claim_next_pending_patch` returns `None`, call `claim_next_pending_book_job` (one claim per iteration, to give patches priority even if `book_job` is also pending)
- [ ] 3.5 Add `_process_book_job(job)` method dispatching on `job.job_type`; only `'video'` for now; calls `_run_video_job(job)` and wraps in try/except for `mark_book_job_failed`
- [ ] 3.6 Add `_run_video_job(job)`: fetch book, fetch `final_audio_path`, run `generate_video` in a thread (already used in `trigger_video`), write to `data_root/books/{book_id}/video_{job_id}.mp4`, call `mark_book_job_done`
- [ ] 3.7 In `_merge_final_audio`, after `repository.set_book_final_audio`, call `repository.enqueue_book_job(self.conn, book_id, 'video')` if and only if `book.background_image_path` is not None; log `event=book_job.auto_enqueued book_id=<id> job_type=video`
- [ ] 3.8 Update `PatchWorker.__init__` to accept `shutdown_timeout: float = 300.0`; `run_forever` exits cleanly when `self._stop` is set *and* no patch is currently in flight (tracked via a `self._in_flight: bool` flag set in `_process` and `_process_book_job`)
- [ ] 3.9 Expose `worker.last_heartbeat_at` and `worker.state` (`'idle'` / `'busy'` / `'paused'`) as plain attributes for the `/health` endpoint to read

## 4. New API endpoints

- [ ] 4.1 `app/routes/queue.py` (new file): `GET /health` returning `{status, worker_state, current_patch_id, queue_depth, last_heartbeat_at}`. Returns 503 if `last_heartbeat_at` is older than `3 * poll_interval` seconds
- [ ] 4.2 `GET /queue/stats` returning the dict from `repository.get_queue_stats`
- [ ] 4.3 `POST /queue/pause` and `POST /queue/resume` setting `app_state['queue.paused']` to `'1'` or `'0'`, redirecting (303) to `/books`; require no body
- [ ] 4.4 `POST /books/{book_id}/patches/retry-failed` calling `retry_all_failed_patches_for_book`, redirecting (303) to `/books/{book_id}`
- [ ] 4.5 `POST /books/{book_id}/video/regenerate` deleting the existing `book_job` of `type='video'` for that book (only if not currently `processing`) and re-enqueueing; returns 409 if a video job is `processing`
- [ ] 4.6 In `app/routes/books.py`, change `POST /books/{book_id}/video` to enqueue a `book_job` of `type='video'` and return 303 redirect to `/books/{book_id}`; remove the synchronous `generate_video(...)` call
- [ ] 4.7 Mount the new router in `app/main.py`: `app.include_router(queue.router)`
- [ ] 4.8 Add `worker_shutdown_timeout_seconds: float = 300.0` to `Settings` in `app/config.py`

## 5. UI updates

- [ ] 5.1 In `app/templates/book_detail.html`, add a "Last error" block (only visible if `get_last_error_for_book` returns non-None) with the error message and a "Retry all failed patches" form button
- [ ] 5.2 Add a "Video" status row showing the latest `book_job` of `type='video'` for the book (status, error if failed, output path if done) — or "no background image, video skipped" when the book has no `background_image_path`
- [ ] 5.3 Add a "Regenerate video" form button visible when a video `book_job` exists and is not currently `processing`
- [ ] 5.4 In `book_list.html`, add a small "Queue: N pending patches" line under each book (sum of `pending` patches for that book)
- [ ] 5.5 Add minor CSS rules in `app/static/style.css` for the new blocks (reuse existing `.error`, `.meta`, `.btn` classes where possible; only add new rules if necessary)

## 6. Lifespan and shutdown

- [ ] 6.1 In `app/main.py` `lifespan` startup, after `init_schema` and `requeue_stuck_processing`, add a one-shot video backfill: for each book with `status='done'`, non-NULL `final_audio_path` and `background_image_path`, and no existing `book_job` of `type='video'`, insert a `pending` job. Log `info("backfilled %s video book_job(s) on startup", n)`
- [ ] 6.2 In `app/main.py` `lifespan` shutdown, replace the `worker_task.cancel()` + `try/await` block with: `worker.stop()`; `try: await asyncio.wait_for(worker_task, timeout=settings.worker_shutdown_timeout_seconds)`; `except asyncio.TimeoutError: logger.warning("worker did not stop within %s s; cancelling", settings.worker_shutdown_timeout_seconds); worker_task.cancel(); try: await worker_task except asyncio.CancelledError: pass`

## 7. Tests

- [ ] 7.1 `tests/test_queue_stats.py`: insert patches in known states (3 pending, 1 processing, 50 done, 2 failed) and 1 pending `book_job`; assert `get_queue_stats` returns the expected counts and that `last_errors` has length 2 with the most recent first
- [ ] 7.2 `tests/test_retry_failed.py`: insert 2 `failed` patches and 1 `processing` patch for the same book; call `retry_all_failed_patches_for_book`; assert the 2 failed are now `pending` with `error_message=NULL`, the processing one is unchanged, and the return value is 2
- [ ] 7.3 `tests/test_pause_flag.py`: set `app_state['queue.paused']='1'`; run the worker one tick; assert no patch is claimed. Set `'0'`; assert claiming resumes
- [ ] 7.4 `tests/test_video_job.py`: simulate a book with `final_audio_path` and `background_image_path`; call `enqueue_book_job` twice with the same `(book_id, job_type)`; assert only one row exists. Mark it `processing`; assert `claim_next_pending_book_job` returns `None` on a second worker
- [ ] 7.5 `tests/test_health_endpoint.py`: use FastAPI `TestClient` to GET `/health` after a synthetic heartbeat at "now"; assert 200 with `status='ok'`. Backdate `last_heartbeat_at` by `4 * poll_interval`; assert 503 with `status='degraded'`
- [ ] 7.6 Run `python -m pytest tests/` and ensure all existing tests still pass — no DB schema changes to `book` / `chapter` / `patch`; no signature changes to `PatchWorker.__init__` except a new kwarg with a default

## 8. Verification

- [ ] 8.1 Manual smoke test: upload an EPUB with a background image; watch `/queue/stats` show pending → processing → done transitions for both `patch` and `book_job`; verify the video file appears at `data/books/{id}/video_{job_id}.mp4`
- [ ] 8.2 Manual smoke test: deliberately fail a patch (e.g., corrupt voice file at upload); confirm the error message appears on book detail and "Retry all failed patches" resets the failed patch to `pending`
- [ ] 8.3 Manual smoke test: POST `/queue/pause`; observe the worker stops claiming; upload a new book; confirm no patches are processed; POST `/queue/resume`; confirm normal operation resumes. Restart the server during a paused state; confirm the queue remains paused
- [ ] 8.4 Manual smoke test: send SIGTERM to the server while a patch is in flight; confirm the patch finishes (or the timeout fires with a WARNING) and the server exits cleanly; verify no half-written wav remains in `data/books/{id}/patches/`
- [ ] 8.5 Manual smoke test: on a pre-existing DB (one that was created before this change), start the server; confirm the backfill log line reports the expected number of video jobs enqueued
