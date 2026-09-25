"""kaggle_tts job: push a batch to the Kaggle Kernels API, poll it, import its
output, and keep going until every requested patch is synthesized -- rotating
across the account pool when one account's GPU quota runs out, and rescheduling
the whole job (not failing it) when no account has any quota left this week.

See docs/superpowers/specs/2026-09-05-kaggle-api-tts-automation-design.md for the
full lifecycle this implements."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import drive_export, kaggle_accounts, kaggle_api, patch_import, repository
from app.config import settings
from app.jobqueue.context import JobContext
from app.jobqueue.models import JobFatalError, JobRescheduled
from app.kaggle_api import KernelStatus
from app.patch_publishing import (
    enqueue_patch_video,
    fetch_thumbnail_inputs,
    on_patch_audio_ready,
    warm_patch_thumbnail,
)

logger = logging.getLogger(__name__)

# When automation runs: "per_patch" chains video/YouTube right after each patch's
# audio lands (progressive, resilient to a mid-batch failure); "after_all" marks
# every patch done first and automates the whole set once nothing is missing
# (safer, all-or-nothing). Route default is "after_all".
AUTOMATION_PER_PATCH = "per_patch"
AUTOMATION_AFTER_ALL = "after_all"

# When no account has quota and there is no usage history to estimate a real reset
# time from (kaggle_accounts.earliest_quota_reset returns None), come back in a few
# hours rather than guessing a full week - a fresh account could be added any time.
_FALLBACK_RESCHEDULE_HOURS = 6
_BUSY_ACCOUNT_RETRY_MINUTES = 5
# How long to wait for a freshly-created dataset to finish processing before
# pushing the kernel that attaches it. Pushing early does NOT fail -- Kaggle
# silently drops the not-yet-ready source and the kernel runs without input.
_DATASET_READY_TIMEOUT_SECONDS = 600


def _authoritative_quota(ctx: JobContext, account: dict) -> kaggle_api.GpuQuota | None:
    account_ref = kaggle_api.KaggleAccount(
        username=account["username"], api_key=account["api_key"],
    )
    try:
        return kaggle_api.gpu_quota(account_ref)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        # Quota lookup is a guard, not a reason to block valid work when this
        # auxiliary endpoint is temporarily unavailable.
        ctx.log(
            f"Không đọc được quota trực tiếp của {account['username']}; vẫn thử chạy: {exc}",
            level=logging.WARNING,
        )
        return None


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
    version of the same kernel instead of a fresh one each cycle.

    The patch ids are hashed rather than spelled out: a 50-patch batch would produce a
    slug hundreds of characters long, and this slug doubles as the kernel title (see
    _kernel_metadata)."""
    ids = ",".join(str(p) for p in sorted(patch_ids))
    digest = hashlib.sha1(ids.encode("utf-8")).hexdigest()[:10]
    return f"epub-tts-batch-{book_id}-{digest}"


def _kernel_metadata(username: str, slug: str, _legacy_dataset_ref: str | None = None) -> dict:
    """The title is the slug verbatim, and deliberately so: Kaggle derives a NEW
    kernel's slug from its title and ignores the slug the push asked for when the two
    disagree, so a prettier title ("epub-tts batch 18") lands the kernel on a
    different slug than the one every later push and status poll uses -- which is how
    the first live run died with HTTP 409 ALREADY_EXISTS on its second attempt. Since
    the slug is already lowercase-and-hyphens, slugify(title) == slug holds."""
    return {
        "id": f"{username}/{slug}",
        "title": slug,
        "code_file": "colab_kaggle_batch_tts_template.ipynb",
        "language": "python",
        "kernel_type": "notebook",
        "is_private": True,
        "enable_gpu": True,
        "machine_shape": settings.kaggle_machine_shape,
        "enable_internet": True,
        "dataset_sources": [],
    }


def _push_kernel(
    account_ref: kaggle_api.KaggleAccount, package_dir: Path, username: str, slug: str,
) -> str:
    """Push, turning Kaggle's title collision into a fatal error rather than letting
    the job spend its remaining attempts on it: the title is the slug, so every retry
    sends the identical title and earns the identical 409 -- after uploading another
    throwaway dataset first."""
    try:
        return kaggle_api.push_kernel(
            account_ref, package_dir, _kernel_metadata(username, slug),
        )
    except RuntimeError as exc:
        if "ALREADY_EXISTS" not in str(exc):
            raise
        raise JobFatalError(
            f"Kaggle đã có notebook mang tên '{slug}', nhiều khả năng còn lại từ một lần "
            f"chạy hỏng trước đó. Vào kaggle.com/code xoá notebook đó rồi chạy lại. "
            f"Chi tiết: {exc}"
        ) from exc


# Signature Cell 4 (notebook template, nhánh kaggle_native) in ra khi kernel chạy
# mà không có dataset nào được mount: dataset đã được SaveKernel chấp nhận lúc
# push nhưng phiên chạy bắt đầu quá sớm, Kaggle chưa mount được input.
_MISSING_INPUT_SIGNATURE = "No attached Kaggle input"
# Số lần push lại kernel với cùng một dataset (đã già đi sau mỗi lần đợi) khi gặp
# lỗi trên, trước khi chịu thua. Mỗi lần chạy lỗi chỉ tốn ~1 phút quota (Cell 4
# chết ở assert sau ~16s), rẻ hơn nhiều so với bỏ cả job.
_ATTACH_RETRY_ATTEMPTS = 3
_ATTACH_RETRY_DELAY_SECONDS = 120.0


def _fetch_kernel_log_text(
    ctx: JobContext, account_ref: kaggle_api.KaggleAccount, kernel_ref: str,
) -> str:
    """Đọc session log của kernel; lỗi khi đọc thì trả "" để caller dùng message
    chung thay vì làm hỏng cả job vì một call chẩn đoán."""
    try:
        return kaggle_api.kernel_log(account_ref, kernel_ref)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        ctx.log(f"Không đọc được kernel log của {kernel_ref}: {exc}", level=logging.WARNING)
        return ""


def _sleep_with_heartbeat(ctx: JobContext, seconds: float) -> None:
    """Đợi một khoảng cố định nhưng vẫn heartbeat để reaper không tưởng job chết,
    và vẫn tôn trọng hủy giữa chừng."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if ctx.should_cancel():
            raise asyncio.CancelledError()
        time.sleep(min(max(settings.kaggle_poll_interval_seconds, 1.0), remaining))
        ctx.heartbeat()


def _wait_dataset_ready(
    ctx: JobContext, account_ref: kaggle_api.KaggleAccount, dataset_ref: str,
) -> None:
    """Block until the fresh dataset finishes processing. Pushing the kernel early
    does not fail -- Kaggle silently drops the not-ready source (SaveKernel's
    invalidDatasetSources, which push_kernel() now turns into an error) and the
    kernel runs with no attached input, dying later in Cell 4's assert."""
    ctx.log(f"Waiting for dataset {dataset_ref} to become ready")
    deadline = time.monotonic() + _DATASET_READY_TIMEOUT_SECONDS
    last_status = ""
    while True:
        try:
            status = kaggle_api.dataset_status(account_ref, dataset_ref)
        except RuntimeError as exc:
            # Kaggle answers GetDatasetStatus with HTTP 403 PERMISSION_DENIED
            # ("Permission 'datasets.get' was denied") when the fresh private
            # dataset is not yet visible/indexed -- observed live 2026-09-06 on
            # the very first poll after CreateDataset. The dataset exists and
            # becomes queryable seconds later, so treat this as "still pending"
            # instead of failing the job (which would retry with yet another
            # throwaway dataset). Any other error still raises immediately.
            msg = str(exc)
            if "HTTP 403" not in msg and "PERMISSION_DENIED" not in msg:
                raise
            status = "pending"
            if status != last_status:
                ctx.log(
                    f"Dataset {dataset_ref} status={status} "
                    f"(GetDatasetStatus 403, still propagating)",
                    level=logging.WARNING,
                )
                last_status = status
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"Dataset {dataset_ref} still not ready after "
                    f"{_DATASET_READY_TIMEOUT_SECONDS}s (GetDatasetStatus kept 403)"
                ) from exc
            if ctx.should_cancel():
                raise asyncio.CancelledError()
            time.sleep(settings.kaggle_poll_interval_seconds)
            ctx.heartbeat()
            continue
        if status == "ready":
            ctx.log(f"Dataset {dataset_ref} is ready")
            return
        if status in kaggle_api.DATASET_FAILED_STATUSES:
            raise JobFatalError(
                f"Dataset {dataset_ref} của Kaggle ở trạng thái {status}, không dùng được. "
                f"Mở kaggle.com kiểm tra dataset rồi chạy lại."
            )
        if status != last_status:
            ctx.log(f"Dataset {dataset_ref} status={status}")
            last_status = status
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Dataset {dataset_ref} still not ready after "
                f"{_DATASET_READY_TIMEOUT_SECONDS}s (last status={status or 'unknown'})"
            )
        if ctx.should_cancel():
            raise asyncio.CancelledError()
        time.sleep(settings.kaggle_poll_interval_seconds)
        ctx.heartbeat()


def _verify_dataset_not_empty(
    ctx: JobContext, account_ref: kaggle_api.KaggleAccount, dataset_ref: str,
) -> None:
    """Fail fast when the upload linked nothing: a "ready" yet empty dataset
    attaches fine and the kernel runs -- then dies in Cell 4's assert after
    burning GPU quota to discover what ListDatasetFiles shows in one call."""
    files = kaggle_api.dataset_files(account_ref, dataset_ref)
    names = [str(f.get("name") or f.get("ref") or "?") for f in files]
    ctx.log(f"Dataset {dataset_ref} contains {len(files)} file(s): {names[:5]}")
    if not files:
        raise JobFatalError(
            f"Dataset {dataset_ref} của Kaggle rỗng (upload không gắn được file nào). "
            f"Mở kaggle.com kiểm tra dataset rồi chạy lại."
        )


def _drive_creds_for_kernel(conn) -> dict | None:
    """Best-effort Drive credentials for the kernel's optional output sync (see
    Cell 4's Drive API branch): the first connected account that can mint a
    GDRIVE_CREDS payload. Returns None when Drive is not connected. Never raises;
    the active Kaggle handler uses the stricter account-aware helper below."""
    try:
        from app import google_drive
    except ImportError:
        return None
    try:
        accounts = google_drive.list_accounts(conn)
    except Exception as exc:
        logger.warning("Drive account lookup for Kaggle sync failed: %s", exc)
        return None
    for account in accounts:
        try:
            creds = google_drive.kaggle_credentials(conn, account["id"])
        except Exception as exc:
            logger.warning(
                "Drive creds lookup for Kaggle sync failed (account %s): %s",
                account.get("id"), exc,
            )
            continue
        if creds:
            return creds
    return None


def _drive_account_for_kernel(conn) -> tuple[dict, dict] | None:
    """Return the first Drive account and credentials usable by the Kaggle notebook."""
    try:
        from app import google_drive
        for account in google_drive.list_accounts(conn):
            creds = google_drive.kaggle_credentials(conn, account["id"])
            if creds:
                return account, creds
    except Exception as exc:
        logger.warning("Drive account lookup for Kaggle input failed: %s", exc)
    return None


def _stable_batch_id(slug: str, model_id: str, voice_id: str | None,
                     max_chars: int, with_effects: bool) -> str:
    """One Drive-sync identity per (patch set, TTS params): every cycle and every
    job retry of the same batch reuses it, so a new kernel version finds the
    previous run's chunk/output files on Drive and resumes instead of starting
    from scratch (same behaviour as the manual Drive notebook). A different
    voice/model gets a different id so it never resumes from stale audio."""
    key = f"{model_id}|{voice_id or ''}|{max_chars}|{int(with_effects)}"
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:6]
    return f"{slug}-{digest}"


def _automation_policy(payload: dict) -> tuple[dict, str, bool]:
    """(request_policy, mode, active) for the video/YouTube fallback chain.

    Mirrors the "Tạo âm thanh" dialog (/tts/generate) honored by audiobook_tts:
    explicit payload flags override the book's persisted flags; when the route
    sends no flags at all the job stays legacy (only the old publish hook runs).
    Automation is active when video and/or upload is explicitly requested."""
    request_policy: dict = {}
    if payload.get("auto_create_video") is not None:
        request_policy["auto_create_video"] = bool(payload.get("auto_create_video"))
    if payload.get("auto_upload_youtube") is not None:
        request_policy["auto_upload_youtube"] = bool(payload.get("auto_upload_youtube"))
    mode = payload.get("automation_mode") or AUTOMATION_AFTER_ALL
    if mode not in (AUTOMATION_PER_PATCH, AUTOMATION_AFTER_ALL):
        mode = AUTOMATION_AFTER_ALL
    active = bool(request_policy.get("auto_create_video") or request_policy.get("auto_upload_youtube"))
    return request_policy, mode, active


def _automate_patch(ctx: JobContext, patch_id: int, request_policy: dict) -> str:
    """Thumbnail warm + enqueue_patch_video for one freshly-imported patch -- the
    same finish path the local TTS handler uses. Returns the outcome state."""
    thumbnail_inputs = fetch_thumbnail_inputs(ctx.conn, patch_id)
    warm_patch_thumbnail(thumbnail_inputs)
    outcome = enqueue_patch_video(ctx.conn, patch_id, request_policy=request_policy)
    ctx.log(f"patch {patch_id} automation={outcome['state']}")
    return outcome["state"]


def _automate_after_all(
    ctx: JobContext, conn, book_id: int, patch_ids: list[int],
    imported_this_job: list[int], request_policy: dict,
) -> None:
    """Fallback an toàn: cả batch xong mới chuỗi video/upload một lượt.

    Bao gồm patch job này vừa import lẫn patch đã done từ trước nhưng chưa có
    video (lấp chỗ trống khi job phải retry nhiều attempt); bỏ qua patch đã có
    video done để không render trùng."""
    fresh = [pid for pid in imported_this_job if pid in patch_ids]
    gaps = []
    for pid in patch_ids:
        if pid in fresh:
            continue
        patch = repository.get_patch(conn, pid)
        if patch is None or patch.book_id != book_id or patch.status != "done":
            continue
        row = conn.execute(
            "SELECT video_status, video_path FROM patch_pipeline WHERE patch_id=?", (pid,),
        ).fetchone()
        if row is not None and (row["video_status"] == "done"
                                or (row["video_path"] and Path(row["video_path"]).is_file())):
            continue
        gaps.append(pid)
    targets = fresh + gaps
    if not targets:
        return
    ctx.progress(len(patch_ids), len(patch_ids), phase="automating")
    states = [_automate_patch(ctx, pid, request_policy) for pid in targets]
    ctx.log(f"Automation after_all cho {len(targets)} patch (mới: {len(fresh)}, lấp: {len(gaps)}): {states}")


def _poll_drive_results(
    ctx: JobContext,
    conn,
    book_id: int,
    patch_ids: list[int],
    patches: list,
    package_dir: Path,
    drive_account_id: int,
    drive_batch_folder_id: str,
    request_policy: dict,
    automation_mode: str,
    automation_active: bool,
    imported_this_job: list[int],
    kernel_ref: str,
) -> int:
    """Fetch Drive/result and install every newly completed patch.

    This is intentionally safe to call on every Kaggle status poll. Database patch
    status is the de-duplication guard, so a result that remains on Drive is never
    sent through the audio/video/YouTube pipeline twice.
    """
    from app import google_drive

    with ctx.keep_alive():
        service = google_drive.get_drive_service(conn, drive_account_id)
        downloaded = google_drive.download_result_directory(
            service, drive_batch_folder_id, package_dir,
        )
    if downloaded:
        ctx.log(
            f"Drive/result: {len(downloaded)} file(s) available while kernel {kernel_ref} runs"
        )

    imported = 0
    for patch in patches:
        current = repository.get_patch(conn, patch.id)
        if current is None or current.book_id != book_id or current.status == "done":
            continue
        patch_folder = package_dir / "patches" / f"patch_{patch.patch_index:03d}"
        result = patch_import.resolve_batch_result(patch_folder, patch.id)
        if result is None or not result.is_file():
            continue
        audio_path = repository.get_patch_audio_path(book_id, patch.patch_index)
        try:
            patch_import.install_imported_wav(result, audio_path)
        except Exception:
            logger.warning(
                "Kaggle result WAV invalid for patch %s; will check Drive again",
                patch.id, exc_info=True,
            )
            continue
        repository.mark_patch_done(conn, patch.id, str(audio_path))
        if automation_active and automation_mode == AUTOMATION_PER_PATCH:
            _automate_patch(ctx, patch.id, request_policy)
        elif not automation_active:
            on_patch_audio_ready(conn, patch.id)
        imported_this_job.append(patch.id)
        imported += 1
        done = len(patch_ids) - len(_missing_patch_ids(conn, book_id, patch_ids))
        ctx.progress(done, len(patch_ids), phase="importing")
        ctx.log(
            f"Imported patch {patch.id} from Drive/result while kernel {kernel_ref} runs "
            f"({done}/{len(patch_ids)})"
        )
    return imported


def handle(ctx: JobContext) -> dict | None:
    payload = ctx.job.payload
    book_id = int(payload["book_id"])
    patch_ids = [int(p) for p in payload["patch_ids"]]
    model_id = payload.get("model_id") or "voxcpm2"
    voice_id = payload.get("voice_id")
    max_chars = int(payload.get("max_chars") or 0)
    with_effects = bool(payload.get("with_effects"))
    request_policy, automation_mode, automation_active = _automation_policy(payload)

    conn = ctx.conn
    account: dict | None = None
    package_dir: Path | None = None
    drive_batch_folder_id: str | None = None
    total = len(patch_ids)
    imported_this_job: list[int] = []
    # Stable across every push in this job (based on the ORIGINAL full patch_ids
    # plus the TTS params), so a retry -- same job, new kernel version -- maps to
    # the same Drive sync folder and the notebook resumes finished chunks instead
    # of re-synthesizing from scratch (like the manual Drive notebook).
    slug = _kernel_slug(book_id, patch_ids)
    stable_batch_id = _stable_batch_id(slug, model_id, voice_id, max_chars, with_effects)
    drive_account_info = _drive_account_for_kernel(conn)
    if drive_account_info:
        drive_account, drive_creds = drive_account_info
        ctx.log(
            f"Drive sync bật cho kernel này (batch {stable_batch_id}): "
            "input và chunk/output dùng Google Drive API, retry sẽ resume phần đã xong"
        )
    else:
        raise JobFatalError(
            "Kaggle API cần Google Drive API để truyền batch và nhận thư mục result; "
            "hãy kết nối ít nhất một tài khoản Google Drive trước khi chạy."
        )
    ctx.progress(0, total, phase="preparing")
    try:
        # Self-heal cooldowns written by older releases from an inaccurate local
        # wall-clock estimate. Each claimed account is checked against Kaggle below.
        kaggle_accounts.recover_unavailable_accounts(conn)
        while True:
            missing = _missing_patch_ids(conn, book_id, patch_ids)
            done = total - len(missing)
            imported_in_cycle = 0
            if not missing:
                if automation_active and automation_mode == AUTOMATION_AFTER_ALL:
                    _automate_after_all(ctx, conn, book_id, patch_ids, imported_this_job, request_policy)
                ctx.progress(total, total, phase="done")
                return {"imported": len(patch_ids)}

            patches = [p for p in (repository.get_patch(conn, pid) for pid in missing) if p is not None]
            if not patches:
                raise JobFatalError("none of the pending patch_ids exist anymore")

            if account is None:
                account = kaggle_accounts.claim_idle_account(conn, ctx.job.id)
                if account is None:
                    reset = kaggle_accounts.earliest_quota_reset(conn)
                    if reset:
                        message = "no Kaggle GPU quota available in any account this week"
                        retry_at = reset
                    else:
                        message = "no idle Kaggle account available; all accounts are busy or disabled"
                        retry_at = (
                            datetime.now(timezone.utc)
                            + timedelta(minutes=_BUSY_ACCOUNT_RETRY_MINUTES)
                        ).isoformat()
                    raise JobRescheduled(
                        retry_at,
                        message,
                    )

                quota = _authoritative_quota(ctx, account)
                if quota is not None and quota.remaining_seconds <= 0:
                    cooldown_until = quota.refresh_at or (
                        datetime.now(timezone.utc) + timedelta(hours=_FALLBACK_RESCHEDULE_HOURS)
                    ).isoformat()
                    ctx.log(
                        f"Account {account['username']} hết quota theo Kaggle; "
                        f"cooldown tới {cooldown_until}"
                    )
                    kaggle_accounts.release_account(
                        conn, account["id"], cooldown_until=cooldown_until,
                    )
                    account = None
                    continue

            try:
                package_dir, _batch_manifest = drive_export.build_kaggle_export_package(
                    conn, patches, model_id=model_id, voice_id=voice_id,
                    max_chars=max_chars, with_effects=with_effects,
                    gdrive_creds=drive_creds, batch_id=stable_batch_id,
                    drive_folder_name=slug,
                )
            except ValueError as exc:
                raise JobFatalError(str(exc)) from exc

            account_ref = kaggle_api.KaggleAccount(username=account["username"], api_key=account["api_key"])

            # The notebook reads its input and persists outputs through Drive API. The
            # package is uploaded once for this job; retries reuse the same Drive folder.
            if drive_batch_folder_id is None:
                ctx.progress(done, total, phase="uploading")
                ctx.log(
                    f"Uploading batch data to Google Drive account {drive_account['account_email']}"
                )
                drive_batch_folder = drive_export.publish_package_to_drive_api(
                    conn, drive_account["id"], package_dir, slug,
                )
                drive_batch_folder_id = drive_batch_folder["id"]

            ctx.progress(done, total, phase="pushing")
            ctx.log(f"Pushing kernel {account['username']}/{slug} ({len(patches)} patch(es))")
            kernel_ref = ""
            for attach_attempt in range(1, _ATTACH_RETRY_ATTEMPTS + 1):
                kernel_ref = _push_kernel(
                    account_ref, package_dir, account["username"], slug,
                )
                ctx.log(f"Kernel đã push: https://www.kaggle.com/code/{kernel_ref}")
                usage_id = kaggle_accounts.record_usage_start(conn, account["id"], kernel_ref)
                started_at = time.monotonic()

                status = kaggle_api.kernel_status(account_ref, kernel_ref)
                ctx.progress(done, total, phase="running")
                ctx.log(f"Kernel {kernel_ref} status={status.value}")
                polls = 0
                last_status = status
                while status in (KernelStatus.QUEUED, KernelStatus.RUNNING):
                    if ctx.should_cancel():
                        kaggle_api.cancel_kernel(account_ref, kernel_ref)
                        kaggle_accounts.record_usage_finish(conn, usage_id, int(time.monotonic() - started_at))
                        ctx.log("Tác vụ đã bị hủy; đã yêu cầu hủy kernel trên Kaggle")
                        return None
                    time.sleep(settings.kaggle_poll_interval_seconds)
                    ctx.heartbeat()
                    _poll_drive_results(
                        ctx, conn, book_id, patch_ids, patches, package_dir,
                        drive_account["id"], drive_batch_folder_id,
                        request_policy, automation_mode, automation_active,
                        imported_this_job, kernel_ref,
                    )
                    # Đẩy tiến độ xuống DB mỗi vòng poll để bảng Queue thấy phase/heartbeat
                    # thay đổi thay vì đứng yên suốt hàng giờ.
                    ctx.progress(done, total, phase="running")
                    status = kaggle_api.kernel_status(account_ref, kernel_ref)
                    polls += 1
                    if status is not last_status:
                        ctx.log(f"Kernel {kernel_ref} status={status.value} (sau {polls} lượt kiểm tra)")
                        last_status = status
                    else:
                        ctx.log(f"Kernel {kernel_ref} vẫn {status.value} (lượt kiểm tra {polls})")

                kaggle_accounts.record_usage_finish(conn, usage_id, int(time.monotonic() - started_at))
                ctx.log(f"Kernel {kernel_ref} finished with status={status.value}")

                if status == KernelStatus.CANCELLED:
                    raise JobFatalError(f"Kernel {kernel_ref} đã bị hủy trên Kaggle.")
                if status != KernelStatus.ERROR:
                    break
                log_text = _fetch_kernel_log_text(ctx, account_ref, kernel_ref)
                if _MISSING_INPUT_SIGNATURE in log_text and attach_attempt < _ATTACH_RETRY_ATTEMPTS:
                    # Keep the retry for transient Drive/API startup failures. The
                    # notebook will locate the same Drive folder on the next version.
                    ctx.log(
                        f"Kernel {kernel_ref} không đọc được batch từ Google Drive "
                        f"(lần {attach_attempt}/{_ATTACH_RETRY_ATTEMPTS}); "
                        f"đợi {_ATTACH_RETRY_DELAY_SECONDS:g}s rồi push lại.",
                        level=logging.WARNING,
                    )
                    _sleep_with_heartbeat(ctx, _ATTACH_RETRY_DELAY_SECONDS)
                    continue
                tail = (log_text or "<không đọc được kernel log>")[-1500:]
                raise JobFatalError(
                    f"Kernel {kernel_ref} thất bại trên Kaggle. "
                    f"Xem log tại https://www.kaggle.com/code/{kernel_ref} "
                    f"(đuôi log: {tail}). "
                    f"Nguyên nhân hay gặp: Kaggle gán GPU P100 (sm_60) thay vì T4 — "
                    f"PyTorch trong image hiện tại chỉ chạy trên sm_70+."
                )

            imported_in_cycle += _poll_drive_results(
                ctx, conn, book_id, patch_ids, patches, package_dir,
                drive_account["id"], drive_batch_folder_id,
                request_policy, automation_mode, automation_active,
                imported_this_job, kernel_ref,
            )

            shutil.rmtree(package_dir, ignore_errors=True)
            package_dir = None

            if imported_in_cycle == 0:
                raise JobFatalError(
                    f"Kernel {kernel_ref} hoàn tất nhưng không có WAV kết quả hợp lệ; "
                    "không tự chạy lại để tránh tốn thêm quota Kaggle."
                )

            if not _missing_patch_ids(conn, book_id, patch_ids):
                if automation_active and automation_mode == AUTOMATION_AFTER_ALL:
                    _automate_after_all(ctx, conn, book_id, patch_ids, imported_this_job, request_policy)
                ctx.progress(total, total, phase="done")
                return {"imported": len(patch_ids)}

            quota = _authoritative_quota(ctx, account)
            if quota is not None and quota.remaining_seconds <= 0:
                cooldown_until = quota.refresh_at or (
                    datetime.now(timezone.utc) + timedelta(hours=_FALLBACK_RESCHEDULE_HOURS)
                ).isoformat()
                ctx.log(f"Account {account['username']} out of quota; cooling down until {cooldown_until}")
                kaggle_accounts.release_account(conn, account["id"], cooldown_until=cooldown_until)
                account = None
            # else: loop again, pushing a continuation version to the same account/kernel
    finally:
        if package_dir is not None:
            shutil.rmtree(package_dir, ignore_errors=True)
        if account is not None:
            kaggle_accounts.release_account(conn, account["id"])
