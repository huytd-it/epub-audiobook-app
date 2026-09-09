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
  referenced by slug in the kernel's dataset_sources. The package travels as ONE
  zip blob (StartBlobUpload returns a token + a presigned upload URL, the raw
  bytes get PUT there, and the token is what CreateDataset references): per-file
  upload cannot work because blobs carry bare basenames and CreateDataset's files
  list carries tokens only, so no directory structure survives -- every patch's
  manifest.json would land flat and overwrite the others. Single-zip mirrors
  kaggle-cli's --dir-mode zip for folders -- see create_dataset().
- A rejected data source does NOT fail SaveKernel (the kernel runs without the
  input); push_kernel() raises on invalidDatasetSources instead of stranding the
  run. A fresh dataset also needs time to process, so the handler polls
  dataset_status() until "ready" before pushing.
- KernelWorkerStatus values are upper-snake-case (QUEUED, RUNNING, COMPLETE, ERROR,
  CANCEL_REQUESTED, CANCEL_ACKNOWLEDGED, NEW_SCRIPT), not the lowercase/camelCase
  guessed originally.

The first run against a real account (2026-09-06) confirmed the auth scheme, the RPC
paths, and the blob-upload/CreateDataset sequence all reach the server, and turned up
two things reading the source had missed -- both fixed here, both documented at their
call site:

- SaveKernel's `ref` comes back as a URL path ("/code/<user>/<slug>"), so it has to be
  normalized before any later call can use it -- see normalize_kernel_ref().
- A new kernel's slug is derived from its TITLE, not from the slug the push asks for
  -- see push_kernel()'s docstring.

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
import io
import json
import urllib.error
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

BASE_URL = "https://api.kaggle.com/v1"
_TIMEOUT_SECONDS = 60.0

RequestFn = Callable[..., dict]


@dataclass(frozen=True)
class KaggleAccount:
    username: str
    api_key: str


@dataclass(frozen=True)
class GpuQuota:
    remaining_seconds: int
    refresh_at: str | None


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

# kagglesdk's DatabundleVersionStatus enum (datasets/types/dataset_enums.py), by number.
_DATABUNDLE_STATUS_BY_NUMBER = {
    0: "not_yet_persisted",
    1: "blobs_received",
    2: "blobs_decompressed",
    3: "blobs_copied_to_sds",
    4: "individual_blobs_compressed",
    5: "ready",
    6: "failed",
    7: "deleted",
    8: "reprocessing",
}

# Dataset states that mean "keep waiting" in the handler's wait-for-ready loop.
DATASET_PENDING_STATUSES = frozenset({
    "unknown", "not_yet_persisted", "blobs_received", "blobs_decompressed",
    "blobs_copied_to_sds", "individual_blobs_compressed", "reprocessing", "pending",
})
# States that mean the dataset will never become usable -- fail the job, do not loop.
DATASET_FAILED_STATUSES = frozenset({"failed", "deleted"})


def _auth_header(account: KaggleAccount) -> dict[str, str]:
    token = base64.b64encode(f"{account.username}:{account.api_key}".encode()).decode("ascii")
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


def _duration_seconds(value) -> int:
    """Decode protobuf Duration JSON emitted by either Kaggle gateway version."""
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        return int(float(value.removesuffix("s")))
    if isinstance(value, dict):
        return int(value.get("seconds") or 0)
    return 0


def gpu_quota(account: KaggleAccount, *, request: RequestFn = _request) -> GpuQuota:
    """Return Kaggle's authoritative GPU allowance for the current quota window."""
    data = _rpc(
        request, account, "kernels.KernelsApiService",
        "GetAcceleratorQuotaStatistics", {},
    )
    quota = data.get("gpuQuota") or data.get("gpu_quota") or {}
    total = _duration_seconds(quota.get("totalTimeAllowed") or quota.get("total_time_allowed"))
    if total <= 0:
        raise ValueError("Kaggle quota response omitted total GPU allowance")
    used = _duration_seconds(quota.get("timeUsed") or quota.get("time_used"))
    reserved = _duration_seconds(quota.get("timeReserved") or quota.get("time_reserved"))
    refresh_at = data.get("quotaRefreshTime") or data.get("quota_refresh_time")
    return GpuQuota(max(0, total - used - reserved), str(refresh_at) if refresh_at else None)


# Path segments that can sit in front of "username/slug" in a kernel URL, and so must
# never be mistaken for the username when taking the last two segments.
_REF_PATH_PREFIXES = {"code", "kernels", "notebooks"}


def normalize_kernel_ref(value: str | None, fallback: str = "") -> str:
    """Reduce whatever SaveKernel hands back to a bare "username/slug".

    Verified against a live account (2026-09): SaveKernel's `ref` is a URL PATH --
    "/code/yukihuy9999/epub-tts-batch-18" -- not the "username/slug" that the pull
    path's `ref` carries, so passing it straight to kernel_status() raised
    ValueError. Accepts a full URL, a "/code/..." path (optionally with a
    "/versions/N" suffix), or an already-bare ref; returns `fallback` when there is
    nothing usable left."""
    raw = (value or "").split("?")[0].split("#")[0]
    parts = [p for p in raw.split("/") if p and not p.endswith(":")]
    if len(parts) >= 2 and parts[-2] == "versions" and parts[-1].isdigit():
        parts = parts[:-2]
    if len(parts) >= 2 and parts[-2] not in _REF_PATH_PREFIXES:
        return f"{parts[-2]}/{parts[-1]}"
    return fallback


def _kernel_slug(kernel_ref: str) -> tuple[str, str]:
    username, _, slug = normalize_kernel_ref(kernel_ref).partition("/")
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
    kernel's ref ("username/slug").

    NOTE: for a kernel that does not exist yet, Kaggle derives the slug from the
    TITLE and ignores the slug asked for here when the two disagree -- kaggle-cli
    warns about exactly this ("Your kernel title does not resolve to the specified
    id", kaggle_api_extended.py). Callers must therefore pass a title that slugifies
    to metadata["id"]'s slug, or the next push (which does use that slug) is treated
    as a brand-new kernel and fails with HTTP 409 ALREADY_EXISTS on the title."""
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
    # Concrete GPU type (NvidiaTeslaT4, NvidiaTeslaP100, Tpu1VmV38). enableGpu only
    # says "a GPU" and Kaggle may hand out a P100 (sm_60), which the PyTorch in its
    # current image cannot execute on -- torch claims cuda.is_available() and then
    # dies with "no kernel image is available for execution on the device".
    if metadata.get("machine_shape"):
        body["machineShape"] = metadata["machine_shape"]
    data = _rpc(request, account, "kernels.KernelsApiService", "SaveKernel", body)
    if data.get("error"):
        raise RuntimeError(f"Kaggle kernel push failed: {data['error']}")
    # A rejected data source does NOT fail the push: the kernel is created and runs
    # anyway, just without the input (kaggle-cli only prints a warning here too).
    # Treating that as success strands the run -- Cell 4's kaggle_native branch finds
    # no batch_manifest.json under /kaggle/input and the kernel dies with an assert
    # (observed live, 2026-09-06) -- so fail loudly instead.
    bad = [s for s in (data.get("invalidDatasetSources") or []) if s] + [
        s for s in (data.get("invalidCompetitionSources") or []) if s
    ]
    if bad:
        raise RuntimeError(f"Kaggle rejected kernel data sources: {bad}")
    return normalize_kernel_ref(data.get("ref") or data.get("url"), fallback=metadata["id"])


def kernel_status(
    account: KaggleAccount, kernel_ref: str, *, request: RequestFn = _request,
) -> KernelStatus:
    username, slug = _kernel_slug(kernel_ref)
    data = _rpc(request, account, "kernels.KernelsApiService", "GetKernelSessionStatus", {
        "userName": username, "kernelSlug": slug,
    })
    raw = str(data.get("status", "")).strip()
    if not raw:
        # QUEUED is KernelWorkerStatus's zero value (kagglesdk kernels_enums.py), and
        # proto3 JSON omits zero-valued fields -- so a kernel that is merely queued can
        # answer with the field absent. Reading that as "unknown" and raising would
        # kill the job in the seconds right after a push.
        return KernelStatus.QUEUED
    mapped = _STATUS_MAP.get(raw)
    if mapped is None:
        raise RuntimeError(f"Unknown Kaggle kernel status: {raw!r}")
    return mapped


def kernel_output(
    account: KaggleAccount, kernel_ref: str, dest_dir: Path, *,
    version_label: str | None = None, request: RequestFn = _request,
) -> list[Path]:
    """Download the batch's result files into dest_dir (mirroring each entry's
    fileName as a relative path) and return the local paths written.

    Hidden paths (any segment starting with ".", e.g. the notebook's
    /kaggle/working/.cache with its GBs of HuggingFace/pip blobs) are skipped:
    downloading them stalls the worker past the reaper window and nothing in the
    import pipeline reads them -- it only needs result/*.wav (+ sidecars) and
    the locally-built patch manifests."""
    username, slug = _kernel_slug(kernel_ref)
    dest_dir = Path(dest_dir)
    entries = []
    page_token = ""
    seen_tokens = set()
    while True:
        body = {"userName": username, "kernelSlug": slug, "pageSize": 100}
        if page_token:
            body["pageToken"] = page_token
        if version_label:
            body["versionLabel"] = version_label
        data = _rpc(
            request, account, "kernels.KernelsApiService",
            "ListKernelSessionOutput", body,
        )
        entries.extend(data.get("files") or [])
        next_token = str(data.get("nextPageToken") or data.get("next_page_token") or "")
        if not next_token or next_token in seen_tokens:
            break
        seen_tokens.add(next_token)
        page_token = next_token

    written = []
    for entry in entries:
        name = str(entry.get("fileName") or "")
        if not name or any(part.startswith(".") for part in Path(name).parts):
            continue
        # Presigned download URLs need no Kaggle auth of their own.
        content = _call(request, entry["url"], method="GET", headers={})
        target = dest_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, (bytes, bytearray)) else content.encode("utf-8"))
        written.append(target)
    return written


def kernel_log(
    account: KaggleAccount, kernel_ref: str, *, request: RequestFn = _request,
) -> str:
    """Full session log of a kernel as plain text (stdout/stderr concatenated).

    ListKernelSessionOutput carries it in a `log` field as a JSON-encoded list of
    {stream_name, time, data} entries -- a separate field from `files`, so an
    ERROR kernel with no output files still has its traceback here. Returns ""
    when the session produced no log."""
    username, slug = _kernel_slug(kernel_ref)
    data = _rpc(request, account, "kernels.KernelsApiService", "ListKernelSessionOutput", {
        "userName": username, "kernelSlug": slug, "pageSize": 1,
    })
    raw = data.get("log") or ""
    try:
        entries = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return raw if isinstance(raw, str) else ""
    if not isinstance(entries, list):
        return ""
    return "".join(str(e.get("data") or "") for e in entries if isinstance(e, dict))


def cancel_kernel(
    account: KaggleAccount, kernel_ref: str, *, request: RequestFn = _request,
) -> None:
    """Best-effort: callers treat this as fire-and-forget and never see an exception
    from it. Currently a documented no-op -- CancelKernelSession needs a numeric
    kernel_session_id that no other call in this module surfaces (see module
    docstring's "Known gaps"). Safe to leave as-is: the caller stops polling and
    returns regardless of whether Kaggle itself was told to stop."""
    return


def _package_zip_bytes(package_dir: Path) -> bytes:
    """Zip the whole batch package into one archive, preserving the
    patches/patch_NNN/ hierarchy. Single-file upload is not just simpler -- it is
    the only correct shape: StartBlobUpload carries a bare basename and
    CreateDataset's files list carries tokens only, so per-file upload CANNOT
    transmit directory structure (every patch's manifest.json would land flat and
    overwrite the others). This mirrors kaggle-cli's --dir-mode zip for folders.
    The notebook itself is kernel code, not input data, so it stays out."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(package_dir.rglob("*")):
            if not path.is_file() or path.suffix == ".ipynb":
                continue
            zf.write(path, path.relative_to(package_dir).as_posix())
    return buf.getvalue()


def create_dataset(
    account: KaggleAccount, package_dir: Path, slug: str, title: str,
    *, request: RequestFn = _request,
) -> str:
    """Upload package_dir as ONE zip blob inside a new private Kaggle Dataset and
    return its ref ("username/slug"), for use in a kernel push's dataset_sources.
    Always creates a fresh dataset (see module docstring's "Known gaps" on why this
    does not version an existing one in place). The kernel unzips the archive into
    /kaggle/working (Cell 4's kaggle_native branch), so paths line up with what
    patch_import.resolve_batch_result expects from kernel_output()."""
    data = _package_zip_bytes(Path(package_dir))
    start = _rpc(request, account, "blobs.BlobApiService", "StartBlobUpload", {
        "type": "DATASET", "name": f"{slug}.zip", "contentLength": len(data),
    })
    # Presigned upload URL: a plain PUT of the raw bytes, no Kaggle auth header.
    # The PUT response carries no usable receipt, so the handler verifies with
    # dataset_files() after the dataset is ready instead of trusting this call.
    _call(request, start["createUrl"], method="PUT", headers={}, body=data)
    payload = _rpc(request, account, "datasets.DatasetApiService", "CreateDataset", {
        "ownerSlug": account.username,
        "slug": slug,
        "title": title,
        "licenseName": "CC0-1.0",
        "isPrivate": True,
        "files": [{"token": start["token"]}],
    })
    if payload.get("error"):
        raise RuntimeError(f"Kaggle dataset create failed: {payload['error']}")
    return f"{account.username}/{slug}"


def dataset_status(
    account: KaggleAccount, dataset_ref: str, *, request: RequestFn = _request,
) -> str:
    """Ready-state of a dataset ("ready", "pending", ...), lowercased. Mirrors
    kaggle-cli's dataset_status (GetDatasetStatus), whose wire value is the
    DatabundleVersionStatus enum name -- accept its numeric value too, in case the
    RPC layer serializes enums as ints."""
    username, _, slug = dataset_ref.partition("/")
    if not username or not slug:
        raise ValueError(f"dataset_ref must look like 'username/slug', got {dataset_ref!r}")
    data = _rpc(request, account, "datasets.DatasetApiService", "GetDatasetStatus", {
        "ownerSlug": username, "datasetSlug": slug,
    })
    raw = data.get("status", "")
    if isinstance(raw, int):
        return _DATABUNDLE_STATUS_BY_NUMBER.get(raw, f"unknown({raw})")
    return str(raw or "unknown").lower()


def dataset_files(
    account: KaggleAccount, dataset_ref: str, *, request: RequestFn = _request,
) -> list[dict]:
    """File listing of a dataset (name + size per entry). Used as the ground truth
    after upload: the blob PUT returns no receipt and CreateDataset reports
    success even when nothing lands, so the handler refuses to push a kernel for
    an empty dataset instead of discovering it one GPU session later in Cell 4."""
    username, _, slug = dataset_ref.partition("/")
    if not username or not slug:
        raise ValueError(f"dataset_ref must look like 'username/slug', got {dataset_ref!r}")
    data = _rpc(request, account, "datasets.DatasetApiService", "ListDatasetFiles", {
        "ownerSlug": username, "datasetSlug": slug, "pageSize": 100,
    })
    # Wire key is "datasetFiles" (kagglesdk's ApiListDatasetFilesResponse exposes
    # it to Python as `.files` / `.dataset_files`, which is why the raw-JSON
    # reader here must not look for "files"). Verified against kagglesdk 0.1.36:
    # FieldMetadata("datasetFiles", "dataset_files", ...). Reading "files"
    # returned [] forever, so every upload false-failed as "empty dataset"
    # even though the dataset was visible on kaggle.com (observed 2026-09-06).
    files = data.get("datasetFiles", data.get("files")) or []
    return [dict(f) for f in files if isinstance(f, dict)]
