"""Managed CLI resume: real SQLite/files, mocked remote services only."""
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from app import db, drive_export, google_drive, kaggle_accounts, kaggle_api, kaggle_packages as packages, repository
from app.config import settings
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.handlers import kaggle_package, kaggle_tts
from app.jobqueue.joblog import JobLogger
from app.jobqueue.models import JobFatalError


def forbidden(*args, **kwargs):
    raise AssertionError("Resume must not upload inputs, create folders, or use Kaggle output")


@pytest.fixture
def batch(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "kaggle_poll_interval_seconds", 0)
    conn = db.connect(str(tmp_path / "app.db"))
    db.init_schema(conn)
    stamp = packages.now()
    bid = conn.execute(
        "INSERT INTO book(title,original_filename,epub_path,status,created_at,updated_at) VALUES ('Ebook A','a.epub','a.epub','ready',?,?)",
        (stamp, stamp),
    ).lastrowid
    ids = []
    for index in range(2):
        ids.append(conn.execute(
            "INSERT INTO patch(book_id,patch_index,chapter_start,chapter_end,status,created_at,updated_at) VALUES (?,?,0,0,'pending',?,?)",
            (bid, index, stamp, stamp),
        ).lastrowid)
    conn.commit()
    payload = {"book_id": bid, "patch_ids": ids, "model_id": "zerotts"}
    jid = store.enqueue(conn, "kaggle_tts", payload=payload, book_id=bid, dedupe_key=f"kaggle_tts:book={bid}")
    conn.execute("UPDATE job SET status='failed' WHERE id=?", (jid,))
    conn.commit()
    package = packages.register(conn, store.get(conn, jid))
    aid = kaggle_accounts.create_account(conn, "Kaggle", "original-user", "secret")
    packages.update(conn, package["id"], drive_account_id=42, drive_folder_id="original-folder",
                    kaggle_account_id=aid, kernel_ref=f"original-user/{package['slug']}")
    package = packages.get(conn, package["id"])
    remote = {}
    downloads = []
    service = SimpleNamespace(files=lambda: SimpleNamespace(get=lambda **kw: SimpleNamespace(
        execute=lambda: {"id": "original-folder", "mimeType": "application/vnd.google-apps.folder"})))
    monkeypatch.setattr(google_drive, "get_drive_service", lambda c, account_id: service)
    monkeypatch.setattr(google_drive, "list_result_files", lambda s, fid: list(remote.values()))
    monkeypatch.setattr(google_drive, "list_files", lambda s, fid: [{"id": "manifest", "name": "batch_manifest.json"}])
    monkeypatch.setattr(google_drive, "kaggle_credentials", lambda *a: {"refresh_token": "x", "client_id": "y", "client_secret": "z"})
    def download(svc, file_id, target):
        downloads.append(file_id)
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if file_id == "manifest":
            target.write_text(json.dumps({"batch_id": package["batch_id"], "patches": [{"patch_id": pid} for pid in ids]}))
        elif target.suffix == ".json":
            target.write_text(json.dumps({"version": 1, "chunks": []}))
        else:
            sf.write(target, np.zeros(100, dtype=np.float32), 16000)
    monkeypatch.setattr(google_drive, "download_file", download)
    for name in ["upload_directory", "create_folder", "upload_file", "get_or_create_root_folder"]:
        monkeypatch.setattr(google_drive, name, forbidden)
    monkeypatch.setattr(drive_export, "publish_package_to_drive_api", forbidden)
    monkeypatch.setattr(drive_export, "build_kaggle_export_package", forbidden)
    monkeypatch.setattr(kaggle_api, "kernel_output", forbidden)
    monkeypatch.setattr(kaggle_api, "push_kernel", forbidden)
    monkeypatch.setattr(kaggle_tts, "on_patch_audio_ready", lambda *a: None)
    monkeypatch.setattr(kaggle_api, "gpu_quota", lambda *a: kaggle_api.GpuQuota(3600, None))
    def add(index, timeline=False):
        name = f"{bid}_{index + 1:03d}" + (".timeline.json" if timeline else ".wav")
        remote[name] = {"id": name, "name": name, "size": "244", "modifiedTime": stamp}
    def run(check=False):
        job_id = packages.enqueue_action(conn, package["id"], check_only=check)
        job = store.claim(conn, "kaggle_tts", "test-worker")
        assert job.id == job_id
        ctx = JobContext(job, conn, JobLogger(job.id, "kaggle_tts"), lambda: False)
        return kaggle_tts.handle(ctx)
    yield SimpleNamespace(conn=conn, package=package, ids=ids, bid=bid, add=add, run=run,
                          remote=remote, downloads=downloads, service=service)
    conn.close()


def test_complete_drive_downloads_only_absent_local_files_and_never_pushes(batch):
    batch.add(0); batch.add(1)
    local = repository.get_patch_audio_path(batch.bid, 0)
    local.parent.mkdir(parents=True, exist_ok=True)
    sf.write(local, np.zeros(100), 16000)
    original = local.read_bytes()
    assert batch.run()["complete"] is True
    assert batch.downloads == [f"{batch.bid}_002.wav"]
    assert local.read_bytes() == original
    assert packages.get(batch.conn, batch.package["id"])["status"] == "done"


def test_done_database_flag_does_not_hide_a_missing_local_file(batch):
    batch.add(0); batch.add(1)
    repository.mark_patch_done(batch.conn, batch.ids[0], "/no/such/file.wav")
    assert batch.run()["complete"] is True
    assert len(batch.downloads) == 2
    assert repository.get_patch_audio_path(batch.bid, 0).is_file()


def test_missing_timeline_is_downloaded_without_redownloading_audio(batch):
    batch.add(0); batch.add(1); batch.add(0, timeline=True)
    for index in range(2):
        local = repository.get_patch_audio_path(batch.bid, index)
        local.parent.mkdir(parents=True, exist_ok=True)
        sf.write(local, np.zeros(100), 16000)
    assert batch.run()["complete"] is True
    assert batch.downloads == [f"{batch.bid}_001.timeline.json"]


def test_check_only_keeps_partial_package_incomplete_without_kaggle(batch):
    batch.add(0)
    assert batch.run(check=True)["complete"] is False
    package = packages.get(batch.conn, batch.package["id"])
    assert (package["remote_count"], package["local_count"], package["status"]) == (1, 1, "incomplete")


def test_resume_pushes_same_kernel_with_pinned_drive_folder_and_no_input_upload(batch, monkeypatch):
    batch.add(0)
    pushed = []
    def push(account, directory, metadata):
        notebook = json.loads((directory / "colab_kaggle_batch_tts_template.ipynb").read_text(encoding="utf-8"))
        cell4 = "".join(notebook["cells"][4]["source"])
        assert "_batch_folder_id = 'original-folder'" in cell4
        assert "    # Locate the batch folder:" not in cell4
        assert metadata["id"] == batch.package["kernel_ref"]
        assert account.username == "original-user"
        pushed.append(metadata["id"])
        batch.add(1)
        return metadata["id"]
    monkeypatch.setattr(kaggle_api, "push_kernel", push)
    monkeypatch.setattr(kaggle_api, "kernel_status", lambda *a: kaggle_api.KernelStatus.COMPLETE)
    assert batch.run()["complete"] is True
    assert pushed == [batch.package["kernel_ref"]]
    assert batch.downloads.count(f"{batch.bid}_001.wav") == 1


def test_active_kernel_is_rejoined_without_pushing_another_version(batch, monkeypatch):
    checks = []
    def status(*args):
        checks.append(True)
        batch.add(0); batch.add(1)
        return kaggle_api.KernelStatus.RUNNING
    monkeypatch.setattr(kaggle_api, "kernel_status", status)
    assert batch.run()["complete"] is True
    assert len(checks) == 1


def test_drive_error_aborts_before_any_kaggle_action(batch, monkeypatch):
    def fail(*a):
        raise RuntimeError("Drive unavailable")
    monkeypatch.setattr(google_drive, "list_result_files", fail)
    with pytest.raises(RuntimeError, match="Drive unavailable"):
        batch.run()
    assert packages.get(batch.conn, batch.package["id"])["status"] != "done"


def test_missing_original_folder_never_creates_replacement(batch, monkeypatch):
    packages.update(batch.conn, batch.package["id"], drive_folder_id=None, drive_account_id=None)
    monkeypatch.setattr(google_drive, "list_accounts", lambda c: [])
    with pytest.raises(JobFatalError, match="thư mục Drive cũ"):
        batch.run()


def test_empty_remote_file_does_not_count_as_completed(batch):
    batch.add(0); batch.add(1)
    batch.remote[f"{batch.bid}_002.wav"]["size"] = "0"
    assert batch.run(check=True)["complete"] is False
    assert packages.get(batch.conn, batch.package["id"])["remote_count"] == 1


def test_package_survives_deleting_queue_history(batch):
    batch.conn.execute("DELETE FROM job")
    batch.conn.commit()
    package = packages.get(batch.conn, batch.package["id"])
    assert package["last_job_id"] is None
    assert package["drive_folder_id"] == "original-folder"


def test_filters_search_sort_and_pagination_include_all_packages(batch):
    for model in ["model-b", "model-c"]:
        packages.register(batch.conn, SimpleNamespace(id=None, created_at=packages.now(), payload={
            "book_id": batch.bid, "patch_ids": batch.ids, "model_id": model,
        }))
    result = packages.list_packages(batch.conn, search="ebook a", book_id=batch.bid,
                                    per_page=1, page=2, sort="model_id", direction="asc")
    assert result["total"] == 3
    assert result["total_pages"] == 3
    assert result["items"][0]["model_id"] == "model-c"
    assert "payload_json" not in result["items"][0]
    result = packages.list_packages(batch.conn, model="model-b", status="incomplete")
    assert result["total"] == 1
    assert packages.list_packages(batch.conn, search="no match")["total"] == 0


def test_duplicate_resume_requests_are_rejected(batch):
    packages.enqueue_action(batch.conn, batch.package["id"], check_only=False)
    with pytest.raises(ValueError, match="đang được xử lý"):
        packages.enqueue_action(batch.conn, batch.package["id"], check_only=False)


def test_continuation_notebook_uses_drive_mode_and_valid_python(tmp_path):
    drive_export.build_kaggle_continuation_notebook(tmp_path, batch_id="stable-id", folder_id="old-folder",
        credentials={"client_id": "client", "client_secret": "secret", "refresh_token": "refresh"})
    nb = json.loads((tmp_path / "colab_kaggle_batch_tts_template.ipynb").read_text(encoding="utf-8"))
    assert 'MODE = "kaggle_drive"' in "".join(nb["cells"][1]["source"])
    assert 'IS_KAGGLE = True' in "".join(nb["cells"][1]["source"])
    source = "".join(line if not line.lstrip().startswith("!") else
                     line[:len(line) - len(line.lstrip())] + "pass\n"
                     for line in nb["cells"][4]["source"])
    compile(source, "notebook_cell4", "exec")
    assert "'stable-id', 'Drive batch identity mismatch'" in "".join(nb["cells"][4]["source"])
