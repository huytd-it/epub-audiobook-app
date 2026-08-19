"""Fetch the ZeroTTS weights into data/zerotts.

huggingface_hub's snapshot_download is unusable here: huggingface.co throttles a
single connection down to ~25 KB/s after a few dozen MB, and hub 1.x names its
`.incomplete` files per attempt, so a retry restarts the file from zero instead
of resuming. This pulls each file as parallel HTTP Range requests instead —
every chunk lands in its own `.part`, so an interrupted run resumes for free.

HF_TOKEN comes from .env via settings.hf_token (the same token the Colab export
already uses). The repo is public, so it only raises the rate limit; the
download works without one.

Usage:
    python scripts/download_zerotts.py             # fetch / resume
    python scripts/download_zerotts.py --verify    # only check what is on disk
    python scripts/download_zerotts.py --workers 8 # fewer connections
"""
import argparse
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402

REPO = "zeroweight-ai/ZeroTTS"
# Pinned so a repo update never half-replaces a local copy. Bump deliberately.
REVISION = "7fdb2342d1242fd84b738223281242e4f149825c"
BASE = f"https://huggingface.co/{REPO}/resolve/{REVISION}"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEST = PROJECT_ROOT / "data" / "zerotts"
CHUNK = 8 * 1024 * 1024
DEFAULT_WORKERS = 24
RETRIES = 15

_lock = threading.Lock()
_done = [0]


def read_token() -> str | None:
    """HF_TOKEN, from the environment first so a one-off run can override .env."""
    token = os.environ.get("HF_TOKEN") or settings.hf_token
    return token.strip() or None if token else None


def manifest(headers: dict) -> list[tuple[str, int]]:
    """(path, size) for every file in the repo at REVISION."""
    r = requests.get(
        f"https://huggingface.co/api/models/{REPO}/revision/{REVISION}",
        params={"blobs": "true"}, headers=headers, timeout=30,
    )
    r.raise_for_status()
    files = []
    for entry in r.json()["siblings"]:
        size = entry.get("size")
        if size is None:  # blobs=true fills this in; bail loudly if it did not
            raise RuntimeError(f"no size for {entry['rfilename']}")
        files.append((entry["rfilename"], size))
    return files


def fetch_chunk(path: str, idx: int, start: int, end: int,
                partdir: Path, headers: dict) -> None:
    part = partdir / f"{idx:05d}.part"
    want = end - start + 1
    if part.exists() and part.stat().st_size == want:
        with _lock:
            _done[0] += want
        return
    for attempt in range(RETRIES):
        try:
            r = requests.get(f"{BASE}/{path}", stream=True, timeout=(15, 30),
                             headers={**headers, "Range": f"bytes={start}-{end}"})
            r.raise_for_status()
            tmp = part.with_suffix(".part.tmp")
            written = 0
            with open(tmp, "wb") as fh:
                for block in r.iter_content(256 * 1024):
                    fh.write(block)
                    written += len(block)
            if written != want:
                raise IOError(f"short read {written}/{want}")
            os.replace(tmp, part)
            with _lock:
                _done[0] += want
            return
        except Exception:
            if attempt == RETRIES - 1:
                raise
            time.sleep(1.5)


def download(path: str, size: int, headers: dict, workers: int) -> None:
    out = DEST / path
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and out.stat().st_size == size:
        with _lock:
            _done[0] += size
        return

    if size <= CHUNK:
        for attempt in range(RETRIES):
            try:
                r = requests.get(f"{BASE}/{path}", headers=headers, timeout=(15, 60))
                r.raise_for_status()
                out.write_bytes(r.content)
                with _lock:
                    _done[0] += size
                return
            except Exception:
                if attempt == RETRIES - 1:
                    raise
                time.sleep(1.5)

    partdir = out.with_name(out.name + ".parts")
    partdir.mkdir(parents=True, exist_ok=True)
    ranges = [(i, s, min(s + CHUNK - 1, size - 1))
              for i, s in enumerate(range(0, size, CHUNK))]
    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(lambda a: fetch_chunk(path, *a, partdir, headers), ranges))

    with open(out, "wb") as dst:
        for idx, _, _ in ranges:
            with open(partdir / f"{idx:05d}.part", "rb") as src:
                while block := src.read(1 << 20):
                    dst.write(block)
    if out.stat().st_size != size:
        raise RuntimeError(f"assembled size mismatch for {path}")
    for idx, _, _ in ranges:
        (partdir / f"{idx:05d}.part").unlink()
    partdir.rmdir()


def verify(files: list[tuple[str, int]]) -> list[str]:
    return [p for p, size in files
            if not (DEST / p).exists() or (DEST / p).stat().st_size != size]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=DEFAULT_WORKERS,
                    help="parallel range requests per file (default 24; "
                         "a single connection gets throttled hard)")
    ap.add_argument("--verify", action="store_true",
                    help="report missing/truncated files and exit")
    args = ap.parse_args()

    token = read_token()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    print(f"token: {'yes' if token else 'no (anonymous, slower)'}")

    files = manifest(headers)
    total = sum(size for _, size in files)
    print(f"{len(files)} files, {total / 1e6:.1f} MB -> {DEST}")

    if args.verify:
        bad = verify(files)
        print(f"complete: {len(files) - len(bad)}/{len(files)}")
        for p in bad:
            print(f"  missing/truncated: {p}")
        return 1 if bad else 0

    DEST.mkdir(parents=True, exist_ok=True)
    started = time.time()
    stop = threading.Event()

    def report() -> None:
        while not stop.wait(15):
            elapsed = time.time() - started
            print(f"  {_done[0] / 1e6:7.1f} / {total / 1e6:.1f} MB "
                  f"({_done[0] / 1e6 / max(elapsed, 1):.2f} MB/s)", flush=True)

    threading.Thread(target=report, daemon=True).start()
    try:
        # Smallest first, so a flaky link still makes visible progress early.
        for path, size in sorted(files, key=lambda f: f[1]):
            download(path, size, headers, args.workers)
            print(f"OK  {path}", flush=True)
    finally:
        stop.set()

    bad = verify(files)
    if bad:
        print(f"FAILED: {len(bad)} file(s) incomplete; re-run to resume")
        return 1
    print(f"done: {total / 1e6:.1f} MB in {time.time() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
