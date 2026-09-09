"""app.kaggle_api: client HTTP thuần cho Kaggle API, `request` được inject để test
không chạm mạng thật.

Wire shapes match what Kaggle's own official SDK source
(github.com/Kaggle/kaggle-sdk-python) actually sends -- see the module docstring in
app/kaggle_api.py for the specific findings and remaining unverified gaps."""
from __future__ import annotations

import base64
import json

import pytest

from app.kaggle_api import (
    KaggleAccount,
    KernelStatus,
    cancel_kernel,
    create_dataset,
    dataset_files,
    dataset_status,
    gpu_quota,
    kernel_log,
    kernel_output,
    kernel_status,
    normalize_kernel_ref,
    push_kernel,
)


class FakeRequest:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, *, method, headers, body=None):
        self.calls.append((url, method, headers, body))
        return self._responses.pop(0)


ACCOUNT = KaggleAccount(username="user1", api_key="secret-key")


def test_gpu_quota_uses_kaggles_authoritative_allowance_and_refresh_time():
    fake = FakeRequest([{"status": 200, "body": json.dumps({
        "quotaRefreshTime": "2026-09-13T00:00:00Z",
        "gpuQuota": {
            "timeUsed": "3600s",
            "timeReserved": {"seconds": "1800"},
            "totalTimeAllowed": "108000s",
        },
    })}])

    quota = gpu_quota(ACCOUNT, request=fake)

    assert quota.remaining_seconds == 102600
    assert quota.refresh_at == "2026-09-13T00:00:00Z"
    assert fake.calls[0][0].endswith(
        "/kernels.KernelsApiService/GetAcceleratorQuotaStatistics"
    )

NOTEBOOK_JSON = json.dumps({
    "cells": [
        {"cell_type": "code", "source": ["import os\n", "print(os)\n"], "outputs": ["stale"]},
        {"cell_type": "markdown", "source": ["# hi\n"]},
    ],
    "nbformat": 4,
})


def _write_notebook(tmp_path, name="nb.ipynb"):
    (tmp_path / name).write_text(NOTEBOOK_JSON, encoding="utf-8")
    return name


def test_auth_header_is_http_basic(tmp_path):
    _write_notebook(tmp_path)
    fake = FakeRequest([{"status": 200, "body": json.dumps({"ref": "user1/x"})}])
    push_kernel(ACCOUNT, tmp_path, {"id": "user1/x", "code_file": "nb.ipynb"}, request=fake)
    _, _, headers, _ = fake.calls[0]
    assert headers["Authorization"].startswith("Basic ")
    decoded = base64.b64decode(headers["Authorization"].removeprefix("Basic ")).decode()
    assert decoded == "user1:secret-key"


def test_push_kernel_posts_to_the_save_kernel_rpc_path(tmp_path):
    _write_notebook(tmp_path)
    fake = FakeRequest([{"status": 200, "body": json.dumps({"ref": "user1/epub-tts-batch-abc"})}])
    ref = push_kernel(
        ACCOUNT, tmp_path, {"id": "user1/epub-tts-batch-abc", "code_file": "nb.ipynb"}, request=fake,
    )
    assert ref == "user1/epub-tts-batch-abc"
    url, method, _, _ = fake.calls[0]
    assert url == "https://api.kaggle.com/v1/kernels.KernelsApiService/SaveKernel"
    assert method == "POST"


@pytest.mark.parametrize("raw", [
    "user1/epub-tts-batch-abc",
    "/code/user1/epub-tts-batch-abc",
    "https://www.kaggle.com/code/user1/epub-tts-batch-abc",
    "https://www.kaggle.com/code/user1/epub-tts-batch-abc/versions/3",
    "/code/user1/epub-tts-batch-abc?scriptVersionId=7",
])
def test_normalize_kernel_ref_reduces_any_kernel_url_to_username_slug(raw):
    assert normalize_kernel_ref(raw) == "user1/epub-tts-batch-abc"


@pytest.mark.parametrize("raw", [None, "", "/code/user1", "epub-tts-batch-abc", "/"])
def test_normalize_kernel_ref_falls_back_when_no_username_is_left(raw):
    assert normalize_kernel_ref(raw, fallback="user1/x") == "user1/x"


def test_push_kernel_normalizes_the_url_path_ref_that_kaggle_really_returns(tmp_path):
    """A live SaveKernel answered with ref="/code/<user>/<slug>"; every later call
    (kernel_status, kernel_output) needs a bare "<user>/<slug>"."""
    _write_notebook(tmp_path)
    fake = FakeRequest([{"status": 200, "body": json.dumps({"ref": "/code/user1/epub-tts-batch-18"})}])
    ref = push_kernel(
        ACCOUNT, tmp_path, {"id": "user1/epub-tts-batch-18", "code_file": "nb.ipynb"}, request=fake,
    )
    assert ref == "user1/epub-tts-batch-18"


def test_push_kernel_falls_back_to_the_requested_id_when_the_response_carries_no_ref(tmp_path):
    _write_notebook(tmp_path)
    fake = FakeRequest([{"status": 200, "body": json.dumps({"versionNumber": 1})}])
    ref = push_kernel(ACCOUNT, tmp_path, {"id": "user1/x", "code_file": "nb.ipynb"}, request=fake)
    assert ref == "user1/x"


def test_kernel_status_reads_an_absent_status_field_as_queued():
    """QUEUED is the zero value of Kaggle's KernelWorkerStatus and proto3 JSON drops
    zero-valued fields, so a just-pushed kernel can answer with no status at all."""
    fake = FakeRequest([{"status": 200, "body": json.dumps({})}])
    assert kernel_status(ACCOUNT, "user1/x", request=fake) == KernelStatus.QUEUED


def test_kernel_status_accepts_a_url_path_ref(tmp_path):
    fake = FakeRequest([{"status": 200, "body": json.dumps({"status": "RUNNING"})}])
    assert kernel_status(ACCOUNT, "/code/user1/x", request=fake) == KernelStatus.RUNNING
    _, _, _, body = fake.calls[0]
    assert json.loads(body) == {"userName": "user1", "kernelSlug": "x"}


def test_push_kernel_sends_the_notebook_as_a_single_text_field_with_outputs_stripped(tmp_path):
    _write_notebook(tmp_path)
    fake = FakeRequest([{"status": 200, "body": json.dumps({"ref": "user1/x"})}])
    push_kernel(ACCOUNT, tmp_path, {
        "id": "user1/x", "code_file": "nb.ipynb", "title": "T", "is_private": True,
        "enable_gpu": True, "enable_internet": True, "dataset_sources": ["user1/data"],
    }, request=fake)
    _, _, _, body = fake.calls[0]
    payload = json.loads(body)
    assert payload["slug"] == "user1/x"
    assert payload["newTitle"] == "T"
    assert payload["isPrivate"] is True
    assert payload["enableGpu"] is True
    assert payload["datasetDataSources"] == ["user1/data"]
    notebook = json.loads(payload["text"])
    assert notebook["cells"][0]["source"] == "import os\nprint(os)\n"
    assert notebook["cells"][0]["outputs"] == []


def test_push_kernel_sends_machine_shape_when_given(tmp_path):
    """A concrete GPU type (T4, not whatever the scheduler hands out): P100 (sm_60)
    is visible to CUDA but the PyTorch in Kaggle's image cannot execute on it."""
    _write_notebook(tmp_path)
    fake = FakeRequest([{"status": 200, "body": json.dumps({"ref": "user1/x"})}])
    push_kernel(ACCOUNT, tmp_path, {
        "id": "user1/x", "code_file": "nb.ipynb", "machine_shape": "NvidiaTeslaT4",
    }, request=fake)
    _, _, _, body = fake.calls[0]
    assert json.loads(body)["machineShape"] == "NvidiaTeslaT4"


def test_push_kernel_omits_machine_shape_when_not_given(tmp_path):
    _write_notebook(tmp_path)
    fake = FakeRequest([{"status": 200, "body": json.dumps({"ref": "user1/x"})}])
    push_kernel(ACCOUNT, tmp_path, {"id": "user1/x", "code_file": "nb.ipynb"}, request=fake)
    _, _, _, body = fake.calls[0]
    assert "machineShape" not in json.loads(body)


def test_push_kernel_raises_on_http_error(tmp_path):
    _write_notebook(tmp_path)
    fake = FakeRequest([{"status": 500, "body": "boom"}])
    with pytest.raises(RuntimeError):
        push_kernel(ACCOUNT, tmp_path, {"id": "user1/x", "code_file": "nb.ipynb"}, request=fake)


def test_push_kernel_raises_on_an_error_field_in_a_200_response(tmp_path):
    _write_notebook(tmp_path)
    fake = FakeRequest([{"status": 200, "body": json.dumps({"error": "bad slug"})}])
    with pytest.raises(RuntimeError, match="bad slug"):
        push_kernel(ACCOUNT, tmp_path, {"id": "user1/x", "code_file": "nb.ipynb"}, request=fake)


@pytest.mark.parametrize("raw,expected", [
    ("QUEUED", KernelStatus.QUEUED),
    ("NEW_SCRIPT", KernelStatus.QUEUED),
    ("RUNNING", KernelStatus.RUNNING),
    ("COMPLETE", KernelStatus.COMPLETE),
    ("ERROR", KernelStatus.ERROR),
    ("CANCEL_REQUESTED", KernelStatus.CANCELLED),
    ("CANCEL_ACKNOWLEDGED", KernelStatus.CANCELLED),
])
def test_kernel_status_maps_known_values(raw, expected):
    fake = FakeRequest([{"status": 200, "body": json.dumps({"status": raw})}])
    assert kernel_status(ACCOUNT, "user1/x", request=fake) == expected
    url, method, _, body = fake.calls[0]
    assert url == "https://api.kaggle.com/v1/kernels.KernelsApiService/GetKernelSessionStatus"
    assert method == "POST"
    assert json.loads(body) == {"userName": "user1", "kernelSlug": "x"}


def test_kernel_status_raises_on_an_unknown_value():
    fake = FakeRequest([{"status": 200, "body": json.dumps({"status": "somethingNew"})}])
    with pytest.raises(RuntimeError):
        kernel_status(ACCOUNT, "user1/x", request=fake)


def test_kernel_status_raises_on_http_error():
    fake = FakeRequest([{"status": 404, "body": "not found"}])
    with pytest.raises(RuntimeError):
        kernel_status(ACCOUNT, "user1/x", request=fake)


def test_kernel_output_downloads_every_file_into_dest_dir(tmp_path):
    fake = FakeRequest([
        {"status": 200, "body": json.dumps({"files": [
            {"fileName": "result/1_001.wav", "url": "https://signed/result/1_001.wav"},
            {"fileName": "result/1_001.timeline.json", "url": "https://signed/result/1_001.timeline.json"},
        ]})},
        {"status": 200, "body": b"WAVDATA"},
        {"status": 200, "body": b'{"version": 1}'},
    ])
    dest = tmp_path / "out"
    paths = kernel_output(ACCOUNT, "user1/x", dest, request=fake)
    assert {p.relative_to(dest).as_posix() for p in paths} == {
        "result/1_001.wav", "result/1_001.timeline.json",
    }
    assert (dest / "result" / "1_001.wav").read_bytes() == b"WAVDATA"
    list_url, list_method, _, list_body = fake.calls[0]
    assert list_url == "https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernelSessionOutput"
    assert list_method == "POST"
    assert json.loads(list_body) == {"userName": "user1", "kernelSlug": "x", "pageSize": 100}
    # Signed download URLs get no Kaggle auth header.
    assert fake.calls[1][2] == {}


def test_kernel_output_skips_hidden_cache_blobs():
    """The notebook persists HF/pip caches under /kaggle/working/.cache and Kaggle
    captures them as session output (observed live: GBs of model blobs). The import
    pipeline only reads result/*.wav, so dot-paths must not be downloaded -- fetching
    them stalls the worker past the reaper window."""
    fake = FakeRequest([
        {"status": 200, "body": json.dumps({"files": [
            {"fileName": "result/18_003.wav", "url": "https://signed/result.wav"},
            {"fileName": ".cache/huggingface/hub/models--x/blobs/abc123", "url": "https://signed/blob"},
        ]})},
        {"status": 200, "body": b"WAVDATA"},
    ])
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        paths = kernel_output(ACCOUNT, "user1/x", Path(tmp), request=fake)
    assert [p.name for p in paths] == ["18_003.wav"]
    assert len(fake.calls) == 2  # listing + the one real download, no blob fetch


def test_kernel_output_follows_pages_past_hidden_cache_files(tmp_path):
    fake = FakeRequest([
        {"status": 200, "body": json.dumps({
            "files": [{"fileName": ".cache/model/blob", "url": "https://signed/blob"}],
            "nextPageToken": "page-2",
        })},
        {"status": 200, "body": json.dumps({"files": [
            {"fileName": "result/19_089.wav", "url": "https://signed/result.wav"},
        ]})},
        {"status": 200, "body": b"WAVDATA"},
    ])

    paths = kernel_output(ACCOUNT, "user1/x", tmp_path, version_label="v1", request=fake)

    assert [path.relative_to(tmp_path).as_posix() for path in paths] == ["result/19_089.wav"]
    assert json.loads(fake.calls[1][3]) == {
        "userName": "user1", "kernelSlug": "x", "pageSize": 100,
        "pageToken": "page-2", "versionLabel": "v1",
    }
    assert fake.calls[2][0] == "https://signed/result.wav"


def test_kernel_output_returns_empty_list_when_no_files():
    fake = FakeRequest([{"status": 200, "body": json.dumps({"files": []})}])
    assert kernel_output(ACCOUNT, "user1/x", "/tmp/does-not-matter", request=fake) == []


def test_cancel_kernel_is_a_safe_no_op():
    cancel_kernel(ACCOUNT, "user1/x")  # no request/network call at all; must not raise


def _write_package(tmp_path):
    (tmp_path / "batch_manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "reference.wav").write_bytes(b"RIFF")
    (tmp_path / "colab_kaggle_batch_tts_template.ipynb").write_text("{}", encoding="utf-8")
    patch_dir = tmp_path / "patches" / "patch_000"
    patch_dir.mkdir(parents=True)
    (patch_dir / "manifest.json").write_text("{}", encoding="utf-8")


def test_create_dataset_uploads_the_package_as_one_zip_then_creates(tmp_path):
    """Blob uploads carry basenames only and CreateDataset's files list carries
    tokens only, so per-file upload cannot preserve patches/patch_NNN/ -- the whole
    package must travel as a single zip (mirrors kaggle-cli's --dir-mode zip)."""
    _write_package(tmp_path)
    fake = FakeRequest([
        {"status": 200, "body": json.dumps({"token": "tok-zip", "createUrl": "https://upload/pkg"})},
        {"status": 200, "body": ""},  # PUT to the presigned URL
        {"status": 200, "body": json.dumps({})},  # CreateDataset, no error
    ])
    ref = create_dataset(ACCOUNT, tmp_path, "epub-tts-data-1-abc", "EPUB TTS data 1", request=fake)
    assert ref == "user1/epub-tts-data-1-abc"

    start_calls = [c for c in fake.calls if c[0].endswith("StartBlobUpload")]
    assert len(start_calls) == 1
    assert json.loads(start_calls[0][3])["name"] == "epub-tts-data-1-abc.zip"

    put_calls = [c for c in fake.calls if c[1] == "PUT"]
    assert len(put_calls) == 1
    assert put_calls[0][0] == "https://upload/pkg"
    assert put_calls[0][2] == {}  # no Kaggle auth on the presigned PUT

    import io
    import zipfile
    with zipfile.ZipFile(io.BytesIO(put_calls[0][3])) as zf:
        assert sorted(zf.namelist()) == [
            "batch_manifest.json",
            "patches/patch_000/manifest.json",
            "reference.wav",
        ]  # hierarchy preserved, notebook (kernel code) excluded

    create_call = fake.calls[-1]
    assert create_call[0] == "https://api.kaggle.com/v1/datasets.DatasetApiService/CreateDataset"
    payload = json.loads(create_call[3])
    assert payload["slug"] == "epub-tts-data-1-abc"
    assert payload["ownerSlug"] == "user1"
    assert payload["files"] == [{"token": "tok-zip"}]


def test_create_dataset_raises_on_an_error_field(tmp_path):
    (tmp_path / "batch_manifest.json").write_text("{}", encoding="utf-8")
    fake = FakeRequest([
        {"status": 200, "body": json.dumps({"token": "t", "createUrl": "https://upload/x"})},
        {"status": 200, "body": ""},
        {"status": 200, "body": json.dumps({"error": "slug already in use"})},
    ])
    with pytest.raises(RuntimeError, match="slug already in use"):
        create_dataset(ACCOUNT, tmp_path, "dup-slug", "Title", request=fake)


def test_push_kernel_raises_when_kaggle_rejects_a_data_source(tmp_path):
    """SaveKernel does NOT fail on a bad source -- the kernel runs without input.
    Surfacing invalidDatasetSources loudly beats a kernel that later dies in Cell 4."""
    _write_notebook(tmp_path)
    fake = FakeRequest([{"status": 200, "body": json.dumps({
        "ref": "user1/x", "invalidDatasetSources": ["user1/not-ready-yet"],
    })}])
    with pytest.raises(RuntimeError, match="not-ready-yet"):
        push_kernel(ACCOUNT, tmp_path, {"id": "user1/x", "code_file": "nb.ipynb"}, request=fake)


@pytest.mark.parametrize("raw,expected", [("READY", "ready"), ("PENDING", "pending"), (5, "ready"), (6, "failed")])
def test_dataset_status_normalizes_names_and_numbers(raw, expected):
    fake = FakeRequest([{"status": 200, "body": json.dumps({"status": raw})}])
    assert dataset_status(ACCOUNT, "user1/my-data", request=fake) == expected
    url, method, _, body = fake.calls[0]
    assert url == "https://api.kaggle.com/v1/datasets.DatasetApiService/GetDatasetStatus"
    assert method == "POST"
    assert json.loads(body) == {"ownerSlug": "user1", "datasetSlug": "my-data"}


def test_dataset_status_rejects_a_bare_slug():
    with pytest.raises(ValueError, match="username/slug"):
        dataset_status(ACCOUNT, "no-username-here")


def test_dataset_files_returns_the_server_listing():
    # Wire key is "datasetFiles" (kagglesdk exposes it as .files/.dataset_files
    # on the parsed object, but the raw JSON carries "datasetFiles").
    fake = FakeRequest([{"status": 200, "body": json.dumps({"datasetFiles": [
        {"name": "epub-tts-data-1-abc.zip", "totalBytes": 1234},
    ]})}])
    files = dataset_files(ACCOUNT, "user1/my-data", request=fake)
    assert [f["name"] for f in files] == ["epub-tts-data-1-abc.zip"]
    url, method, _, body = fake.calls[0]
    assert url == "https://api.kaggle.com/v1/datasets.DatasetApiService/ListDatasetFiles"
    assert method == "POST"
    assert json.loads(body) == {"ownerSlug": "user1", "datasetSlug": "my-data", "pageSize": 100}


def test_dataset_files_tolerates_an_empty_listing():
    fake = FakeRequest([{"status": 200, "body": json.dumps({"datasetFiles": []})}])
    assert dataset_files(ACCOUNT, "user1/my-data", request=fake) == []


def test_kernel_log_concatenates_the_json_entries():
    entries = [
        {"stream_name": "stdout", "time": 1.0, "data": "hello "},
        {"stream_name": "stderr", "time": 2.0, "data": "boom\n"},
    ]
    fake = FakeRequest([{"status": 200, "body": json.dumps({"log": json.dumps(entries)})}])
    assert kernel_log(ACCOUNT, "user1/x", request=fake) == "hello boom\n"
    url, method, _, _body = fake.calls[0]
    assert url == "https://api.kaggle.com/v1/kernels.KernelsApiService/ListKernelSessionOutput"
    assert method == "POST"


def test_kernel_log_returns_empty_string_when_the_session_logged_nothing():
    fake = FakeRequest([{"status": 200, "body": json.dumps({})}])
    assert kernel_log(ACCOUNT, "user1/x", request=fake) == ""


def test_kernel_log_passes_through_a_non_json_log():
    fake = FakeRequest([{"status": 200, "body": json.dumps({"log": "plain traceback"})}])
    assert kernel_log(ACCOUNT, "user1/x", request=fake) == "plain traceback"
