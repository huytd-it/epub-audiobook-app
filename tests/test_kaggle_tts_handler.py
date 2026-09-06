"""app.jobqueue.handlers.kaggle_tts: push -> poll -> import -> account rotation.

Uses a real (file-backed) DB and the real build_kaggle_export_package/patch_import
pipeline; only the network-touching app.kaggle_api calls are faked, so this exercises
the actual manifest/result-resolution contract rather than a mocked shape of it."""
from __future__ import annotations

import json
import re

import numpy as np
import pytest
import soundfile as sf

from app import db, kaggle_accounts as ka, repository
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
    job_id = store.enqueue(conn, "kaggle_tts", payload=payload)
    job = store.claim(conn, "kaggle_tts", "w")
    return JobContext(job, conn, JobLogger(job.id, "kaggle_tts"), lambda: cancel)


@pytest.fixture(autouse=True)
def _dataset_already_ready(monkeypatch):
    """Most tests stub create_dataset but exercise the real wait-for-ready and
    verify-not-empty steps -- keep both from touching the network by default;
    the wait/verify tests below override."""
    monkeypatch.setattr(kaggle_tts.kaggle_api, "dataset_status", lambda *a, **k: "ready")
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "dataset_files",
        lambda *a, **k: [{"name": "epub-tts-data-x.zip", "totalBytes": 42}],
    )
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_log", lambda *a, **k: "")


def _write_result_for(package_dir, patch_id):
    """Simulate a completed kernel: write a tiny valid WAV at the exact path
    batch_manifest.json names for this patch's result_wav."""
    manifest = json.loads((package_dir / "batch_manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["patches"] if e["patch_id"] == patch_id)
    result_path = package_dir / entry["result_wav"]
    result_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(result_path, np.zeros(1000, dtype=np.float32), 16000)


def test_kernel_title_resolves_to_the_kernel_slug():
    """Kaggle derives a NEW kernel's slug from its title and ignores the slug the push
    asked for when they disagree, so the next push lands on a different kernel and
    fails with 409 on the title. Guard: title == slug, and the slug survives slugify
    unchanged (lowercase, digits, hyphens only)."""
    slug = kaggle_tts._kernel_slug(18, [6752])
    metadata = kaggle_tts._kernel_metadata("user1", slug, "user1/data")
    assert metadata["title"] == slug
    assert metadata["id"] == f"user1/{slug}"
    assert re.fullmatch(r"[a-z0-9][a-z0-9-]*[a-z0-9]", slug)


def test_kernel_slug_is_stable_per_batch_and_stays_short():
    assert kaggle_tts._kernel_slug(18, [1, 2, 3]) == kaggle_tts._kernel_slug(18, [3, 2, 1])
    assert kaggle_tts._kernel_slug(18, [1, 2, 3]) != kaggle_tts._kernel_slug(18, [1, 2])
    assert kaggle_tts._kernel_slug(18, [1]) != kaggle_tts._kernel_slug(19, [1])
    # A big batch must not blow the slug (and therefore the title) up.
    assert len(kaggle_tts._kernel_slug(18, list(range(500)))) <= 50


def test_a_title_collision_fails_the_job_instead_of_burning_its_retries(tmp_path, monkeypatch):
    """409 ALREADY_EXISTS is deterministic -- retrying re-sends the same title and
    uploads another throwaway dataset on the way to the same error."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")

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

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")

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
    metadata = kaggle_tts._kernel_metadata("user1", "some-slug", "user1/data")
    assert metadata["machine_shape"] == "NvidiaTeslaT4"


def test_handle_waits_for_the_dataset_before_pushing(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")
    seen = []
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "dataset_status",
        lambda account, ref: seen.append(ref) or ("ready" if len(seen) > 2 else "pending"),
    )
    pushed = []
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "push_kernel",
        lambda account, *a, **k: pushed.append(1) or f"{account.username}/slug",
    )
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.COMPLETE)

    def fake_output(account, kernel_ref, dest_dir):
        _write_result_for(dest_dir, patch_id)
        return []
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_output", fake_output)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    assert kaggle_tts.handle(ctx) == {"imported": 1}
    assert len(seen) >= 2  # polled at least once before the single push
    assert pushed == [1]


def test_handle_fails_fast_when_the_dataset_is_broken(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "dataset_status", lambda *a, **k: "failed")
    pushed = []
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda *a, **k: pushed.append(1))

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(JobFatalError, match="failed"):
        kaggle_tts.handle(ctx)
    assert pushed == []  # never push a kernel whose input can never attach


def test_handle_fails_fast_when_the_dataset_is_empty(tmp_path, monkeypatch):
    """A "ready" yet empty dataset attaches fine and the kernel runs -- then dies
    in Cell 4 after burning GPU quota. Refuse to push instead (observed live)."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "dataset_files", lambda *a, **k: [])
    pushed = []
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda *a, **k: pushed.append(1))

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(JobFatalError, match="rỗng"):
        kaggle_tts.handle(ctx)
    assert pushed == []  # no GPU session burned on an input-less kernel


def test_handle_completes_when_one_kernel_run_imports_the_patch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda account, *a, **k: f"{account.username}/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.COMPLETE)

    def fake_output(account, kernel_ref, dest_dir):
        _write_result_for(dest_dir, patch_id)
        return []
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_output", fake_output)

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


def test_handle_raises_job_rescheduled_when_no_account_has_quota(tmp_path):
    conn = _conn(tmp_path)
    book_id, patch_id = _seed_book_and_patch(conn)
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.claim_idle_account(conn, job_id=999)  # busy elsewhere -> claim_idle_account finds nothing
    assert ka.get_account(conn, account_id)["status"] == "busy"

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(JobRescheduled):
        kaggle_tts.handle(ctx)


def test_handle_raises_job_fatal_error_when_patches_are_gone(tmp_path):
    conn = _conn(tmp_path)
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

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")
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
    monkeypatch.setattr(settings, "kaggle_weekly_gpu_quota_hours", 0)  # any usage exhausts quota
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    account1 = ka.create_account(conn, "acc1", "user1", "key1")
    account2 = ka.create_account(conn, "acc2", "user2", "key2")
    book_id, patch_a = _seed_book_and_patch(conn)
    patch_b = _add_patch(conn, book_id, 1)

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")
    push_calls = []
    def fake_push(account, package_dir, metadata):
        push_calls.append(account.username)
        return f"{account.username}/slug"
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", fake_push)
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.COMPLETE)

    output_calls = {"n": 0}
    def fake_output(account, kernel_ref, dest_dir):
        output_calls["n"] += 1
        # First (Kaggle session timeout simulation) run only finishes patch_a; the
        # second run (after rotating accounts) finishes patch_b.
        if output_calls["n"] == 1:
            _write_result_for(dest_dir, patch_a)
        else:
            _write_result_for(dest_dir, patch_b)
        return []
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_output", fake_output)

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

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda account, *a, **k: f"{account.username}/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.ERROR)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "omnivoice"})
    with pytest.raises(JobFatalError, match="thất bại"):
        kaggle_tts.handle(ctx)


def test_handle_repushes_the_same_dataset_when_the_kernel_runs_without_input(tmp_path, monkeypatch):
    """SaveKernel can accept the dataset while the session starts before Kaggle can
    mount it (observed live: dataset READY + files listed, Cell 4 still asserted
    "No attached Kaggle input"). The handler must push a new kernel version with
    the SAME (now older) dataset instead of failing the job."""
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    monkeypatch.setattr(kaggle_tts, "_ATTACH_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(kaggle_tts.drive_export, "_TMP_DIR", tmp_path / "export_tmp")
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    book_id, patch_id = _seed_book_and_patch(conn)

    created = []
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "create_dataset",
        lambda account, *a, **k: created.append(1) or f"{account.username}/data",
    )
    pushed_sources = []
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "push_kernel",
        lambda account, package_dir, metadata: pushed_sources.append(metadata["dataset_sources"]) or f"{account.username}/slug",
    )
    statuses = [KernelStatus.ERROR, KernelStatus.COMPLETE]
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: statuses.pop(0))
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "kernel_log",
        lambda *a, **k: "AssertionError: No attached Kaggle input has a batch_manifest.json",
    )

    def fake_output(account, kernel_ref, dest_dir):
        _write_result_for(dest_dir, patch_id)
        return []
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_output", fake_output)

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    assert kaggle_tts.handle(ctx) == {"imported": 1}
    assert created == [1]  # no fresh dataset for the retry
    assert len(pushed_sources) == 2  # pushed again...
    assert pushed_sources[0] == pushed_sources[1]  # ...with the same dataset


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

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")
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

    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda *a, **k: "user1/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.ERROR)
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "kernel_log", lambda *a, **k: "torch cuda error: no kernel image",
    )

    ctx = _ctx(conn, {"book_id": book_id, "patch_ids": [patch_id], "model_id": "zerotts"})
    with pytest.raises(JobFatalError, match="no kernel image"):
        kaggle_tts.handle(ctx)


def _success_stubs(monkeypatch, tmp_path, write_for):
    """Network fakes for a kernel run that COMPLETES; write_for(patch_id, dest_dir)
    materializes that patch's result wav like a real kernel would."""
    monkeypatch.setattr(kaggle_tts.kaggle_api, "create_dataset", lambda account, *a, **k: f"{account.username}/data")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda *a, **k: "user1/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.COMPLETE)
    monkeypatch.setattr(
        kaggle_tts.kaggle_api, "kernel_output",
        lambda account, kernel_ref, dest_dir: (write_for(dest_dir), []),
    )


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
