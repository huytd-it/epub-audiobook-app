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
# How long to wait for a freshly-created dataset to finish processing before
# pushing the kernel that attaches it. Pushing early does NOT fail -- Kaggle
# silently drops the not-yet-ready source and the kernel runs without input.
_DATASET_READY_TIMEOUT_SECONDS = 600


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


def _kernel_metadata(username: str, slug: str, dataset_ref: str) -> dict:
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
        "dataset_sources": [dataset_ref],
    }


def _push_kernel(
    account_ref: kaggle_api.KaggleAccount, package_dir: Path, username: str, slug: str,
    dataset_ref: str,
) -> str:
    """Push, turning Kaggle's title collision into a fatal error rather than letting
    the job spend its remaining attempts on it: the title is the slug, so every retry
    sends the identical title and earns the identical 409 -- after uploading another
    throwaway dataset first."""
    try:
        return kaggle_api.push_kernel(
            account_ref, package_dir, _kernel_metadata(username, slug, dataset_ref),
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
    except Exception as exc:
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
    total = len(patch_ids)
    ctx.progress(0, total, phase="preparing")
    try:
        while True:
            missing = _missing_patch_ids(conn, book_id, patch_ids)
            done = total - len(missing)
            if not missing:
                ctx.progress(total, total, phase="done")
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
            ctx.progress(done, total, phase="uploading")
            ctx.log(f"Uploading batch data as dataset {account['username']}/{dataset_slug}")
            dataset_ref = kaggle_api.create_dataset(
                account_ref, package_dir, dataset_slug, f"EPUB TTS data book {book_id}",
            )
            _wait_dataset_ready(ctx, account_ref, dataset_ref)
            _verify_dataset_not_empty(ctx, account_ref, dataset_ref)

            ctx.progress(done, total, phase="pushing")
            ctx.log(f"Pushing kernel {account['username']}/{slug} ({len(patches)} patch(es))")
            kernel_ref = ""
            for attach_attempt in range(1, _ATTACH_RETRY_ATTEMPTS + 1):
                kernel_ref = _push_kernel(
                    account_ref, package_dir, account["username"], slug, dataset_ref,
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
                    # SaveKernel đã chấp nhận dataset nhưng phiên chạy bắt đầu khi
                    # Kaggle chưa mount được input (quan sát live 2026-09-06: dataset
                    # READY + ListDatasetFiles có file, kernel vẫn chạy tay không).
                    # Push lại version mới với cùng dataset đã già đi, không tốn
                    # dataset mới cũng không cần retry cả job.
                    ctx.log(
                        f"Kernel {kernel_ref} chạy không thấy dataset {dataset_ref} "
                        f"(lần {attach_attempt}/{_ATTACH_RETRY_ATTEMPTS}); "
                        f"đợi {_ATTACH_RETRY_DELAY_SECONDS:g}s rồi push lại cùng dataset.",
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

            # Tải output có thể chậm (hàng chục file); giữ nhịp tim suốt bước này để
            # reaper không tưởng job chết mà trả về pending giữa chừng.
            with ctx.keep_alive():
                kaggle_api.kernel_output(account_ref, kernel_ref, package_dir)

                ctx.progress(done, total, phase="importing")
            for patch in patches:
                patch_folder = package_dir / "patches" / f"patch_{patch.patch_index:03d}"
                result = patch_import.resolve_batch_result(patch_folder, patch.id)
                if result is None or not result.is_file():
                    continue
                # Layout chuẩn: audio/{book_id}_{episode}.wav (episode = patch_index+1,
                # đệm 3 số), cùng chỗ với audiobook_tts/light_tts ghi qua
                # repository.get_patch_audio_path — không dùng legacy patches/{patch_id}.wav.
                audio_path = repository.get_patch_audio_path(book_id, patch.patch_index)
                audio_path.parent.mkdir(parents=True, exist_ok=True)
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
                done += 1
                ctx.progress(done, total, phase="importing")
                ctx.log(f"Imported patch {patch.id} from kernel {kernel_ref} ({done}/{total})")

            shutil.rmtree(package_dir, ignore_errors=True)
            package_dir = None

            if not _missing_patch_ids(conn, book_id, patch_ids):
                ctx.progress(total, total, phase="done")
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
