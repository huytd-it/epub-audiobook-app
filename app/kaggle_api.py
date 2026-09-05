"""Raw HTTP client for the Kaggle API (https://api.kaggle.com/v1) -- no dependency on
the `kaggle` pip package, same style as `app.tts_api_providers`.

Wire shapes here were verified (2026-09) against Kaggle's own official client source
(github.com/Kaggle/kaggle-cli's kaggle_api_extended.py and
github.com/Kaggle/kaggle-sdk-python's generated request/response classes), NOT against
a live account -- see Task 12 of the implementation plan for what that verification
covered and what it did not (see "Known gaps" below). Key findings that corrected the
original guesses in this file:

- Auth is HTTP Basic (username, api_key), not Bearer -- confirmed by
  KaggleHttpClient._try_fill_auth setting `session.auth = (username, password)`.
- Every call is POST to `{BASE_URL}/{service}.{Service}/{Method}`
  (e.g. "kernels.KernelsApiService/SaveKernel"), not REST-with-query-params.
- A kernel push can only carry ONE file (the notebook itself, as a single `text`
  field) plus metadata -- there is no "attach arbitrary local files" mechanism.
  Any other data (our manifest + reference clip) MUST travel as a Kaggle Dataset,
  referenced by slug in the kernel's dataset_sources. Datasets are built from
  individually-uploaded "blobs": StartBlobUpload returns a token + a presigned
  upload URL, the raw bytes get PUT there, and the token is what CreateDataset
  references -- see create_dataset()/_upload_blob().
- KernelWorkerStatus values are upper-snake-case (QUEUED, RUNNING, COMPLETE, ERROR,
  CANCEL_REQUESTED, CANCEL_ACKNOWLEDGED, NEW_SCRIPT), not the lowercase/camelCase
  guessed originally.

Known gaps (still need a real account to close):
- CancelKernelSession takes a numeric kernel_session_id, which no call this module
  makes (push/status/output) returns anywhere. cancel_kernel() is therefore a
  documented no-op until that lookup is found -- callers already treat it as
  best-effort, so this degrades safely (the job just stops polling and returns).
- create_dataset() always creates a brand-new dataset (unique slug per push cycle)
  rather than versioning one in place -- simpler and safe, at the cost of leaving
  small throwaway datasets on the account across a multi-cycle batch. Periodic
  cleanup of old "epub-tts-data-*" datasets is a reasonable follow-up, not done here.
- The dataset license is hardcoded to "CC0-1.0"; confirm that's an accepted
  license_name value (or make it configurable) before relying on this for real.

Every public function takes an injectable `request` callable
(``request(url, *, method, headers, body=None) -> {"status": int, "body": bytes|str}``)
defaulting to `_request` (real `urllib` calls); tests pass a fake instead so nothing
here touches the network."""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

BASE_URL = "https://api.kaggle.com/v1"
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


# Raw values straight from kagglesdk's KernelWorkerStatus enum (kernels_enums.py).
_STATUS_MAP = {
    "QUEUED": KernelStatus.QUEUED,
    "NEW_SCRIPT": KernelStatus.QUEUED,
    "RUNNING": KernelStatus.RUNNING,
    "COMPLETE": KernelStatus.COMPLETE,
    "ERROR": KernelStatus.ERROR,
    "CANCEL_REQUESTED": KernelStatus.CANCELLED,
    "CANCEL_ACKNOWLEDGED": KernelStatus.CANCELLED,
}


def _auth_header(account: KaggleAccount) -> dict[str, str]:
    token = base64.b64encode(f"{account.username}:{account.api_key}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def _request(url: str, *, method: str, headers: dict[str, str], body=None) -> dict:
    """Real network call. `body`, when given, is already the final wire payload (a str
    or bytes) -- callers serialize their own payload so the injected fake in tests sees
    exactly what would be sent, not a pre-serialization form."""
    data = None
    if body is not None:
        data = body.encode("utf-8") if isinstance(body, str) else body
        if isinstance(body, str):
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


def _rpc(request: RequestFn, account: KaggleAccount, service: str, method: str, body: dict) -> dict:
    """POST to the RPC-style path every kagglesdk call actually uses:
    {BASE_URL}/{service}/{method}, e.g. "kernels.KernelsApiService/SaveKernel"."""
    raw = _call(
        request, f"{BASE_URL}/{service}/{method}", method="POST",
        headers=_auth_header(account), body=json.dumps(body),
    )
    return json.loads(raw)


def _kernel_slug(kernel_ref: str) -> tuple[str, str]:
    username, _, slug = kernel_ref.partition("/")
    if not username or not slug:
        raise ValueError(f"kernel_ref must look like 'username/slug', got {kernel_ref!r}")
    return username, slug


def _load_notebook_text(package_dir: Path, code_file: str) -> str:
    """Read and normalize the notebook the same way kaggle-cli's kernels_push does:
    strip code-cell outputs and join each cell's `source` list into one string (the
    server rejects a list there even though the .ipynb spec allows it)."""
    raw = (Path(package_dir) / code_file).read_text(encoding="utf-8")
    notebook = json.loads(raw)
    for cell in notebook.get("cells", []):
        if cell.get("cell_type") == "code" and "outputs" in cell:
            cell["outputs"] = []
        if isinstance(cell.get("source"), list):
            cell["source"] = "".join(cell["source"])
    return json.dumps(notebook)


def push_kernel(
    account: KaggleAccount, package_dir: Path, metadata: dict, *, request: RequestFn = _request,
) -> str:
    """Push (create or version) a kernel. `metadata` mirrors kernel-metadata.json
    (id="username/slug", title, code_file, language, kernel_type, is_private,
    enable_gpu, enable_internet, dataset_sources, ...); the notebook named by
    code_file is read from package_dir and sent as the single `text` field -- a
    kernel push carries no other files (see module docstring). Returns the pushed
    kernel's ref ("username/slug")."""
    package_dir = Path(package_dir)
    text = _load_notebook_text(package_dir, metadata["code_file"])
    body = {
        "slug": metadata["id"],
        "newTitle": metadata.get("title", ""),
        "text": text,
        "language": metadata.get("language", "python"),
        "kernelType": metadata.get("kernel_type", "notebook"),
        "isPrivate": bool(metadata.get("is_private", True)),
        "enableGpu": bool(metadata.get("enable_gpu", False)),
        "enableInternet": bool(metadata.get("enable_internet", True)),
        "datasetDataSources": metadata.get("dataset_sources") or [],
        "kernelDataSources": metadata.get("kernel_sources") or [],
        "modelDataSources": metadata.get("model_sources") or [],
        "competitionDataSources": metadata.get("competition_sources") or [],
    }
    data = _rpc(request, account, "kernels.KernelsApiService", "SaveKernel", body)
    if data.get("error"):
        raise RuntimeError(f"Kaggle kernel push failed: {data['error']}")
    return data.get("ref") or metadata["id"]


def kernel_status(
    account: KaggleAccount, kernel_ref: str, *, request: RequestFn = _request,
) -> KernelStatus:
    username, slug = _kernel_slug(kernel_ref)
    data = _rpc(request, account, "kernels.KernelsApiService", "GetKernelSessionStatus", {
        "userName": username, "kernelSlug": slug,
    })
    raw = str(data.get("status", "")).strip()
    mapped = _STATUS_MAP.get(raw)
    if mapped is None:
        raise RuntimeError(f"Unknown Kaggle kernel status: {raw!r}")
    return mapped


def kernel_output(
    account: KaggleAccount, kernel_ref: str, dest_dir: Path, *, request: RequestFn = _request,
) -> list[Path]:
    """Download every output file into dest_dir (mirroring each entry's fileName as a
    relative path) and return the local paths written. Only the first page is
    fetched -- a batch's output (a handful of result WAVs + timelines) fits in one
    page in practice; add nextPageToken handling if that stops being true."""
    username, slug = _kernel_slug(kernel_ref)
    dest_dir = Path(dest_dir)
    data = _rpc(request, account, "kernels.KernelsApiService", "ListKernelSessionOutput", {
        "userName": username, "kernelSlug": slug, "pageSize": 100,
    })
    entries = data.get("files") or []

    written = []
    for entry in entries:
        # Presigned download URLs need no Kaggle auth of their own.
        content = _call(request, entry["url"], method="GET", headers={})
        target = dest_dir / entry["fileName"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, (bytes, bytearray)) else content.encode("utf-8"))
        written.append(target)
    return written


def cancel_kernel(
    account: KaggleAccount, kernel_ref: str, *, request: RequestFn = _request,
) -> None:
    """Best-effort: callers treat this as fire-and-forget and never see an exception
    from it. Currently a documented no-op -- CancelKernelSession needs a numeric
    kernel_session_id that no other call in this module surfaces (see module
    docstring's "Known gaps"). Safe to leave as-is: the caller stops polling and
    returns regardless of whether Kaggle itself was told to stop."""
    return


def _upload_blob(
    account: KaggleAccount, path: Path, *, request: RequestFn = _request,
) -> str:
    """Upload one file as a Kaggle "blob" and return its opaque token, for use in a
    subsequent create_dataset() files list."""
    path = Path(path)
    start = _rpc(request, account, "blobs.BlobApiService", "StartBlobUpload", {
        "type": "DATASET", "name": path.name, "contentLength": path.stat().st_size,
    })
    # Presigned upload URL: a plain PUT of the raw bytes, no Kaggle auth header.
    _call(request, start["createUrl"], method="PUT", headers={}, body=path.read_bytes())
    return start["token"]


def create_dataset(
    account: KaggleAccount, package_dir: Path, slug: str, title: str,
    *, request: RequestFn = _request,
) -> str:
    """Upload every file under package_dir as a new private Kaggle Dataset and
    return its ref ("username/slug"), for use in a kernel push's dataset_sources.
    Always creates a fresh dataset (see module docstring's "Known gaps" on why this
    does not version an existing one in place)."""
    package_dir = Path(package_dir)
    files = [
        {"token": _upload_blob(account, path, request=request)}
        for path in sorted(package_dir.rglob("*")) if path.is_file()
    ]
    data = _rpc(request, account, "datasets.DatasetApiService", "CreateDataset", {
        "ownerSlug": account.username,
        "slug": slug,
        "title": title,
        "licenseName": "CC0-1.0",
        "isPrivate": True,
        "files": files,
    })
    if data.get("error"):
        raise RuntimeError(f"Kaggle dataset create failed: {data['error']}")
    return f"{account.username}/{slug}"
