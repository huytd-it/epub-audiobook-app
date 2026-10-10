"""Drive-first execution for managed Kaggle CLI packages."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import drive_export, google_drive, kaggle_accounts, kaggle_api, kaggle_packages as packages
from app import patch_import, repository
from app.config import settings
from app.jobqueue.models import JobFatalError, JobRescheduled
from app.kaggle_api import KernelStatus


def reconcile(ctx, package, service, staging, imported, policy, mode, active):
    """Drive listing is authoritative; download only absent local results."""
    from app.jobqueue.handlers.kaggle_tts import _automate_patch, on_patch_audio_ready

    entries = json.loads(package["patches_json"])
    remote = packages.result_inventory(service, package["drive_folder_id"])
    ready = sum(entry["wav"] in remote for entry in entries)
    packages.update(ctx.conn, package["id"], remote_count=ready, checked_at=packages.now(),
                    status="incomplete", error_message=None)
    for entry in entries:
        if ctx.should_cancel():
            raise asyncio.CancelledError()
        patch = repository.get_patch(ctx.conn, entry["patch_id"])
        if patch is None or patch.book_id != package["book_id"]:
            raise JobFatalError(f"Patch {entry['patch_id']} của gói đã bị xóa.")
        wav = entry["wav"]
        if wav not in remote:
            continue
        target = packages.local_path(package, entry)
        sidecar = target.with_suffix(".timeline.json")
        timeline_name = Path(wav).with_suffix(".timeline.json").name
        was_done = patch.status == "done"
        had_audio = packages.has_file(target)
        if not had_audio:
            source = staging / wav
            google_drive.download_file(service, remote[wav]["id"], str(source))
            if timeline_name in remote:
                google_drive.download_file(service, remote[timeline_name]["id"], str(source.with_suffix(".timeline.json")))
            patch_import.install_imported_wav(source, target)
            ctx.log(f"Đã tải file còn thiếu: {wav}")
        elif timeline_name in remote and not packages.has_file(sidecar):
            temporary = staging / timeline_name
            google_drive.download_file(service, remote[timeline_name]["id"], str(temporary))
            # Validate metadata before installing it beside an existing WAV.
            json.loads(temporary.read_text(encoding="utf-8"))
            local_tmp = sidecar.with_name(sidecar.name + ".download")
            try:
                shutil.copyfile(temporary, local_tmp)
                local_tmp.replace(sidecar)
            finally:
                local_tmp.unlink(missing_ok=True)
            ctx.log(f"Đã tải timeline còn thiếu: {timeline_name}")
        repository.mark_patch_done(ctx.conn, patch.id, str(target))
        # Restoring a deleted local file must not trigger a second publication.
        if not was_done:
            if active and mode == "per_patch":
                _automate_patch(ctx, patch.id, policy)
            elif not active:
                on_patch_audio_ready(ctx.conn, patch.id)
            imported.append(patch.id)
    local = sum(packages.has_file(packages.local_path(package, entry)) for entry in entries)
    complete = ready == len(entries) and local == len(entries) and bool(entries)
    packages.update(ctx.conn, package["id"], local_count=local, remote_count=ready,
                    status="done" if complete else "incomplete", checked_at=packages.now())
    ctx.progress(ready, len(entries), phase="done" if complete else "checking_drive")
    return complete


def _claim_account(conn, job_id, pinned_id):
    if pinned_id is None:
        return kaggle_accounts.claim_idle_account(conn, job_id)
    row = conn.execute(
        """UPDATE kaggle_account SET status='busy', in_use_by_job_id=?, updated_at=?
           WHERE id=? AND (status='idle' OR (status='cooldown' AND cooldown_until<=?))
           RETURNING *""", (job_id, packages.now(), pinned_id, packages.now()),
    ).fetchone()
    conn.commit()
    return dict(row) if row else None


def handle(ctx):
    from app.jobqueue.handlers.kaggle_tts import (
        _automation_policy, _automate_after_all, _authoritative_quota, _push_kernel,
    )
    conn = ctx.conn
    package = packages.get(conn, int(ctx.job.payload["package_id"]))
    if package is None:
        raise JobFatalError("Không tìm thấy gói Kaggle")
    payload = json.loads(package["payload_json"])
    policy, mode, active = _automation_policy(payload)
    imported = []
    account = None
    usage_id = None
    started = time.monotonic()
    kernel_ref = None
    check_only = bool(ctx.job.payload.get("check_only"))
    packages.update(conn, package["id"], last_job_id=ctx.job.id, error_message=None)
    try:
        with tempfile.TemporaryDirectory(prefix="kaggle-package-") as tmp:
            staging = Path(tmp)
            ctx.progress(0, len(json.loads(package["patches_json"])), phase="checking_drive")
            with ctx.keep_alive():
                service, package = packages.discover_folder(conn, package)
                if service is None:
                    if ctx.job.payload.get("resume_existing") or check_only:
                        raise JobFatalError(
                            "Không tìm thấy thư mục Drive cũ của gói. Kết nối đúng tài khoản Drive rồi kiểm tra lại."
                        )
                    accounts = google_drive.list_accounts(conn)
                    if not accounts:
                        raise JobFatalError("Cần kết nối Google Drive trước khi chạy Kaggle CLI.")
                    drive_account_id = accounts[0]["id"]
                    credentials = google_drive.kaggle_credentials(conn, drive_account_id)
                    service = google_drive.get_drive_service(conn, drive_account_id)
                    patches = [repository.get_patch(conn, e["patch_id"]) for e in json.loads(package["patches_json"])]
                    if any(p is None for p in patches):
                        raise JobFatalError("Một patch trong gói đã bị xóa")
                    source, _ = drive_export.build_kaggle_export_package(
                        conn, patches, model_id=payload.get("model_id") or "voxcpm2",
                        voice_id=payload.get("voice_id"), max_chars=int(payload.get("max_chars") or 0),
                        with_effects=bool(payload.get("with_effects")), gdrive_creds=credentials,
                        batch_id=package["batch_id"], drive_folder_name=package["batch_id"],
                    )
                    try:
                        root = google_drive.get_or_create_root_folder(service)
                        folder = google_drive.create_folder(service, package["batch_id"], parent_id=root)
                        # Persist before upload, so even a failed first upload can never
                        # silently create another folder on the next attempt.
                        packages.update(conn, package["id"], drive_account_id=drive_account_id,
                                        drive_folder_id=folder["id"])
                        ctx.progress(0, len(patches), phase="uploading")
                        google_drive.upload_directory(service, folder["id"], str(source))
                    finally:
                        shutil.rmtree(source, ignore_errors=True)
                    package = packages.get(conn, package["id"])

                complete = reconcile(ctx, package, service, staging, imported, policy, mode, active)
                if complete or check_only:
                    if complete and active and mode == "after_all":
                        _automate_after_all(ctx, conn, package["book_id"], payload["patch_ids"], imported, policy)
                    return {"package_id": package["id"], "complete": complete}
                ctx.log("Drive còn thiếu kết quả; dùng lại thư mục cũ và chỉ push version Kaggle.")
                manifests = [f for f in google_drive.list_files(service, package["drive_folder_id"])
                             if f.get("name") == "batch_manifest.json"]
                if len(manifests) != 1:
                    raise JobFatalError("Thư mục Drive cũ phải có đúng một batch_manifest.json; không tự ghi đè dữ liệu.")
                manifest_path = staging / "batch_manifest.json"
                google_drive.download_file(service, manifests[0]["id"], str(manifest_path))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if manifest.get("batch_id") != package["batch_id"]:
                    raise JobFatalError("Manifest trên Drive không khớp gói; dừng để tránh chạy nhầm dữ liệu.")
                covered = {e["patch_id"] for e in manifest.get("patches", [])}
                missing_inputs = set(payload["patch_ids"]) - covered
                if missing_inputs:
                    remote = packages.result_inventory(service, package["drive_folder_id"])
                    entries = json.loads(package["patches_json"])
                    if any(e["patch_id"] in missing_inputs and e["wav"] not in remote for e in entries):
                        raise JobFatalError("Drive thiếu manifest đầu vào cho một số patch chưa xong; không tự upload lại gói cũ.")
                credentials = google_drive.kaggle_credentials(conn, package["drive_account_id"])
                if not credentials:
                    raise JobFatalError("Tài khoản Drive của gói chưa có thông tin xác thực cho Kaggle.")
                drive_export.build_kaggle_continuation_notebook(
                    staging, batch_id=package["batch_id"], folder_id=package["drive_folder_id"],
                    credentials=credentials,
                )

            kaggle_accounts.recover_unavailable_accounts(conn)
            pinned_id = package["kaggle_account_id"]
            if pinned_id is not None and kaggle_accounts.get_account(conn, pinned_id) is None:
                raise JobFatalError("Tài khoản Kaggle gốc đã bị xóa; không thể tạo version cho notebook cũ.")
            account = _claim_account(conn, ctx.job.id, pinned_id)
            if account is None:
                raise JobRescheduled((datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                                     "Tài khoản Kaggle đang bận hoặc chưa khả dụng")
            account_ref = kaggle_api.KaggleAccount(username=account["username"], api_key=account["api_key"])
            kernel_ref = package["kernel_ref"]
            status = kaggle_api.kernel_status(account_ref, kernel_ref) if kernel_ref else None
            if status not in (KernelStatus.QUEUED, KernelStatus.RUNNING):
                quota = _authoritative_quota(ctx, account)
                if quota is not None and quota.remaining_seconds <= 0:
                    raise JobRescheduled(quota.refresh_at or
                        (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat(), "Kaggle đã hết quota GPU")
                if ctx.should_cancel():
                    raise asyncio.CancelledError()
                ctx.progress(0, len(payload["patch_ids"]), phase="pushing")
                # Keep the original account and slug on every continuation.
                kernel_ref = _push_kernel(account_ref, staging, account["username"], package["slug"])
                packages.update(conn, package["id"], kaggle_account_id=account["id"], kernel_ref=kernel_ref)
                ctx.log(f"Đã push version: https://www.kaggle.com/code/{kernel_ref}")
                status = kaggle_api.kernel_status(account_ref, kernel_ref)
            else:
                ctx.log(f"Notebook {kernel_ref} vẫn đang chạy; tiếp tục theo dõi version hiện tại.")
            usage_id = kaggle_accounts.record_usage_start(conn, account["id"], kernel_ref)
            started = time.monotonic()
            while True:
                if ctx.should_cancel():
                    raise asyncio.CancelledError()
                with ctx.keep_alive():
                    complete = reconcile(ctx, package, service, staging, imported, policy, mode, active)
                if complete:
                    if active and mode == "after_all":
                        _automate_after_all(ctx, conn, package["book_id"], payload["patch_ids"], imported, policy)
                    return {"package_id": package["id"], "complete": True}
                if status not in (KernelStatus.QUEUED, KernelStatus.RUNNING):
                    raise JobFatalError(
                        f"Kaggle {status.value}; Drive/result vẫn còn thiếu file. Gói được giữ lại để chạy tiếp."
                    )
                current = packages.get(conn, package["id"])["remote_count"] or 0
                ctx.progress(current, len(payload["patch_ids"]), phase="running")
                # Short waits keep cancellation responsive even with long poll intervals.
                deadline = time.monotonic() + settings.kaggle_poll_interval_seconds
                while time.monotonic() < deadline:
                    if ctx.should_cancel():
                        raise asyncio.CancelledError()
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
                    ctx.heartbeat()
                status = kaggle_api.kernel_status(account_ref, kernel_ref)
    except asyncio.CancelledError:
        if account is not None and kernel_ref:
            kaggle_api.cancel_kernel(account_ref, kernel_ref)
        packages.update(conn, package["id"], status="incomplete")
        raise
    except JobRescheduled:
        raise
    except Exception as exc:
        packages.update(conn, package["id"], status="incomplete", error_message=str(exc))
        raise
    finally:
        if usage_id is not None:
            kaggle_accounts.record_usage_finish(conn, usage_id, int(time.monotonic() - started))
        if account is not None:
            kaggle_accounts.release_account(conn, account["id"])
