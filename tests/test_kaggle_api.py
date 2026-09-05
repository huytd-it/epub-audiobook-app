"""app.kaggle_api: client HTTP thuần cho Kaggle REST API, `request` được inject để test
không chạm mạng thật."""
from __future__ import annotations

import json

import pytest

from app.kaggle_api import KaggleAccount, KernelStatus, cancel_kernel, kernel_output, kernel_status, push_kernel


class FakeRequest:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, url, *, method, headers, body=None):
        self.calls.append((url, method, headers, body))
        return self._responses.pop(0)


ACCOUNT = KaggleAccount(username="user1", api_key="secret-key")


def test_push_kernel_returns_kernel_ref(tmp_path):
    fake = FakeRequest([{"status": 200, "body": json.dumps({"ref": "user1/epub-tts-batch-abc"})}])
    ref = push_kernel(ACCOUNT, tmp_path, {"id": "user1/epub-tts-batch-abc"}, request=fake)
    assert ref == "user1/epub-tts-batch-abc"
    assert fake.calls[0][1] == "POST"


def test_push_kernel_sends_auth_header(tmp_path):
    fake = FakeRequest([{"status": 200, "body": json.dumps({"ref": "user1/x"})}])
    push_kernel(ACCOUNT, tmp_path, {"id": "user1/x"}, request=fake)
    _, _, headers, _ = fake.calls[0]
    assert "Authorization" in headers


def test_push_kernel_bundles_every_file_under_package_dir(tmp_path):
    (tmp_path / "manifest.json").write_text('{"a": 1}', encoding="utf-8")
    (tmp_path / "patches").mkdir()
    (tmp_path / "patches" / "patch_000.txt").write_text("hello", encoding="utf-8")
    fake = FakeRequest([{"status": 200, "body": json.dumps({"ref": "user1/x"})}])
    push_kernel(ACCOUNT, tmp_path, {"id": "user1/x"}, request=fake)
    _, _, _, body = fake.calls[0]
    payload = json.loads(body)
    assert "manifest.json" in payload["files"]
    assert "patches/patch_000.txt" in payload["files"]


def test_push_kernel_raises_on_http_error(tmp_path):
    fake = FakeRequest([{"status": 500, "body": "boom"}])
    with pytest.raises(RuntimeError):
        push_kernel(ACCOUNT, tmp_path, {"id": "user1/x"}, request=fake)


@pytest.mark.parametrize("raw,expected", [
    ("queued", KernelStatus.QUEUED),
    ("running", KernelStatus.RUNNING),
    ("complete", KernelStatus.COMPLETE),
    ("error", KernelStatus.ERROR),
    ("cancelAcknowledged", KernelStatus.CANCELLED),
])
def test_kernel_status_maps_known_values(raw, expected):
    fake = FakeRequest([{"status": 200, "body": json.dumps({"status": raw})}])
    assert kernel_status(ACCOUNT, "user1/x", request=fake) == expected


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
            {"fileName": "result/1_001.wav", "url": "https://kaggle/x/result/1_001.wav"},
            {"fileName": "result/1_001.timeline.json", "url": "https://kaggle/x/result/1_001.timeline.json"},
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
    assert (dest / "result" / "1_001.timeline.json").read_bytes() == b'{"version": 1}'


def test_kernel_output_returns_empty_list_when_no_files():
    fake = FakeRequest([{"status": 200, "body": json.dumps({"files": []})}])
    assert kernel_output(ACCOUNT, "user1/x", "/tmp/does-not-matter", request=fake) == []


def test_cancel_kernel_swallows_errors():
    fake = FakeRequest([{"status": 500, "body": "boom"}])
    cancel_kernel(ACCOUNT, "user1/x", request=fake)  # phải không raise


def test_cancel_kernel_sends_a_post():
    fake = FakeRequest([{"status": 200, "body": "{}"}])
    cancel_kernel(ACCOUNT, "user1/x", request=fake)
    assert fake.calls[0][1] == "POST"
