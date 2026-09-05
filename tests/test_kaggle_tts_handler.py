"""app.jobqueue.handlers.kaggle_tts: push -> poll -> import -> account rotation.

Uses a real (file-backed) DB and the real build_kaggle_export_package/patch_import
pipeline; only the network-touching app.kaggle_api calls are faked, so this exercises
the actual manifest/result-resolution contract rather than a mocked shape of it."""
from __future__ import annotations

import json

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


def _seed_book_and_patch(conn, title="B"):
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES (?, 'b.epub', 'b.epub', 10, 'ready', ?, ?)", (title, now, now),
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


def _write_result_for(package_dir, patch_id):
    """Simulate a completed kernel: write a tiny valid WAV at the exact path
    batch_manifest.json names for this patch's result_wav."""
    manifest = json.loads((package_dir / "batch_manifest.json").read_text(encoding="utf-8"))
    entry = next(e for e in manifest["patches"] if e["patch_id"] == patch_id)
    result_path = package_dir / entry["result_wav"]
    result_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(result_path, np.zeros(1000, dtype=np.float32), 16000)


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
