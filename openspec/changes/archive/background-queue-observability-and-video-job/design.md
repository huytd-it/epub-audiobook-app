## Context

`PatchWorker` (in `app/worker.py:18-106`) runs as a single asyncio task created in `app/main.py:37` inside the FastAPI `lifespan` context. It polls `repository.claim_next_pending_patch` every `worker_poll_interval` seconds (default 2.0, from `app/config.py:17`), runs synthesis in a thread via `asyncio.to_thread`, and finalizes a book's merged audio once all patches are `done`. Recovery: `repository.requeue_stuck_processing` on startup (`app/main.py:25-27`) handles patches left `processing` from a previous crashed run by flipping them back to `pending` with `error_message='requeued after restart'`.

The DB schema is in `app/db.py:7-51`. `book`, `chapter`, `patch` are the existing tables. `patch` has `status` (`pending|processing|done|failed`), `attempt_count`, `error_message`, and `audio_path`. `requeue_stuck_processing` (`app/repository.py:184-192`) is the only recovery story for the "stuck `processing`" failure mode — it relies on the operator restarting the server.

Video generation is in `app/routes/books.py:121-135` (`trigger_video`). It runs `generate_video(book.final_audio_path, bg_image, out_path, use_nvenc=settings.use_nvenc)` synchronously inside the request thread. On a 2-hour audiobook this can take minutes, during which the user's browser is stuck loading and the HTTP worker is occupied.

There is no `/health` endpoint, no admin endpoint, and no observability in `book_detail.html` beyond a per-patch status table. The only error log a failed patch leaves is a `logger.exception("patch %s failed", patch.id)` line that has to be grepped out of the server log.

## Goals / Non-Goals

**Goals:**
- Give operators a way to verify the worker is alive and see queue depth, in one HTTP call.
- Give operators a way to pause the queue for maintenance, with the pause state surviving restarts.
- Give operators a way to retry a book's failed patches in one click (currently: one click per patch).
- Surface failure reasons on the book detail page (currently: invisible from the UI).
- Stop blocking the HTTP worker on video encode; treat video the same as TTS — a queued job with its own status row.
- Guarantee no half-written wav on server restart: in-flight patch finishes before the worker exits, bounded by a timeout.

**Non-Goals:**
- Do not change the polling model (event-driven notification is a separate change).
- Do not split the worker into a separate process (separate change).
- Do not change the existing `patch` table schema — no migration risk for users on existing data.
- Do not add a generic, payload-based job system — only `patch` (existing) and `book_job` with `job_type='video'` (new) for now.
- Do not add per-book pause (global pause is enough for the requested scope).
- Do not add authentication to `/health` (standard pattern for k8s liveness probes; returns no PII).

## Decisions

### 1. Two-queue model: `patch` (existing) and `book_job` (new)

**Lựa chọn:** Keep `patch` for chapter-range TTS unchanged. Add a new `book_job` table for book-level operations with columns `(id, book_id, job_type, status, attempt_count, error_message, output_path, created_at, updated_at)`. `job_type` is a TEXT discriminator with default `'video'`. `UNIQUE(book_id, job_type)` prevents duplicate video jobs for the same book.

**Lý do:** Mixing book-level and chapter-range work in the same table forces a NULL-everywhere mess (`chapter_start` NULL for video, `output_path` NULL for patch). Two tables keep each simple. The migration is purely additive (`CREATE TABLE IF NOT EXISTS`), no risk to existing data.

**Alternative considered:** A single generic `job` table with a JSON payload column. Rejected — over-engineered for two job types and loses SQLite's column-level typing for the small set of fields we actually query (`status`, `attempt_count`, `output_path`).

### 2. Worker priority: `patch` first, then `book_job`

**Lựa chọn:** In each loop iteration, the worker first calls `claim_next_pending_patch` (lowest `book_id`, then `patch_index`). If that returns `None`, it then calls `claim_next_pending_book_job` (lowest `book_id`, then `id`). At most one claim per iteration.

**Lý do:** A book is only "finalized" once all its patches are `done`, so a `book_job` for that book can only become relevant after patches are done. This priority matches the natural dependency: video cannot start until audio exists. No deadlock is possible because `book_job` for video is only enqueued *after* the audio finalization step (see decision 3). One-claim-per-iteration also ensures the worker still polls `patch` every iteration even when there is steady `book_job` work.

**Alternative considered:** Claiming from both tables in a single transaction. Rejected — different tables, no atomicity benefit, and would complicate the claim path.

### 3. Auto-enqueue video on book finalization

**Lựa chọn:** In `PatchWorker._merge_final_audio` (after `repository.set_book_final_audio`), if the book has a `background_image_path` *and* no `book_job` of `job_type='video'` already exists for it, insert a new `book_job` row in `pending` state. Books with no `background_image_path` skip the video step entirely.

**Lý do:** Keeps the operator happy (videos are auto-generated for any book with a background) without forcing every book to have one. Idempotent: if the worker finalizes the same book twice (e.g., after a patch is regenerated and re-finalized), the second call sees the existing `book_job` and does not enqueue a duplicate.

**Alternative considered:** Manual "Generate video" button that the user clicks after audio is done. Rejected — the existing UX already has an auto-trigger button, switching to manual breaks user expectation. The change instead keeps the auto-enqueue and adds a "Regenerate video" button for the failed case.

### 4. Pause flag in `app_state` table

**Lựa chọn:** Add a small key-value `app_state(key TEXT PRIMARY KEY, value TEXT)` table. The pause flag is `app_state['queue.paused']` set to `'1'` or `'0'`. The worker reads it at the top of each loop iteration. `POST /queue/pause` and `/queue/resume` flip it.

**Lý do:** Persists across restarts. Avoids a global Python variable that would be invisible to other processes and reset on restart. Migration: `CREATE TABLE IF NOT EXISTS app_state` is idempotent. The key-value shape is general enough to host future flags (e.g., a future "drain mode") without further schema changes.

**Alternative considered:** In-memory flag in `app.state`. Rejected — would reset on every restart, so a paused queue would un-pause itself, which is the wrong default for "operator paused it for a reason".

### 5. Graceful shutdown with deadline

**Lựa chọn:** `worker.stop()` sets a `_stop` flag. `run_forever` checks the flag at the *top* of each loop iteration. If a patch is in flight, the loop finishes the in-flight patch first, then exits. A separate `worker_shutdown_timeout_seconds` setting (default 300, configurable) bounds how long the FastAPI `lifespan` will wait before forcing cancellation.

**Lý do:** A half-written wav is worse than a long shutdown. The deadline protects against a hung patch (TTS model crashed, GPU stuck, network blip on a remote engine) — at the deadline, the `lifespan` cancels the task and the next startup's `requeue_stuck_processing` (`app/repository.py:184-192`) rescues the patch. The 300s default is generous for TTS but bounded enough that an operator waiting for a restart doesn't get stuck.

**Alternative considered:** Cooperative cancellation via a flag passed into `_synthesize`. Rejected — `VoxCPMEngine.synthesize_patch` is a black-box call to a C++/CUDA extension; we cannot safely interrupt it mid-call, and even if we could, the resulting wav would still need to be deleted and the patch requeued.

### 6. Health endpoint shape and staleness detection

**Lựa chọn:** `GET /health` returns 200 with `{status: "ok", worker_state: "idle"|"busy"|"paused", current_patch_id: int|null, queue_depth: int, last_heartbeat_at: ISO8601}`. The endpoint returns 503 if `last_heartbeat_at` is more than `3 * poll_interval` seconds in the past. `last_heartbeat_at` is updated at the top of every loop iteration (regardless of whether work was found), so a stalled worker is detectable from outside without parsing logs.

**Lý do:** A staleness threshold of `3 * poll_interval` (default 6s with the 2s poll) tolerates one missed heartbeat from a slow iteration (e.g., a long `claim_next_pending_patch` query or a GC pause) without flapping the endpoint. Three is a small enough multiplier that a truly stuck worker (e.g., the asyncio task crashed silently) is still detected quickly.

**Alternative considered:** Always return 200 and let the operator parse the timestamp. Rejected — 503 is the standard liveness-probe pattern for k8s, Docker, and most uptime monitors, and lets alerts fire on the HTTP status alone.

### 7. Last-error summary on book detail

**Lựa chọn:** Add to `book_detail.html` a small block showing:
- "Last error: <message> — [Retry all failed patches]" (only rendered if `get_last_error_for_book` returns non-None)
- A "Video: <status>" row showing the latest `book_job` of `type='video'` for this book

The "Retry all failed patches" button POSTs to `/books/{book_id}/patches/retry-failed` which calls `repository.retry_all_failed_patches_for_book` (a new helper that resets all `failed` patches of a book in one transaction, skipping any patch currently `processing`).

**Lý do:** Reuses the existing `repository.reset_patch` logic. Users currently have no in-UI way to see *why* something failed — they have to read the SQLite database or grep the server log. The retry-all-failed button is the natural counterpart to the "fix it from the UI" affordance, replacing the current flow of clicking "regenerate" 14 times in a row for a 14-patch book where 12 patches failed.

### 8. Worker logs are key=value, not free-form

**Lựa chọn:** Every worker event log line uses the format `event=<name> patch_id=<id> book_id=<id> attempt=<n> [error="<msg>"]` as the log message body, at INFO level (ERROR for failure events). The structured fields are part of the message string, not in `extra={...}`, so the default `logging.basicConfig` formatter prints them inline and tools like `grep`, `awk`, and simple log shippers can parse them without JSON config.

**Lý do:** The project currently uses `logging.basicConfig(level=INFO)` (`app/main.py:18`) with no JSON handler. Sticking with the default formatter keeps the change minimal — no new dependency, no new formatter, no log-config code. The key=value format is also robust to reordering and partial matches (a log shipper that just wants `event=patch.failed` doesn't need a JSON parser).

**Alternative considered:** Structured JSON logs via `python-json-logger` or `structlog`. Rejected — adds a dependency and a config file for a project that has none today. Defer to a later change if/when log shipping is added.

## Risks / Trade-offs

- **Two tables to keep in sync during claim/done logic.** → Mitigation: keep both claim/done helpers in the same `app/repository.py` module; the worker uses the same `db_lock` for both. A `book_job` and a `patch` for the same book can be `processing` at the same time, but they touch independent engine paths (TTS vs ffmpeg) so no resource conflict.
- **`book_job` auto-enqueue runs inside `_merge_final_audio`, which holds `db_lock` only briefly.** → Mitigation: the insert is a single `INSERT` statement inside the same `with self.db_lock:` block as `set_book_final_audio`. Route handlers also acquire the same lock, so there is no race window.
- **Graceful shutdown adds up to 5 minutes to server restart time.** → Mitigation: configurable timeout, logged loudly (`logger.warning("worker did not stop within %s s; cancelling", timeout)`) when the timeout fires, fallback to `requeue_stuck_processing` on next boot. 300s is the *upper* bound; a quiet queue exits in one `poll_interval`.
- **Health endpoint could be used as a DoS amplifier.** → Mitigation: lightweight query (one `SELECT` on `patch` and one on `book_job`, both indexed), no auth needed (standard for k8s liveness), no PII in the response. If this becomes a real concern, rate-limit at the ingress.
- **`UNIQUE(book_id, job_type)` migration is silently a no-op for existing data because the table is new.** No risk to existing rows.
- **Backfill at startup could enqueue many video jobs at once on a large library.** → Mitigation: the backfill is one `SELECT` + one `executemany` of `INSERT`s; even on a 1000-book library this is sub-second. The worker then drains the `book_job` queue at the same rate as TTS, with no special throttling needed.

## Migration Plan

1. Add `book_job` and `app_state` tables to `app/db.py` schema; both use `CREATE TABLE IF NOT EXISTS` so the migration is purely additive and idempotent.
2. Add `BookJob` dataclass to `app/models.py` and CRUD helpers in `app/repository.py`.
3. Update `PatchWorker` to: emit structured logs, check the pause flag, drain `book_job` after `patch` is empty, auto-enqueue video on finalization, support graceful shutdown with deadline, and track `last_heartbeat_at`.
4. Add new routes in `app/routes/queue.py` (health, stats, pause/resume, retry-failed, regenerate-video).
5. Update `book_detail.html` with last-error block + video status row + retry/regenerate buttons; add a small "pending patches" count to `book_list.html`.
6. Convert `POST /books/{id}/video` to enqueue + redirect; remove the synchronous `generate_video` call from the request handler.
7. In `app/main.py` `lifespan` startup, add the one-shot video-job backfill. In `lifespan` shutdown, replace `worker_task.cancel()` with `await asyncio.wait_for(worker_task, timeout=settings.worker_shutdown_timeout_seconds)`.
8. Add `worker_shutdown_timeout_seconds` to `app/config.py`.
9. Smoke test: upload an EPUB, watch patches run via `/queue/stats`, verify `book_job` for video is auto-enqueued after finalization, verify the video file appears at `data/books/{id}/video_{job_id}.mp4`.
10. Rollback: drop the two new tables and revert the worker + route changes. The `patch` table and existing flows are untouched, so the rollback is safe even mid-flight (a `book_job` row that no worker reads is just dead data).

## Open Questions

- Should `/health` be authenticated? → *Default: no*, since it returns no PII and is the standard pattern for k8s liveness. Document this in the spec.
- Should we add a "drain mode" (stop accepting new uploads, finish current work, then exit)? → *Defer* to a separate change if needed. The pause flag in this change already covers the "stop the worker" use case.
- Should pause be per-book or global? → *Defer* per-book to a later change; global is enough for the requested scope and is the simpler primitive to build per-book pause on top of later.
- Should we expose `book_job` job types other than `video` (e.g., a future `summary` or `chapter_rewrite`)? → *Defer*; the `job_type` column is text-typed so adding a new type is just a constant in the worker dispatch, no schema change.
- Should `worker_shutdown_timeout_seconds` default be 300s? → *Yes* for the initial release; revisit if real-world restart times are reported as too long. The setting is configurable so a user with a tight restart SLA can lower it.
