"""app.jobqueue.handlers.kaggle_tts: push -> poll -> import -> account rotation.

Uses a real (file-backed) DB and the real build_kaggle_export_package/patch_import
pipeline; only the network-touching app.kaggle_api calls are faked, so this exercises
the actual manifest/result-resolution contract rather than a mocked shape of it."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
import soundfile as sf

from app import db, repository
from app import kaggle_accounts as ka
from app.config import settings
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.handlers import kaggle_tts
from app.jobqueue.joblog import JobLogger
from app.jobqueue.models import JobFatalError, JobRescheduled
from app.kaggle_api import KernelStatus


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "app.db"))
    db.init_schema(conn)
    return conn


def _seed_book_and_patch(conn, title="B", voice_clip_path=None):
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, voice_clip_path, created_at, updated_at) "
        "VALUES (?, 'b.epub', 'b.epub', 10, 'ready', ?, ?, ?)", (title, voice_clip_path, now, now),
    )
    book_id = cur.lastrowid
    conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, 0, 'C', 'Hello world.', 12)",
        (book_id,),
    )
    cur = conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
        "VALUES (?, 0, 0, 0, 'pending', ?, ?)", (book_id, now, now),
    )
    conn.commit()
    return book_id, cur.lastrowid


def _add_patch(conn, book_id, patch_index):
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, ?, 'C', 'More text here.', 15)",
        (book_id, patch_index),
    )
    cur = conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
        (book_id, patch_index, patch_index, patch_index, now, now),
    )
    conn.commit()
    return cur.lastrowid


def _ctx(conn, payload, *, cancel=False):
    store.enqueue(conn, "kaggle_tts", payload=payload)
    job = store.claim(conn, "kaggle_tts", "w")
    return JobContext(job, conn, JobLogger(job.id, "kaggle_tts"), lambda: cancel)


@pytest.fixture(autouse=True)
def _kaggle_guards(monkeypatch):
    """Default network guards: kernel log empty + quota available. The Drive
    transport (publish + result download) is stubbed per-test via _drive_stubs."""
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_log", lambda *a, **k: "")
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "gpu_quota",
        lambda *a, **k: kaggle_tts.kaggle_api.GpuQuota(3600, "2026-09-13T00:00:00Z"),
    )


def _write_result_for(package_dir, patch_id):
    """Simulate a completed kernel: write a tiny valid WAV at the exact path
    batch_manifest.json names for this patch's result_wav. Skips silently when
    the patch is not part of this cycle's manifest (continuation cycles only
    rebuild the still-missing patches)."""
    manifest = json.loads((package_dir / "batch_manifest.json").read_text(encoding="utf-8"))
    entry = next((e for e in manifest["patches"] if e["patch_id"] == patch_id), None)
    if entry is None:
        return
    result_path = package_dir / entry["result_wav"]
    result_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(result_path, np.zeros(1000, dtype=np.float32), 16000)


def _seed_drive_account(conn):
    """One connected Drive account with a refresh token, so the handler's
    Drive-required gate passes without touching the network."""
    conn.execute(
        "INSERT INTO drive_oauth_client (name, client_id, client_secret, created_at, updated_at) "
        "VALUES ('c', 'cid', 'cs', '2026-01-01', '2026-01-01')"
    )
    cur = conn.execute(
        "INSERT INTO google_drive_credentials (account_email, access_token, refresh_token, "
        "token_expiry, oauth_client_id, created_at, updated_at) "
        "VALUES ('a@x.com', 'at', 'rt', '2026-01-01', 1, '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    return cur.lastrowid


def _drive_stubs(monkeypatch, conn, write_for):
    """Fake the Drive-only transport: publish returns a stable folder id and every
    Drive/result poll materializes write_for(package_dir) like a running kernel
    would. No zip is created and kernel_output is never called."""
    _seed_drive_account(conn)
    monkeypatch.setattr(
        kaggle_tts.drive_export, "publish_package_to_drive_api",
        lambda c, account_id, package_dir, folder_name: {"id": "drive-folder-1", "link": "https://drive/x"},
    )
    from app import google_drive as _gd
    monkeypatch.setattr(_gd, "get_drive_service", lambda c, account_id: object())
    def _fake_download(service, folder_id, dest_root):
        assert folder_id == "drive-folder-1"
        from pathlib import Path as _P
        write_for(_P(dest_root))
        return [str(_P(dest_root) / "result")]
    monkeypatch.setattr(_gd, "download_result_directory", _fake_download)


def test_kernel_title_resolves_to_the_kernel_slug():
    """Kaggle derives a NEW kernel's slug from its title and ignores the slug the push
    asked for when they disagree, so the next push lands on a different kernel and
    fails with 409 on the title. Guard: title == slug, and the slug survives slugify
    unchanged (lowercase, digits, hyphens only)."""
    slug = kaggle_tts._kernel_slug(18, [6752])
    metadata = kaggle_tts._kernel_metadata("user1", slug)
    assert metadata["title"] == slug
    assert metadata["id"] == f"user1/{slug}"
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", slug)
    # Drive-only transport: no dataset is ever attached to the kernel.
    assert metadata["dataset_sources"] == []


def test_kernel_slug_is_stable_per_batch_and_stays_short():
    assert kaggle_tts._kernel_slug(18, [1, 2, 3]) == kaggle_tts._kernel_slug(18, [3, 2, 1])
    assert kaggle_tts._kernel_slug(18, [1, 2, 3]) != kaggle_tts._kernel_slug(18, [1, 2])
    assert kaggle_tts._kernel_slug(18, [1]) != kaggle_tts._kernel_slug(19, [1])
    # A big batch must not blow the slug (and therefore the title) up.
    assert len(kaggle_tts._kernel_slug(18, list(range(500)))) <= 50


def test_a_title_collision_fails_the_job_instead_of_burning_its_retries(tmp_path, monkeypatch):
    """409 ALREADY_EXISTS is deterministic -- retrying re-sends the same title."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    _drive_stubs(monkeypatch, conn, lambda dest: None)

    def conflict(*a, **k):
        raise RuntimeError(
            'Kaggle API POST .../SaveKernel failed: HTTP 409: b\'{"error":{"code":409,'
            '"message":"The requested title is already in use","status":"ALREADY_EXISTS"}}\''
        )
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", conflict)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(JobFatalError, match="kaggle.com/code"):
        kaggle_tts.handle(ctx)
    assert ka.get_account(conn, account_id)["status"] == "idle"


def test_other_push_failures_stay_retryable(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    _drive_stubs(monkeypatch, conn, lambda dest: None)

    def boom(*a, **k):
        raise RuntimeError("Kaggle API POST .../SaveKernel failed: HTTP 500: b'boom'")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", boom)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(RuntimeError) as excinfo:  # JobFatalError does not subclass RuntimeError
        kaggle_tts.handle(ctx)
    assert not isinstance(excinfo.value, JobFatalError)


def test_kernel_metadata_requests_a_concrete_gpu_type(monkeypatch):
    """enable_gpu alone lets the scheduler hand out a P100 (sm_60), which the
    PyTorch in Kaggle's image cannot execute on -- pin the machine shape."""
    monkeypatch.setattr(settings, "kaggle_machine_shape", "NvidiaTeslaT4")
    metadata = kaggle_tts._kernel_metadata("user1", "some-slug")
    assert metadata["machine_shape"] == "NvidiaTeslaT4"


def test_handle_requires_a_connected_drive_account(tmp_path, monkeypatch):
    """No Drive account -> fatal fast: the Drive-only transport has nowhere to
    put the batch and nowhere to read Drive/result from."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(JobFatalError, match="Google Drive"):
        kaggle_tts.handle(ctx)


def test_handle_uploads_package_to_drive_once_and_reuses_it_for_retries(tmp_path, monkeypatch):
    """The package is published to Drive once per job; attach retries push new
    kernel versions against the same Drive folder (same batch_id) so the
    notebook resumes instead of starting over."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts, "_ATTACH_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    _seed_drive_account(conn)

    published = []
    real_publish = kaggle_tts.drive_export.publish_package_to_drive_api
    def _spy_publish(c, account_id, package_dir, folder_name):
        published.append(folder_name)
        return {"id": "drive-folder-9", "link": "https://drive/x"}
    monkeypatch.setattr(kaggle_tts.drive_export, "publish_package_to_drive_api", _spy_publish)
    from app import google_drive as _gd
    monkeypatch.setattr(_gd, "get_drive_service", lambda c, account_id: object())
    monkeypatch.setattr(
        _gd, "download_result_directory",
        lambda service, folder_id, dest_root: (_write_result_for(__import__("pathlib").Path(dest_root), patch_id), [str(__import__("pathlib").Path(dest_root) / "result")])[1],
    )
    statuses = [KernelStatus.ERROR, KernelStatus.COMPLETE]
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: statuses.pop(0))
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "kernel_log",
        lambda *a, **k: "AssertionError: No attached Kaggle input has a batch_manifest.json",
    )
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda *a, **k: "user1/slug")

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    assert kaggle_tts.handle(ctx) == {"imported": 1}
    assert published == [kaggle_tts._kernel_slug(book_id, [patch_id])]


def test_handle_completes_when_one_kernel_run_imports_the_patch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    # Regression: old local wall-clock accounting could park an account for days
    # even while Kaggle's own dashboard still showed available GPU time.
    ka.release_account(conn, account_id, cooldown_until="2099-01-01T00:00:00+00:00")
    book_id, patch_id = _seed_book_and_patch(conn)
    _drive_stubs(monkeypatch, conn, lambda dest: _write_result_for(dest, patch_id))

    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda account, *a, **k: f"{account.username}/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.COMPLETE)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    result = kaggle_tts.handle(ctx)

    assert result == {"imported": 1}
    assert repository.get_patch(conn, patch_id).status == "done"
    # Layout chuẩn audio/{book}_{episode}.wav, cùng chỗ audiobook_tts/light_tts ghi.
    from pathlib import Path as _Path
    expected = repository.get_patch_audio_path(book_id, 0)
    assert repository.get_patch(conn, patch_id).audio_path == str(expected)
    assert _Path(expected).is_file()
    assert ka.get_account(conn, account_id)["status"] == "idle"
    assert ka.get_account(conn, account_id)["in_use_by_job_id"] is None


def test_handle_does_not_push_again_when_complete_kernel_has_no_result(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    # Drive/result stays empty: kernel COMPLETEs but nothing was ever written there.
    _drive_stubs(monkeypatch, conn, lambda dest: None)
    pushes = []
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "push_kernel",
        lambda account, *a, **k: pushes.append(account.username) or f"{account.username}/slug",
    )
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.COMPLETE,
    )

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(JobFatalError, match="không tự chạy lại"):
        kaggle_tts.handle(ctx)

    assert pushes == ["user1"]


def test_handle_reschedules_briefly_when_an_account_is_legitimately_busy(tmp_path):
    conn = _conn(tmp_path)
    _seed_drive_account(conn)
    book_id, patch_id = _seed_book_and_patch(conn)
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    owner_id = store.enqueue(
        conn, "kaggle_tts",
        payload={"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"},
    )
    store.claim(conn, "kaggle_tts", "other-worker")
    ka.claim_idle_account(conn, job_id=owner_id)  # legitimately busy in another running job
    assert ka.get_account(conn, account_id)["status"] == "busy"

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(JobRescheduled, match="busy or disabled") as excinfo:
        kaggle_tts.handle(ctx)
    retry_at = datetime.fromisoformat(excinfo.value.next_retry_at)
    assert retry_at < datetime.now(timezone.utc) + timedelta(minutes=6)


def test_handle_raises_job_fatal_error_when_patches_are_gone(tmp_path):
    conn = _conn(tmp_path)
    _seed_drive_account(conn)
    book_id, _ = _seed_book_and_patch(conn)
    ka.create_account(conn, "acc1", "user1", "key1")

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [999999], "model_id": "zerotts"})
    with pytest.raises(JobFatalError):
        kaggle_tts.handle(ctx)


def test_handle_returns_none_and_releases_account_when_cancelled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    _drive_stubs(monkeypatch, conn, lambda dest: None)

    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda account, *a, **k: f"{account.username}/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.RUNNING)
    cancelled = []
    monkeypatch.setattr(kaggle_tts.kaggle_api, "cancel_kernel", lambda *a, **k: cancelled.append(1))

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"}, cancel=True)
    result = kaggle_tts.handle(ctx)

    assert result is None
    assert cancelled == [1]
    assert repository.get_patch(conn, patch_id).status != "done"
    assert ka.get_account(conn, account_id)["status"] == "idle"


def test_handle_rotates_to_a_second_account_when_the_first_runs_out_of_quota(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    account1 = ka.create_account(conn, "acc1", "user1", "key1")
    account2 = ka.create_account(conn, "acc2", "user2", "key2")
    book_id, patch_a = _seed_book_and_patch(conn)
    patch_b = _add_patch(conn, book_id, 1)
    _seed_drive_account(conn)

    monkeypatch.setattr(
        kaggle_tts.drive_export, "publish_package_to_drive_api",
        lambda c, account_id, package_dir, folder_name: {"id": "drive-folder-1", "link": "https://drive/x"},
    )
    from app import google_drive as _gd
    monkeypatch.setattr(_gd, "get_drive_service", lambda c, account_id: object())
    push_calls = []
    def fake_push(account, package_dir, metadata):
        push_calls.append(account.username)
        assert metadata["dataset_sources"] == []
        return f"{account.username}/slug"
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", fake_push)
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.COMPLETE)

    def fake_quota(account):
        # First cycle (user1) reports quota only after its Drive/result import;
        # the handler then rotates because user1 is out of quota for cycle 2.
        remaining = 0 if account.username == "user1" and len(push_calls) >= 1 and repository.get_patch(conn, patch_a).status == "done" else 3600
        return kaggle_tts.kaggle_api.GpuQuota(remaining, "2026-09-13T00:00:00Z")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "gpu_quota", fake_quota)

    def fake_download(service, folder_id, dest_root):
        from pathlib import Path as _P
        dest = _P(dest_root)
        # First kernel run finishes patch_a; the continuation run finishes patch_b.
        # Drive/result is cumulative, so keep patch_a present on later polls.
        _write_result_for(dest, patch_a)
        if len(push_calls) >= 2:
            _write_result_for(dest, patch_b)
        return [str(dest / "result")]
    monkeypatch.setattr(_gd, "download_result_directory", fake_download)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_a, patch_b], "model_id": "zerotts"})
    result = kaggle_tts.handle(ctx)

    assert result == {"imported": 2}
    assert push_calls == ["user1", "user2"]
    assert repository.get_patch(conn, patch_a).status == "done"
    assert repository.get_patch(conn, patch_b).status == "done"
    assert ka.get_account(conn, account1)["status"] == "cooldown"
    assert ka.get_account(conn, account2)["status"] == "idle"


def test_handle_raises_fatal_when_kernel_errors(tmp_path, monkeypatch):
    """When the Kaggle kernel finishes with ERROR (e.g. P100 GPU), the handler
    must raise JobFatalError immediately instead of trying to download output.
    Uses omnivoice (a GPU-required model) so the test matches the real P100 case."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    clip = tmp_path / "voice.wav"
    sf.write(clip, np.zeros(8000, dtype=np.float32), 16000)
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn, voice_clip_path=str(clip))
    _drive_stubs(monkeypatch, conn, lambda dest: None)

    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda account, *a, **k: f"{account.username}/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.ERROR)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "omnivoice"})
    with pytest.raises(JobFatalError, match="thất bại"):
        kaggle_tts.handle(ctx)


def test_handle_repushes_the_same_drive_folder_when_the_kernel_misses_input(tmp_path, monkeypatch):
    """The notebook may start before Drive is readable (transient API/startup
    failure, surfaced as the missing-input signature). The handler must push a
    new kernel version against the SAME Drive folder instead of failing the job."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts, "_ATTACH_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    _seed_drive_account(conn)

    published = []
    monkeypatch.setattr(
        kaggle_tts.drive_export, "publish_package_to_drive_api",
        lambda c, account_id, package_dir, folder_name: published.append(folder_name) or {"id": "drive-folder-1", "link": "https://drive/x"},
    )
    from app import google_drive as _gd
    monkeypatch.setattr(_gd, "get_drive_service", lambda c, account_id: object())
    monkeypatch.setattr(
        _gd, "download_result_directory",
        lambda service, folder_id, dest_root: (_write_result_for(__import__("pathlib").Path(dest_root), patch_id), [str(__import__("pathlib").Path(dest_root) / "result")])[1],
    )
    pushed = []
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "push_kernel",
        lambda account, package_dir, metadata: pushed.append(1) or f"{account.username}/slug",
    )
    statuses = [KernelStatus.ERROR, KernelStatus.COMPLETE]
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: statuses.pop(0))
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "kernel_log",
        lambda *a, **k: "AssertionError: No attached Kaggle input has a batch_manifest.json",
    )

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    assert kaggle_tts.handle(ctx) == {"imported": 1}
    assert published == [kaggle_tts._kernel_slug(book_id, [patch_id])]  # uploaded once...
    assert pushed == [1, 1]  # ...pushed again against the same Drive folder


def test_handle_gives_up_after_repeated_runs_without_input(tmp_path, monkeypatch):
    """The attach retry is bounded: a kernel that never sees its input fails the
    job (with the real log tail, not the P100 guess) instead of looping forever."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts, "_ATTACH_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(kaggle_tts, "_ATTACH_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    _drive_stubs(monkeypatch, conn, lambda dest: None)

    pushed = []
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda *a, **k: pushed.append(1) or "user1/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.ERROR)
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "kernel_log",
        lambda *a, **k: "No attached Kaggle input has a batch_manifest.json with batch_id=X",
    )

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(JobFatalError, match="batch_id=X"):
        kaggle_tts.handle(ctx)
    assert pushed == [1, 1]


def test_handle_fatal_kernel_error_includes_the_real_log_tail(tmp_path, monkeypatch):
    """A genuinely failed kernel (e.g. P100 GPU) must surface its actual log tail
    instead of only the P100 guess, so the next debug step is obvious."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    _drive_stubs(monkeypatch, conn, lambda dest: None)

    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda *a, **k: "user1/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.ERROR)
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "kernel_log", lambda *a, **k: "torch cuda error: no kernel image",
    )

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(JobFatalError, match="no kernel image"):
        kaggle_tts.handle(ctx)


def _success_stubs(monkeypatch, conn_or_tmp, write_for, tmp_path=None):
    """Network fakes for a kernel run that COMPLETES; write_for(dest_dir)
    materializes that patch's result wav into Drive/result like a running
    kernel would. No zip is created and kernel_output is never called."""
    import inspect as _inspect
    if tmp_path is None:
        # Called as _success_stubs(monkeypatch, tmp_path, write_for).
        tmp_path = conn_or_tmp
        conn = None
        # Find the caller's conn (tests always name it `conn`).
        frame = _inspect.currentframe().f_back
        conn = frame.f_locals.get("conn")
    else:
        conn = conn_or_tmp
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda *a, **k: "user1/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.COMPLETE)
    monkeypatch.setattr(
        kaggle_tts.drive_export, "publish_package_to_drive_api",
        lambda c, account_id, package_dir, folder_name: {"id": "drive-folder-1", "link": "https://drive/x"},
    )
    from app import google_drive as _gd
    if conn is not None and not _gd.list_accounts(conn):
        _seed_drive_account(conn)
    monkeypatch.setattr(_gd, "get_drive_service", lambda c, account_id: object())
    def _fake_download(service, folder_id, dest_root):
        from pathlib import Path as _P
        write_for(_P(dest_root))
        return [str(_P(dest_root) / "result")]
    monkeypatch.setattr(_gd, "download_result_directory", _fake_download)


def _automation_spies(monkeypatch):
    """Stub the video-chain side effects; record call order in events."""
    events = []
    monkeypatch.setattr(kaggle_tts, "fetch_thumbnail_inputs", lambda conn, pid: events.append(("thumb", pid)) or [])
    monkeypatch.setattr(kaggle_tts, "warm_patch_thumbnail", lambda inputs: events.append(("warm",)))
    monkeypatch.setattr(
        kaggle_tts, "enqueue_patch_video",
        lambda conn, pid, request_policy=None: events.append(("video", pid, dict(request_policy or {}))) or {"state": "queued"},
    )
    monkeypatch.setattr(
        kaggle_tts, "on_patch_audio_ready", lambda conn, pid: events.append(("legacy", pid)),
    )
    return events


def test_per_patch_automation_chains_video_right_after_each_audio(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    _success_stubs(monkeypatch, tmp_path, lambda dest: _write_result_for(dest, patch_id))
    events = _automation_spies(monkeypatch)

    ctx = _ctx(conn, {
        "book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts",
        "auto_create_video": True, "auto_upload_youtube": False,
        "automation_mode": "per_patch",
    })
    assert kaggle_tts.handle(ctx) == {"imported": 1}
    assert ("video", patch_id, {"auto_create_video": True, "auto_upload_youtube": False}) in events
    assert not [e for e in events if e[0] == "legacy"]


def test_after_all_automation_waits_for_the_whole_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_a = _seed_book_and_patch(conn)
    patch_b = _add_patch(conn, book_id, 1)

    def write_both(dest_dir):
        _write_result_for(dest_dir, patch_a)
        _write_result_for(dest_dir, patch_b)
    _success_stubs(monkeypatch, tmp_path, write_both)
    events = _automation_spies(monkeypatch)
    real_mark = repository.mark_patch_done
    monkeypatch.setattr(
        repository, "mark_patch_done",
        lambda conn, pid, path: events.append(("mark", pid)) or real_mark(conn, pid, path),
    )

    ctx = _ctx(conn, {
        "book_id": book_id, "patch_ids": [patch_a, patch_b], "model_id": "zerotts",
        "auto_create_video": True, "automation_mode": "after_all",
    })
    assert kaggle_tts.handle(ctx) == {"imported": 2}
    kinds = [e[0] for e in events]
    # Mọi mark done xong trước mọi enqueue video (an toàn = cả batch xong mới chuỗi).
    assert kinds.index("video") > kinds.index("mark")
    assert kinds.count("mark") == 2 and kinds.count("video") == 2
    assert not [e for e in events if e[0] == "legacy"]


def test_after_all_backfills_a_previously_done_patch_without_video(tmp_path, monkeypatch):
    """Job retry nhiều attempt: patch done từ attempt trước nhưng chưa có video
    được lấp vào đợt automation cuối (patch đã có video thì bỏ qua)."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_a = _seed_book_and_patch(conn)
    patch_b = _add_patch(conn, book_id, 1)
    repository.mark_patch_done(conn, patch_a, "/tmp/legacy.wav")  # done từ trước, chưa automation
    _success_stubs(monkeypatch, tmp_path, lambda dest: _write_result_for(dest, patch_b))
    events = _automation_spies(monkeypatch)

    ctx = _ctx(conn, {
        "book_id": book_id, "patch_ids": [patch_a, patch_b], "model_id": "zerotts",
        "auto_create_video": True, "automation_mode": "after_all",
    })
    assert kaggle_tts.handle(ctx) == {"imported": 2}
    automated = [e[1] for e in events if e[0] == "video"]
    assert sorted(automated) == sorted([patch_a, patch_b])


def test_legacy_job_without_flags_keeps_the_old_publish_hook(tmp_path, monkeypatch):
    """Payload không có flags automation: không gọi dây chuyền video mới, giữ
    nguyên on_patch_audio_ready như trước."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    _success_stubs(monkeypatch, tmp_path, lambda dest: _write_result_for(dest, patch_id))
    events = _automation_spies(monkeypatch)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    assert kaggle_tts.handle(ctx) == {"imported": 1}
    assert ("legacy", patch_id) in events
    assert not [e for e in events if e[0] == "video"]


def test_stable_batch_id_survives_retries_but_isolates_voices():
    """Cùng patch set + cùng TTS params -> cùng batch_id qua mọi retry (Drive sync
    folder chung, resume được); khác voice/model -> khác id (không resume nhầm)."""
    slug = kaggle_tts._kernel_slug(7, [3, 1, 2])
    assert kaggle_tts._stable_batch_id(slug, "zerotts", None, 0, False) == \
        kaggle_tts._stable_batch_id(slug, "zerotts", None, 0, False)
    assert kaggle_tts._stable_batch_id(slug, "zerotts", "voice-a", 0, False) != \
        kaggle_tts._stable_batch_id(slug, "zerotts", "voice-b", 0, False)
    assert kaggle_tts._stable_batch_id(slug, "zerotts", None, 0, False) != \
        kaggle_tts._stable_batch_id(slug, "voxcpm2", None, 0, False)
    assert kaggle_tts._stable_batch_id(slug, "zerotts", None, 0, False).startswith(slug)


def test_drive_creds_for_kernel_is_none_without_drive(tmp_path):
    """Chưa kết nối Drive -> None (kernel offline), không raise."""
    conn = _conn(tmp_path)
    assert kaggle_tts._drive_creds_for_kernel(conn) is None


def test_drive_creds_for_kernel_returns_first_usable_account(tmp_path, monkeypatch):
    """Có Drive account với refresh token -> trả payload kaggle_credentials."""
    from app import google_drive

    conn = _conn(tmp_path)
    conn.execute(
        "INSERT INTO drive_oauth_client (name, client_id, client_secret, created_at, updated_at) "
        "VALUES ('c', 'cid', 'cs', '2026-01-01', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO google_drive_credentials (account_email, access_token, refresh_token, "
        "token_expiry, oauth_client_id, created_at, updated_at) "
        "VALUES ('a@x.com', 'at', 'rt', '2026-01-01', 1, '2026-01-01', '2026-01-01')"
    )
    conn.commit()
    creds = kaggle_tts._drive_creds_for_kernel(conn)
    assert creds is not None
    assert creds["refresh_token"] == "rt"
    assert google_drive.list_accounts(conn)


def test_handle_passes_stable_batch_id_and_drive_creds_to_package(tmp_path, monkeypatch):
    """handle() build package với cùng batch_id ổn định + creds mỗi cycle, để
    retry resume qua Drive thay vì làm lại từ đầu."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)
    _success_stubs(monkeypatch, tmp_path, lambda dest: _write_result_for(dest, patch_id))

    seen = {}
    real_build = kaggle_tts.drive_export.build_kaggle_export_package

    def _spy(c, patches, **kwargs):
        seen.setdefault("batch_ids", []).append(kwargs.get("batch_id"))
        seen.setdefault("creds", []).append(kwargs.get("gdrive_creds"))
        seen["folder"] = kwargs.get("drive_folder_name")
        return real_build(c, patches, **kwargs)

    monkeypatch.setattr(kaggle_tts.drive_export, "build_kaggle_export_package", _spy)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    assert kaggle_tts.handle(ctx) == {"imported": 1}
    assert seen["batch_ids"] and all(b == seen["batch_ids"][0] for b in seen["batch_ids"])
    assert all(c is not None for c in seen["creds"])
    assert seen["folder"] == kaggle_tts._kernel_slug(book_id, [patch_id])
