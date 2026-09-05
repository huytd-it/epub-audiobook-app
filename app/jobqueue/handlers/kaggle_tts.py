"""kaggle_tts job: push a batch to the Kaggle Kernels API, poll it, import its
output, and keep going until every requested patch is synthesized -- rotating
across the account pool when one account's GPU quota runs out, and rescheduling
the whole job (not failing it) when no account has any quota left this week.

See docs/superpowers/specs/2026-09-05-kaggle-api-tts-automation-design.md for the
full lifecycle this implements."""
from __future__ import annotations

import logging
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import drive_export, kaggle_accounts, kaggle_api, patch_import, repository
from app.config import settings
from app.jobqueue.context import JobContext
from app.jobqueue.models import JobFatalError, JobRescheduled
from app.kaggle_api import KernelStatus
from app.patch_publishing import on_patch_audio_ready

logger = logging.getLogger(__name__)

# When no account has quota and there is no usage history to estimate a real reset
# time from (kaggle_accounts.earliest_quota_reset returns None), come back in a few
# hours rather than guessing a full week - a fresh account could be added any time.
_FALLBACK_RESCHEDULE_HOURS = 6
# Same fallback for a single account's cooldown when it ran out of quota but has no
# usage history of its own to estimate a reset from (should not normally happen -
# remaining_quota_seconds only reaches 0 once usage exists).
_FALLBACK_COOLDOWN_DAYS = 7


def _missing_patch_ids(conn, book_id: int, patch_ids: list[int]) -> list[int]:
    """patch_ids not yet synthesized: missing from the DB, belonging to a different
    book, or not status='done'."""
    missing = []
    for patch_id in patch_ids:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id or patch.status != "done":
            missing.append(patch_id)
    return missing


def _kernel_slug(book_id: int, patch_ids: list[int]) -> str:
    """Stable across every push in this job (based on the ORIGINAL full patch_ids,
    not the shrinking missing list), so re-pushing to the same account creates a new
    version of the same kernel instead of a fresh one each cycle."""
    ids = "-".join(str(p) for p in sorted(patch_ids))
    return f"epub-tts-batch-{book_id}-{ids}"[:120]


def _kernel_metadata(username: str, slug: str, title: str, dataset_ref: str) -> dict:
    return {
        "id": f"{username}/{slug}",
        "title": title,
        "code_file": "colab_kaggle_batch_tts_template.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        "enable_internet": True,
        "dataset_sources": [dataset_ref],
    }


def handle(ctx: JobContext) -> dict | None:
    payload = ctx.job.payload
    book_id = int(payload["book_id"])
    patch_ids = [int(p) for p in payload["patch_ids"]]
    model_id = payload.get("model_id") or "voxcpm2"
    voice_id = payload.get("voice_id")
    max_chars = int(payload.get("max_chars") or 0)
    with_effects = bool(payload.get("with_effects"))

    conn = ctx.conn
    account: dict | None = None
    package_dir: Path | None = None
    try:
        while True:
            missing = _missing_patch_ids(conn, book_id, patch_ids)
            if not missing:
                return {"imported": len(patch_ids)}

            patches = [p for p in (repository.get_patch(conn, pid) for pid in missing) if p is not None]
            if not patches:
                raise JobFatalError("none of the pending patch_ids exist anymore")

            if account is None:
                account = kaggle_accounts.claim_idle_account(conn, ctx.job.id)
                if account is None:
                    reset = kaggle_accounts.earliest_quota_reset(conn)
                    fallback = (
                        datetime.now(timezone.utc) + timedelta(hours=_FALLBACK_RESCHEDULE_HOURS)
                    ).isoformat()
                    raise JobRescheduled(
                        reset or fallback,
                        "no Kaggle GPU quota available in any account this week",
                    )

            try:
                package_dir, _batch_manifest = drive_export.build_kaggle_export_package(
                    conn, patches, model_id=model_id, voice_id=voice_id,
                    max_chars=max_chars, with_effects=with_effects,
                )
            except ValueError as exc:
                raise JobFatalError(str(exc)) from exc

            account_ref = kaggle_api.KaggleAccount(username=account["username"], api_key=account["api_key"])
            slug = _kernel_slug(book_id, patch_ids)

            # A kernel push carries only its own notebook text -- the manifest and
            # reference clip travel as a Dataset instead, referenced by slug in the
            # kernel's dataset_sources. A fresh dataset every cycle (see kaggle_api's
            # create_dataset docstring) rather than versioning one in place.
            dataset_slug = f"epub-tts-data-{book_id}-{uuid.uuid4().hex[:8]}"
            ctx.log(f"Uploading batch data as dataset {account['username']}/{dataset_slug}")
            dataset_ref = kaggle_api.create_dataset(
                account_ref, package_dir, dataset_slug, f"EPUB TTS data book {book_id}",
            )

            ctx.log(f"Pushing kernel {account['username']}/{slug} ({len(patches)} patch(es))")
            kernel_ref = kaggle_api.push_kernel(
                account_ref, package_dir,
                _kernel_metadata(account["username"], slug, f"epub-tts batch {book_id}", dataset_ref),
            )
            usage_id = kaggle_accounts.record_usage_start(conn, account["id"], kernel_ref)
            started_at = time.monotonic()

            status = kaggle_api.kernel_status(account_ref, kernel_ref)
            while status in (KernelStatus.QUEUED, KernelStatus.RUNNING):
                if ctx.should_cancel():
                    kaggle_api.cancel_kernel(account_ref, kernel_ref)
                    kaggle_accounts.record_usage_finish(conn, usage_id, int(time.monotonic() - started_at))
                    ctx.log("Tác vụ đã bị hủy; đã yêu cầu hủy kernel trên Kaggle")
                    return None
                time.sleep(settings.kaggle_poll_interval_seconds)
                ctx.heartbeat()
                status = kaggle_api.kernel_status(account_ref, kernel_ref)

            kaggle_accounts.record_usage_finish(conn, usage_id, int(time.monotonic() - started_at))
            ctx.log(f"Kernel {kernel_ref} finished with status={status.value}")

            kaggle_api.kernel_output(account_ref, kernel_ref, package_dir)

            for patch in patches:
                patch_folder = package_dir / "patches" / f"patch_{patch.patch_index:03d}"
                result = patch_import.resolve_batch_result(patch_folder, patch.id)
                if result is None or not result.is_file():
                    continue
                audio_path = Path(settings.data_root) / "books" / str(book_id) / "patches" / f"{patch.id}.wav"
                try:
                    patch_import.install_imported_wav(result, audio_path)
                except Exception:
                    logger.warning(
                        "Kaggle result WAV invalid for patch %s; will retry next cycle",
                        patch.id, exc_info=True,
                    )
                    continue
                repository.mark_patch_done(conn, patch.id, str(audio_path))
                on_patch_audio_ready(conn, patch.id)
                ctx.log(f"Imported patch {patch.id} from kernel {kernel_ref}")

            shutil.rmtree(package_dir, ignore_errors=True)
            package_dir = None

            if not _missing_patch_ids(conn, book_id, patch_ids):
                return {"imported": len(patch_ids)}

            if kaggle_accounts.remaining_quota_seconds(conn, account["id"]) <= 0:
                cooldown_until = (
                    kaggle_accounts.account_quota_reset(conn, account["id"])
                    or (datetime.now(timezone.utc) + timedelta(days=_FALLBACK_COOLDOWN_DAYS)).isoformat()
                )
                ctx.log(f"Account {account['username']} out of quota; cooling down until {cooldown_until}")
                kaggle_accounts.release_account(conn, account["id"], cooldown_until=cooldown_until)
                account = None
            # else: loop again, pushing a continuation version to the same account/kernel
    finally:
        if package_dir is not None:
            shutil.rmtree(package_dir, ignore_errors=True)
        if account is not None:
            kaggle_accounts.release_account(conn, account["id"])
