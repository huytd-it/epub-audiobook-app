"""Raw HTTP client for the Kaggle Kernels REST API (https://www.kaggle.com/api/v1) --
no dependency on the `kaggle` pip package, same style as `app.tts_api_providers`.

Every public function takes an injectable `request` callable
(``request(url, *, method, headers, body=None) -> {"status": int, "body": bytes|str}``)
defaulting to `_request` (real `urllib` calls); tests pass a fake instead so nothing
here touches the network.

The exact wire shapes below (auth header, push payload, output listing) are a
reasonable first cut, not verified against Kaggle's live API yet -- see the
"Implementation-time verification required" section of the design doc and Task 12 of
the implementation plan. `_auth_header` is the one place to fix if the auth scheme
turns out to be different."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

BASE_URL = "https://www.kaggle.com/api/v1"
_TIMEOUT_SECONDS = 60.0

RequestFn = Callable[..., dict]


@dataclass(frozen=True)
class KaggleAccount:
    username: str
    api_key: str


class KernelStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"


_STATUS_MAP = {
    "queued": KernelStatus.QUEUED,
    "running": KernelStatus.RUNNING,
    "complete": KernelStatus.COMPLETE,
    "error": KernelStatus.ERROR,
    "cancelAcknowledged": KernelStatus.CANCELLED,
    "cancelled": KernelStatus.CANCELLED,
}


def _auth_header(account: KaggleAccount) -> dict[str, str]:
    return {"Authorization": f"Bearer {account.api_key}"}


def _request(url: str, *, method: str, headers: dict[str, str], body=None) -> dict:
    """Real network call. `body`, when given, is already the final wire payload (a str
    or bytes) -- callers serialize their own payload so the injected fake in tests sees
    exactly what would be sent, not a pre-serialization form."""
    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        headers = {**headers, "Content-Type": "application/json"}
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
            return {"status": response.status, "body": response.read()}
    except urllib.error.HTTPError as exc:
        return {"status": exc.code, "body": exc.read()}


def _call(request: RequestFn, url: str, *, method: str, headers: dict[str, str], body=None):
    response = request(url, method=method, headers=headers, body=body)
    status = response["status"]
    if not (200 <= status < 300):
        raise RuntimeError(f"Kaggle API {method} {url} failed: HTTP {status}: {response['body']!r}")
    return response["body"]


def _kernel_slug(kernel_ref: str) -> tuple[str, str]:
    username, _, slug = kernel_ref.partition("/")
    if not username or not slug:
        raise ValueError(f"kernel_ref must look like 'username/slug', got {kernel_ref!r}")
    return username, slug


def push_kernel(
    account: KaggleAccount, package_dir: Path, metadata: dict, *, request: RequestFn = _request,
) -> str:
    """Push (create or version) a kernel from every file under package_dir plus
    kernel-metadata.json's fields. Returns the kernel_ref ("username/slug")."""
    package_dir = Path(package_dir)
    files = {}
    for path in sorted(package_dir.rglob("*")):
        if path.is_file():
            rel = path.relative_to(package_dir).as_posix()
            files[rel] = base64.b64encode(path.read_bytes()).decode("ascii")
    payload = {"kernelMetadata": metadata, "files": files}
    body = _call(request, f"{BASE_URL}/kernels/push", method="POST",
                 headers=_auth_header(account), body=json.dumps(payload))
    data = json.loads(body)
    return data["ref"]


def kernel_status(
    account: KaggleAccount, kernel_ref: str, *, request: RequestFn = _request,
) -> KernelStatus:
    username, slug = _kernel_slug(kernel_ref)
    url = f"{BASE_URL}/kernels/status?user_name={username}&kernel_slug={slug}"
    body = _call(request, url, method="GET", headers=_auth_header(account))
    raw = str(json.loads(body).get("status", "")).strip()
    mapped = _STATUS_MAP.get(raw)
    if mapped is None:
        raise RuntimeError(f"Unknown Kaggle kernel status: {raw!r}")
    return mapped


def kernel_output(
    account: KaggleAccount, kernel_ref: str, dest_dir: Path, *, request: RequestFn = _request,
) -> list[Path]:
    """Download every output file into dest_dir (mirroring each entry's fileName as a
    relative path) and return the local paths written."""
    username, slug = _kernel_slug(kernel_ref)
    dest_dir = Path(dest_dir)
    list_url = f"{BASE_URL}/kernels/output?user_name={username}&kernel_slug={slug}"
    body = _call(request, list_url, method="GET", headers=_auth_header(account))
    entries = json.loads(body).get("files", [])

    written = []
    for entry in entries:
        content = _call(request, entry["url"], method="GET", headers=_auth_header(account))
        target = dest_dir / entry["fileName"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, (bytes, bytearray)) else content.encode("utf-8"))
        written.append(target)
    return written


def cancel_kernel(
    account: KaggleAccount, kernel_ref: str, *, request: RequestFn = _request,
) -> None:
    """Best-effort: a job the user cancelled must still be marked cancelled locally
    even if this call itself fails, so every error here is swallowed."""
    username, slug = _kernel_slug(kernel_ref)
    url = f"{BASE_URL}/kernels/{username}/{slug}/cancel"
    try:
        _call(request, url, method="POST", headers=_auth_header(account))
    except Exception:
        pass
