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
    KaggleAccount, KernelStatus, cancel_kernel, create_dataset, kernel_output,
    kernel_status, push_kernel,
)


class FakeRequest:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, *, method, headers, body=None):
        self.calls.append((url, method, headers, body))
        return self._responses.pop(0)


ACCOUNT = KaggleAccount(username="user1", api_key="secret-key")

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


def test_kernel_output_returns_empty_list_when_no_files():
    fake = FakeRequest([{"status": 200, "body": json.dumps({"files": []})}])
    assert kernel_output(ACCOUNT, "user1/x", "/tmp/does-not-matter", request=fake) == []


def test_cancel_kernel_is_a_safe_no_op():
    cancel_kernel(ACCOUNT, "user1/x")  # no request/network call at all; must not raise


def test_create_dataset_uploads_each_file_then_creates(tmp_path):
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "reference.wav").write_bytes(b"RIFF")
    fake = FakeRequest([
        {"status": 200, "body": json.dumps({"token": "tok-manifest", "createUrl": "https://upload/manifest"})},
        {"status": 200, "body": ""},  # PUT to the presigned URL
        {"status": 200, "body": json.dumps({"token": "tok-ref", "createUrl": "https://upload/reference"})},
        {"status": 200, "body": ""},
        {"status": 200, "body": json.dumps({})},  # CreateDataset, no error
    ])
    ref = create_dataset(ACCOUNT, tmp_path, "epub-tts-data-1-abc", "EPUB TTS data 1", request=fake)
    assert ref == "user1/epub-tts-data-1-abc"

    start_calls = [c for c in fake.calls if c[0].endswith("StartBlobUpload")]
    assert len(start_calls) == 2
    put_calls = [c for c in fake.calls if c[1] == "PUT"]
    assert {c[0] for c in put_calls} == {"https://upload/manifest", "https://upload/reference"}
    assert all(c[2] == {} for c in put_calls)  # no Kaggle auth on the presigned PUT

    create_call = fake.calls[-1]
    assert create_call[0] == "https://api.kaggle.com/v1/datasets.DatasetApiService/CreateDataset"
    payload = json.loads(create_call[3])
    assert payload["slug"] == "epub-tts-data-1-abc"
    assert payload["ownerSlug"] == "user1"
    assert {"token": "tok-manifest"} in payload["files"]
    assert {"token": "tok-ref"} in payload["files"]


def test_create_dataset_raises_on_an_error_field(tmp_path):
    (tmp_path / "manifest.json").write_text("{}", encoding="utf-8")
    fake = FakeRequest([
        {"status": 200, "body": json.dumps({"token": "t", "createUrl": "https://upload/x"})},
        {"status": 200, "body": ""},
        {"status": 200, "body": json.dumps({"error": "slug already in use"})},
    ])
    with pytest.raises(RuntimeError, match="slug already in use"):
        create_dataset(ACCOUNT, tmp_path, "dup-slug", "Title", request=fake)
