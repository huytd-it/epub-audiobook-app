# Kaggle API TTS Automation Design

## Goal

Replace the manual Colab/Kaggle round trip (export package → upload to Google Drive →
open Kaggle by hand → paste a `GDRIVE_CREDS` secret → click Run All → wait → import
from Drive) with a fully automated path driven by the official Kaggle REST API
(`https://www.kaggle.com/api/v1`). The app SHALL push a kernel, poll it, and pull its
output without any human touching kaggle.com, and SHALL rotate across a pool of Kaggle
accounts so a book is not limited to one account's ~30 GPU-hours/week.

This SHALL fully replace Google Drive for the Kaggle target. The existing Drive
Desktop/API export, the manual zip download, and the Colab notebook path are unaffected
and remain available side by side — this feature adds a third export target
("Kaggle (tự động)") next to "Google Drive" and "Tải zip".

## Non-goals

- Replacing the Drive round trip for Colab (Colab has no equivalent push/pull API and
  keeps its existing manual flow).
- Guaranteeing exact GPU-quota accounting. Kaggle exposes no quota-remaining endpoint;
  the app SHALL keep a local, best-effort usage ledger (see Quota Tracking) and SHALL
  treat a quota-exceeded error from Kaggle itself as the authoritative signal.
- Multi-tenant credential encryption. This is a local single-user app; Kaggle API keys
  are stored the same way Drive OAuth tokens already are — plaintext columns in the
  app's own sqlite database (see `app/google_drive.py`).

## Data model

### `kaggle_account`

Owned by a new module `app/kaggle_accounts.py` (NOT `app/repository.py` — see
Global Constraints in the implementation plan), mirroring how `app/google_drive.py`
owns `drive_account`/`drive_oauth_client` directly instead of going through
`repository.py`.

```sql
CREATE TABLE IF NOT EXISTS kaggle_account (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT NOT NULL,
    username         TEXT NOT NULL,
    api_key          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'idle',  -- idle | busy | cooldown | disabled
    cooldown_until   TEXT,
    in_use_by_job_id INTEGER,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kaggle_account_username ON kaggle_account(username);
```

`disabled` SHALL be a manual state (set from the settings page) that account selection
SHALL always skip, for an account the user wants to keep configured but not used (e.g.
one whose password changed and needs reconnecting).

### `kaggle_usage`

Local, best-effort GPU-time ledger. One row per kernel run (one push→poll→output
cycle), not per job — a job that needs 3 kernel versions to finish a large batch
writes 3 rows.

```sql
CREATE TABLE IF NOT EXISTS kaggle_usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES kaggle_account(id) ON DELETE CASCADE,
    kernel_ref   TEXT NOT NULL,      -- "username/slug"
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    gpu_seconds  INTEGER,            -- NULL while the run is in flight
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kaggle_usage_account ON kaggle_usage(account_id, started_at DESC);
```

Remaining quota for an account SHALL be computed as
`weekly_quota_seconds - SUM(gpu_seconds WHERE account_id=? AND started_at >= now - 7 days)`,
clamped to zero. `weekly_quota_seconds` SHALL come from
`settings.kaggle_weekly_gpu_quota_hours` (default 30h), not hardcoded, since Kaggle has
changed this number before and phone-verified accounts sometimes get more.

### `patch_export` (existing table) — two new nullable columns

```sql
ALTER TABLE patch_export ADD COLUMN kaggle_account_id INTEGER;
ALTER TABLE patch_export ADD COLUMN kaggle_kernel_ref TEXT;
```

Added via the existing `_migrate()` "check `PRAGMA table_info`, `ALTER TABLE ADD COLUMN`
if missing" pattern in `app/db.py` — no new migration mechanism needed. These sit
alongside the existing `drive_account_id`/`sync_target_id` columns exactly the way those
two already coexist as alternate nullable provenance fields on the same row.

## Key reuse: `local_folder_path` already means "read a batch package from here"

The existing import path (`import_patch_from_drive` in `app/routes/patches.py`) does not
care *how* a local folder got populated — Drive Desktop sync, rclone, and a future
Kaggle-download all produce "a directory containing `batch_manifest.json` +
`patches/patch_NNN/manifest.json` + `result/<book>_<episode>.wav` (+`.timeline.json`)",
and the importer already walks up from a patch's folder to find `batch_manifest.json`
and resolves `result_wav` from it (`_resolve_batch_result`).

The Kaggle job handler SHALL exploit this directly: after downloading a kernel's
output, it SHALL materialize a local staging directory shaped exactly like an existing
batch export package (it already has `batch_manifest.json` and every `manifest.json`
from the push step; it only needs to write the downloaded `result/*.wav` and
`result/*.timeline.json` files into it), then invoke the same import routine the Drive
Desktop path uses. No new import logic, no new WAV/timeline validation path.

This requires one refactor: `_resolve_batch_result`, `_build_import_timeline`,
`_timeline_metadata`, `_install_imported_wav`, `_safe_batch_path`, and the
orchestration currently inlined in `import_patch_from_drive` SHALL move to a new
`app/patch_import.py` module with a single entry point:

```python
def import_batch_patch(conn, patch: Patch, package_folder: Path, *, chunk_pause_ms: int) -> ImportOutcome
```

`app/routes/patches.py` SHALL call this instead of its own inlined logic (pure
refactor — behavior unchanged, existing tests SHALL still pass unmodified). The
jobqueue handler (which SHALL NOT import from `app/routes/*`, per the job queue's own
routing/handler separation) imports `app.patch_import` instead.

## `app/kaggle_api.py` — HTTP client

Raw `urllib`-based client, same style as `app/tts_api_providers.py._request` — no
`kaggle` pip package dependency (it pulls in a CLI, argument parsing, and its own retry
logic the app does not want).

```python
@dataclass
class KaggleAccount:
    username: str
    api_key: str

def push_kernel(account: KaggleAccount, package_dir: Path, metadata: dict) -> str        # -> kernel_ref "username/slug"
def kernel_status(account: KaggleAccount, kernel_ref: str) -> KernelStatus               # queued|running|complete|error|cancelled
def kernel_output(account: KaggleAccount, kernel_ref: str, dest_dir: Path) -> list[Path]  # downloads every output file, returns local paths
def cancel_kernel(account: KaggleAccount, kernel_ref: str) -> None                       # best-effort; failures are swallowed
def create_dataset(account: KaggleAccount, package_dir: Path, slug: str, title: str) -> str  # -> dataset_ref "username/slug"
```

`metadata` mirrors `kernel-metadata.json`: `id` (`f"{username}/{slug}"`), `title`,
`code_file`, `language: "python"`, `kernel_type: "notebook"`, `is_private: true`,
`enable_gpu: true`, `enable_internet: true` (the notebook downloads model weights from
Hugging Face), `dataset_sources: [dataset_ref]`.

**UPDATE (Task 12, verified against Kaggle's own official SDK source — not a live
account, see below):** the original plan above ("small payloads travel as the kernel's
own attached files, `create_or_update_dataset` is an out-of-scope extension point") was
wrong and has been corrected in the implementation. Reading
`github.com/Kaggle/kaggle-cli`'s `kaggle_api_extended.py` and
`github.com/Kaggle/kaggle-sdk-python`'s generated request classes shows:

- **A kernel push carries exactly one file** — the notebook itself, as a single `text`
  field (its cells' `outputs` stripped and each cell's `source` list joined into one
  string). There is no mechanism to attach arbitrary local files to a kernel push.
  Any other data (our manifest + reference clip) **must** travel as a Kaggle Dataset,
  referenced by slug in `dataset_sources` — this is not optional, so `create_dataset`
  is now part of the core `kaggle_api.py` surface, not an extension point.
- **Auth is HTTP Basic** (`Authorization: Basic base64(username:api_key)`), not Bearer.
  Confirmed by `KaggleHttpClient._try_fill_auth` setting `session.auth = (username,
  password)`, which is exactly what `requests` treats as HTTP Basic.
- **Every call is POST** to `https://api.kaggle.com/v1/{service}.{Service}/{Method}`
  (e.g. `kernels.KernelsApiService/SaveKernel`, `.../GetKernelSessionStatus`,
  `.../ListKernelSessionOutput`; `blobs.BlobApiService/StartBlobUpload`;
  `datasets.DatasetApiService/CreateDataset`) — not REST-with-query-params against
  `www.kaggle.com/api/v1`.
- **Datasets are built from individually-uploaded blobs**: `StartBlobUpload` returns a
  `token` + a presigned `createUrl`; the raw file bytes are `PUT` there with no Kaggle
  auth header; `CreateDataset` then references the returned tokens.
- **Kernel status values are upper-snake-case**: `QUEUED`, `RUNNING`, `COMPLETE`,
  `ERROR`, `CANCEL_REQUESTED`, `CANCEL_ACKNOWLEDGED`, `NEW_SCRIPT` — not the
  lowercase/camelCase originally guessed.

`app/kaggle_api.py`'s module docstring carries the same findings plus the citations.
`create_dataset` always creates a brand-new dataset per push cycle rather than
versioning one in place (simpler; leaves small throwaway datasets behind — a periodic
cleanup is a reasonable follow-up, not implemented). `cancel_kernel` is a **documented
no-op**: `CancelKernelSession` needs a numeric `kernel_session_id` that no other call
in this module (push/status/output) surfaces anywhere, and this was not resolved.
Callers already treat cancellation as best-effort, so this degrades safely — the job
just stops polling and returns without confirming Kaggle itself stopped the kernel.

**Still not verified — genuinely needs a live account** (source-reading closes the gap
on wire shapes, not on whether Kaggle's backend actually behaves as its own client
code implies):

- An end-to-end real run: push a small batch, confirm it queues/runs/completes, confirm
  `kernel_output` returns real files with a real `fileName`/`url` shape matching what
  was assumed here.
- Whether `CC0-1.0` (hardcoded as the dataset license) is accepted, or needs to be
  configurable.
- Where a numeric `kernel_session_id` can actually be obtained, to make `cancel_kernel`
  do something.

None of the above affects the architecture below — only `kaggle_api.py`'s internals.

## Account selection & quota tracking (`app/kaggle_accounts.py`)

```python
def claim_idle_account(conn, job_id: int) -> KaggleAccountRow | None
def release_account(conn, account_id: int, *, cooldown_until: str | None = None) -> None
def remaining_quota_seconds(conn, account_id: int) -> int
def record_usage_start(conn, account_id: int, kernel_ref: str) -> int          # -> usage row id
def record_usage_finish(conn, usage_id: int, gpu_seconds: int) -> None
def earliest_quota_reset(conn) -> str | None                                   # across all accounts, for reschedule
```

`claim_idle_account` SHALL atomically pick one account that is `idle`, or `cooldown`
whose `cooldown_until` has passed (self-heals a stale cooldown), ordered by
least-recently-used, via one `UPDATE ... WHERE id = (SELECT ...) RETURNING *` — the
same atomic-claim pattern `jobqueue/store.py::claim` already uses for jobs, for the
same reason (two `kaggle_tts` jobs must never grab the same account).

`release_account` SHALL set the account back to `idle` (job succeeded or account still
has quota) or `cooldown` with a computed `cooldown_until` (Kaggle's own API rejected the
push/run for quota reasons). `disabled` is never set by the queue — only by the settings
page.

## Notebook: one template, one new `MODE` flag

Rather than fork `colab_kaggle_batch_tts_template.ipynb` into a second, near-duplicate
notebook (the existing design doc for the previous notebook refactor already flags
"old export compatibility" drift as a real cost), the existing notebook SHALL gain a
second global next to the existing `IS_KAGGLE` flag (`tests/test_notebook_templates.py`
already pins `IS_KAGGLE = False` as the first line of cell 1 and forbids any
platform auto-detection — that flag stays exactly as-is and keeps meaning
Colab-vs-Kaggle quirks like the `google.colab` import guard):

```python
IS_KAGGLE = False
MODE = "__MODE__"   # "drive" | "kaggle_native" — set by the export builder; only
                     # meaningful when IS_KAGGLE is True (Colab has no push/pull API
                     # and always behaves as "drive")
```

Every cell that currently mounts Drive / reads `GDRIVE_CREDS` SHALL be guarded by
`if MODE == "drive":` (which is unconditionally true whenever `IS_KAGGLE` is False, so
Colab's behavior is unchanged bit-for-bit). Input/output path resolution SHALL branch once:

```python
INPUT_ROOT  = Path("/kaggle/input/epub-tts-batch") if MODE == "kaggle_native" else DRIVE_BATCH_FOLDER
OUTPUT_ROOT = Path("/kaggle/working")              if MODE == "kaggle_native" else DRIVE_BATCH_FOLDER
```

Cell 8's chunk/merge/pause/timeline/SKIP_EXISTING logic is unchanged either way — it
already only reads `INPUT_ROOT/patches/.../manifest.json` and writes
`OUTPUT_ROOT/result/...`; only *how those roots are populated* differs. In
`kaggle_native` mode there is nothing to "upload back" — files written under
`/kaggle/working/result/` are Kaggle's own kernel output and SHALL survive a
forced session timeout the same way partial output already survives today (this is
what makes multi-session continuation possible at all).

## `app/drive_export.py` — Kaggle-native package builder

A new function alongside the existing `build_batch_export_package`:

```python
def build_kaggle_export_package(conn, patches, *, model_id, voice_id=None, max_chars=0,
                                 with_effects=False, hf_token=None) -> tuple[Path, dict]
```

Same manifest/reference-clip construction as today, minus everything Drive-specific
(`gdrive_creds`, the Drive-mode notebook placeholders). It SHALL set the notebook's
`__MODE__` placeholder to `"kaggle_native"` instead of `"drive"`. `build_batch_export_package`
SHALL gain a `mode: str = "drive"` parameter internally so both builders share the same
manifest-writing code (`_write_patch_files`, `folder_name_for_batch`, etc.) rather than
duplicating it.

## Job type `kaggle_tts` (new jobqueue handler)

Registered in `app/jobqueue/backfill.py::build_queue` exactly like every other handler:
`queue.register("kaggle_tts", kaggle_tts.handle, cancellable=True)`.

**Payload:** `{"book_id": int, "patch_ids": list[int], "tts": {...}, "chunk_pause_ms": int, "chapter_pause_ms": int}` —
the same shape `build_kaggle_export_package` already consumes.

**Lifecycle** (`app/jobqueue/handlers/kaggle_tts.py::handle(ctx)`), run inside the
queue's own thread pool like `video`/`youtube_upload`, so blocking `time.sleep` polling
is fine:

1. Loop while patches remain unimported:
   a. `claim_idle_account(conn, ctx.job.id)`. If none available, go to step 4.
   b. Build the package (`drive_export.build_kaggle_export_package`, excluding
      already-imported patches from this run).
   c. `kaggle_api.push_kernel(...)`; `kaggle_accounts.record_usage_start(...)`.
   d. Poll `kaggle_api.kernel_status(...)` every `settings.kaggle_poll_interval_seconds`,
      calling `ctx.heartbeat()` each iteration (same pattern `video`/`youtube_upload`
      already use for long blocking steps) and checking `ctx.should_cancel()` — if
      cancelled, best-effort `kaggle_api.cancel_kernel(...)`, release the account, and
      `return None` (the runner itself marks the job cancelled once the handler
      returns, per the existing `_execute` contract — no exception needed).
   e. On `complete` or `error` or a status that indicates the run was force-stopped:
      `kaggle_api.kernel_output(...)` into a staging dir shaped like a batch package
      (write the already-known `batch_manifest.json`/`manifest.json` files alongside
      the downloaded `result/*`), then `patch_import.import_batch_patch(...)` per patch
      whose result file is present. `record_usage_finish(...)` with the elapsed wall
      time as the `gpu_seconds` estimate.
   f. If some patches are still missing and the account's `remaining_quota_seconds` is
      still positive → loop back to (b) with the same account, pushing a new kernel
      version (SKIP_EXISTING marks already-imported patches to skip).
   g. Otherwise → `release_account(..., cooldown_until=...)` if quota looks exhausted
      (or `idle` if it just hit the session time cap with quota to spare), and loop
      back to (a) to try a different account for the remaining patches.
2. If all patches imported → return (job finishes normally).
3. (loop continues at 1)
4. No account is idle or past cooldown: raise `JobRescheduled(earliest_quota_reset(conn) or <+6h fallback>, "no Kaggle GPU quota available in any account this week")`.

**Failure classes:**
- Bad manifest / no reference clip / unknown model → `JobFatalError` (same as every
  other export path already raises for these — see `_voice_clip_or_raise`).
- Transient network/API errors talking to Kaggle → let the normal exception fall
  through to the queue's existing retry/backoff (capped at 600s, 3 attempts by
  default) — these are exactly what that mechanism is for.
- No quota anywhere → `JobRescheduled`, a new, distinct outcome (see below) — NOT a
  failure, and NOT a normal 600s-capped retry (polling every 10 minutes for days waiting
  on a weekly reset is wasteful and pointless).

## `JobRescheduled` — new small primitive in the job queue

`store.fail`'s backoff is capped at 600 seconds and is meant for transient errors, not
"come back in up to a week." Add one small, isolated primitive, used only by
`kaggle_tts`:

- `app/jobqueue/models.py`: `class JobRescheduled(Exception): def __init__(self, next_retry_at: str, message: str | None = None)`.
- `app/jobqueue/store.py`: `def reschedule(conn, job_id, next_retry_at: str, message: str | None = None, *, worker_id=None) -> bool` —
  returns the job to `pending` at the given timestamp without touching `attempt_count`
  or `max_attempts` (this is not a retry against a budget; the job did not fail).
- `app/jobqueue/runner.py::_execute`: a new `except JobRescheduled as exc:` branch
  (checked before the generic `except Exception:`) calling `store.reschedule(conn, job.id, exc.next_retry_at, exc.message, worker_id=job.worker_id)`.

This SHALL NOT change behavior for any existing job type — `JobRescheduled` is never
raised outside `kaggle_tts`.

## Routes

New `app/routes/kaggle.py`, mirroring `app/routes/drive.py`'s Form/Redirect pattern for
account CRUD plus one JSON aggregate endpoint for the frontend:

- `GET /api/ui/kaggle` — `{"accounts": [...], "exports": [...]}` (accounts include a
  computed `remaining_quota_hours` for display).
- `POST /kaggle/accounts` (`label`, `username`, `api_key`) — create.
- `POST /kaggle/accounts/{id}/edit`
- `POST /kaggle/accounts/{id}/delete` — refuses (400) while `in_use_by_job_id IS NOT NULL`.
- `POST /kaggle/accounts/{id}/toggle` — flips `idle`/`disabled`.

New endpoint in `app/routes/patches.py`, alongside `export_batch_to_drive`/
`export_batch_to_drive_api`:

- `POST /books/{book_id}/patches/export-batch-kaggle` (`patch_ids`, `model_id`,
  `voice_id`, `max_chars`, `with_effects`) — validates the selection
  (`_load_batch_patches`, same as the other two export routes), enqueues one
  `kaggle_tts` job via `jobqueue.store.enqueue` with `dedupe_key=f"kaggle_tts:book={book_id}"`
  (one in-flight Kaggle job per book, same reasoning as existing job dedupe keys),
  returns the job id as JSON so the frontend can link to it in the Queue page.

## Frontend

`frontend/src/pages/book-detail/ExportPanel.tsx` gains a third option, "Kaggle (tự
động)", next to the existing Drive/zip options — picks patches + TTS config the same
way, then `postJson("/books/{id}/patches/export-batch-kaggle", ...)` and shows a link
to the enqueued job on the Queue page (which already renders job progress/logs
generically — no new UI needed there).

A new settings page (or a new tab on the existing `/drive` page — TBD at
implementation time, whichever reads more natural once the account list exists) lists
`kaggle_account` rows with label, username, status, and estimated remaining quota, with
add/edit/delete/toggle actions — same shape as the existing Drive accounts section in
`DrivePage.tsx`.

## Config additions (`app/config.py`)

```python
kaggle_poll_interval_seconds: int = 30
# Kaggle GPU notebook sessions are capped; this is the app's own safety timeout for one
# push→poll cycle, independent of whatever Kaggle currently enforces.
kaggle_max_session_hours: int = 9
kaggle_weekly_gpu_quota_hours: int = 30
queue_concurrency: str = "...,kaggle_tts=<number of configured accounts, computed at boot>"
```

`kaggle_tts` concurrency SHALL be set to the number of enabled `kaggle_account` rows at
boot (via `configured_concurrency`, same mechanism `queue_concurrency` already uses),
capped by whatever `QUEUE_CONCURRENCY` explicitly overrides — this keeps two jobs from
ever contending for fewer accounts than are configured.

## Error handling

- A kernel that fails Kaggle-side (`status: error`) with patches still missing SHALL
  still attempt to import whatever `result/*` files did land before the failure — partial
  progress is never discarded.
- A malformed/unreadable downloaded result WAV SHALL be treated exactly like today's
  "corrupt notebook result" case: skip that patch (leave it for the next kernel
  version/import cycle), do not fail the whole job.
- `cancel_kernel` failures SHALL be logged and swallowed — a job the user cancelled
  SHALL still be marked cancelled locally even if the best-effort remote cancel call
  itself fails.
- Account CRUD SHALL refuse to delete an account currently `in_use_by_job_id` (400),
  matching the existing refusal pattern for Drive OAuth clients still referenced by an
  account (`google_drive.delete_client`).

## Testing

- `kaggle_accounts.py`: atomic claim under concurrent callers (mirrors
  `test_claim_is_atomic_across_threads`), cooldown self-heals once `cooldown_until`
  passes, quota math over the 7-day window, `disabled` is never returned by claim.
- `store.reschedule`: returns a job to `pending` at the given timestamp without
  incrementing `attempt_count`; a fenced call from a reaped worker is a no-op (same
  fencing tests as `finish`/`fail`).
- `patch_import.import_batch_patch`: existing Drive-import test coverage moves here
  unchanged (pure refactor — `tests/test_export_reference_required.py` and friends
  SHALL still pass with no edits beyond the import path).
- `kaggle_tts.handle`: fake `kaggle_api` module (no real network) driving through
  queued→running→complete, quota exhaustion mid-batch triggering account rotation, and
  the "no account has quota" path raising `JobRescheduled` with a sane timestamp.
- Notebook: extend `tests/test_notebook_templates.py`'s existing JSON/cell assertions
  to also check the new `MODE` cell and that `kaggle_native` branches never reference
  Drive-only symbols.

## Out of Scope

- Versioning a Dataset in place across a batch's continuation cycles (Task 12 update:
  a fresh dataset is created every push cycle instead — see `create_dataset`'s
  docstring; periodic cleanup of old `epub-tts-data-*` datasets is not implemented).
- A UI for manually editing `kaggle_usage` rows or overriding the quota estimate.
- Automatically registering a Kaggle account (OAuth-less API-key accounts are added by
  hand, same as the user already does for Drive OAuth clients).
- Colab automation (no equivalent push/pull API exists for Colab).
