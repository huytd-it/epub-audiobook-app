## ADDED Requirements

### Requirement: Health endpoint
The system SHALL expose `GET /health` returning a JSON body with at least these keys: `status` (`"ok"` or `"degraded"`), `worker_state` (`"idle"`, `"busy"`, or `"paused"`), `current_patch_id` (integer or null), `queue_depth` (integer count of pending patches), and `last_heartbeat_at` (UTC ISO8601 timestamp of the worker's last loop iteration). The endpoint MUST return HTTP 200 when `last_heartbeat_at` is within `3 * worker_poll_interval` seconds of "now" and HTTP 503 otherwise. The endpoint MUST require no authentication.

#### Scenario: Worker is alive and idle
- **WHEN** the worker has just finished a patch and the queue is empty
- **THEN** `GET /health` returns 200 with `status: "ok"`, `worker_state: "idle"`, `current_patch_id: null`, `queue_depth: 0`, and a `last_heartbeat_at` within the last `worker_poll_interval` seconds

#### Scenario: Worker is processing a patch
- **WHEN** the worker has claimed a patch and synthesis is in progress
- **THEN** `GET /health` returns 200 with `status: "ok"`, `worker_state: "busy"`, `current_patch_id: <id>`, `queue_depth: 0`

#### Scenario: Queue is paused
- **WHEN** `app_state['queue.paused']` is `'1'`
- **THEN** `GET /health` returns 200 with `status: "ok"`, `worker_state: "paused"`, `current_patch_id: null`, `queue_depth: <count of pending patches>`

#### Scenario: Worker heartbeat is stale
- **WHEN** `last_heartbeat_at` is older than `3 * worker_poll_interval` seconds
- **THEN** `GET /health` returns 503 with `status: "degraded"` and a human-readable `reason` field

### Requirement: Queue statistics endpoint
The system SHALL expose `GET /queue/stats` returning a JSON object with per-status counts for the `patch` table (`pending`, `processing`, `done`, `failed`), per-status counts for the `book_job` table, the age in seconds of the oldest pending patch (`oldest_pending_patch_age_seconds`, or 0 if no patches are pending), and a `last_errors` array of up to 5 most recent `error_message` values from patches or book_jobs in `failed` state, each entry containing `entity` (`"patch"` or `"book_job"`), `id`, `book_id`, and `updated_at`. Entries in `last_errors` MUST be ordered by `updated_at` descending (most recent first).

#### Scenario: Mixed-state queue
- **WHEN** the queue has 3 pending patches, 1 processing, 50 done, 2 failed and 1 pending video book_job
- **THEN** the response has `patch.pending=3, patch.processing=1, patch.done=50, patch.failed=2`, `book_job.pending=1`, and `last_errors` contains the 2 most recent failed-patch error messages (most recent first)

#### Scenario: Empty queue
- **WHEN** there are no patches and no book_jobs in the system
- **THEN** all counts are 0, `oldest_pending_patch_age_seconds` is 0, `last_errors` is an empty array

#### Scenario: More than 5 failures
- **WHEN** the system has 20 failed patches
- **THEN** `last_errors` contains exactly 5 entries, the 5 most recent ones, and `patch.failed` is 20

### Requirement: Structured worker logging
The system SHALL emit one log line per worker event (`patch.claimed`, `patch.started`, `patch.done`, `patch.failed`, `book_job.claimed`, `book_job.started`, `book_job.done`, `book_job.failed`, `queue.paused`, `queue.resumed`, `worker.heartbeat`) at INFO level (ERROR for failure events). Each line MUST contain the keys `event`, `patch_id` or `book_job_id` (whichever applies), `book_id`, and `attempt` (where applicable), formatted as `key=value` pairs in the log message body so the default Python `logging` formatter prints them inline. The `event=worker.heartbeat` log line MUST be emitted at least once per loop iteration (at the top of `run_forever`).

#### Scenario: Patch claim log
- **WHEN** the worker claims patch 42 of book 7 on attempt 1
- **THEN** a log line of the form `event=patch.claimed patch_id=42 book_id=7 attempt=1` is emitted at INFO

#### Scenario: Patch failure log
- **WHEN** the worker fails patch 42 of book 7 on attempt 1 with error "out of memory"
- **THEN** a log line of the form `event=patch.failed patch_id=42 book_id=7 attempt=1 error="out of memory"` is emitted at ERROR

#### Scenario: Heartbeat is emitted every iteration
- **WHEN** the worker loop iterates 5 times with no work found
- **THEN** 5 `event=worker.heartbeat` log lines are emitted, one per iteration

### Requirement: Pause and resume queue
The system SHALL expose `POST /queue/pause` and `POST /queue/resume` (both require no body, return 303 redirect to `/books` on success) that set an `app_state['queue.paused']` key to `'1'` or `'0'` respectively. While the key is `'1'`, the worker MUST NOT claim new patches or new book_jobs; any patch or book_job already `processing` MUST be allowed to finish. The pause state MUST persist across server restarts.

#### Scenario: Pause stops new claims
- **WHEN** the queue is paused and a new patch is enqueued (e.g., via upload)
- **THEN** the worker does not claim the new patch on subsequent iterations; the patch remains `pending` indefinitely

#### Scenario: In-flight patch finishes during pause
- **WHEN** the queue is paused while patch 5 is `processing`
- **THEN** patch 5 completes normally and is marked `done`; subsequent pending patches are not claimed

#### Scenario: Resume after restart
- **WHEN** the queue was paused and the server is restarted
- **THEN** the queue remains paused (no new claims) until `POST /queue/resume` is called

#### Scenario: Resume flips the flag back
- **WHEN** `POST /queue/resume` is called while the queue is paused
- **THEN** `app_state['queue.paused']` is set to `'0'` and the worker claims new work on subsequent iterations

### Requirement: Retry all failed patches for a book
The system SHALL expose `POST /books/{book_id}/patches/retry-failed` that resets every patch of the book currently in `failed` state back to `pending` (clearing `error_message` and `audio_path`) and returns 303 redirect to `/books/{book_id}`. The endpoint MUST skip patches currently in `processing` state and MUST be a no-op when the book has no failed patches.

#### Scenario: Book with 2 failed patches and 30 done
- **WHEN** book 3 has 2 patches in `failed` state and 30 patches in `done` state
- **THEN** after the call, both failed patches are `pending` with `error_message=NULL` and `audio_path=NULL`; the 30 done patches are unchanged

#### Scenario: Book with a patch in processing
- **WHEN** book 3 has 1 patch in `failed` state and 1 patch in `processing` state
- **THEN** after the call, the failed patch is `pending` and the processing patch is unchanged

#### Scenario: Book with no failed patches
- **WHEN** book 3 has 0 failed patches
- **THEN** the call returns 303 with no DB changes and the response is otherwise normal

### Requirement: Last-error summary on book detail page
The system SHALL render, on the book detail page (`book_detail.html`), a "Last error" block visible only when at least one patch or book_job for that book has a non-NULL `error_message`. The block MUST show the most recent error message and a "Retry all failed patches" form button (POSTing to `/books/{book_id}/patches/retry-failed`).

#### Scenario: Book with a failed patch
- **WHEN** book 7 has 1 failed patch with `error_message="out of memory"`
- **THEN** the book detail page shows a block "Last error: out of memory — [Retry all failed patches]"

#### Scenario: Book with no failures
- **WHEN** book 7 has no failed patches or book_jobs
- **THEN** the "Last error" block is not rendered

#### Scenario: Book with multiple failed entities
- **WHEN** book 7 has 2 failed patches with different error messages, the most recent one updated 1 minute ago
- **THEN** the block shows the message of the most recent failure, not the older one

### Requirement: Graceful worker shutdown
The system SHALL make `worker.stop()` set a stop flag; the worker loop MUST exit at the top of the next iteration only if no patch or book_job is currently `processing`. The FastAPI `lifespan` shutdown MUST wait for the worker task to finish, bounded by `worker_shutdown_timeout_seconds` (default 300, configurable via `app/config.py`). If the timeout fires, the task is cancelled and a WARNING log line is emitted (`event=worker.shutdown_timeout`).

#### Scenario: Normal shutdown with no in-flight patch
- **WHEN** the worker is idle and the server receives SIGTERM
- **THEN** the worker exits within one `worker_poll_interval` and the server shuts down

#### Scenario: Shutdown with patch in flight, finishes in time
- **WHEN** the worker is processing a patch and SIGTERM arrives; the patch finishes within `worker_shutdown_timeout_seconds`
- **THEN** the worker exits cleanly with the patch marked `done`; the server shuts down without a timeout warning

#### Scenario: Shutdown with stuck patch
- **WHEN** the worker is processing a patch and SIGTERM arrives; the patch does not finish within `worker_shutdown_timeout_seconds`
- **THEN** a WARNING is logged, the worker task is cancelled, and the patch is requeued by `requeue_stuck_processing` on the next startup

### Requirement: Worker heartbeat tracking
The system SHALL update a `last_heartbeat_at` UTC timestamp attribute on the worker at the top of every loop iteration, *before* the pause check, so that the timestamp reflects liveness independent of whether work is available.

#### Scenario: Heartbeat updates every iteration
- **WHEN** the worker loop iterates with no pending work for 10 seconds
- **THEN** `worker.last_heartbeat_at` is updated to the current time on each of the 5 iterations (with `poll_interval=2.0`)

#### Scenario: Heartbeat is exposed via the health endpoint
- **WHEN** a client calls `GET /health`
- **THEN** the response's `last_heartbeat_at` field equals `worker.last_heartbeat_at` at the time of the call
