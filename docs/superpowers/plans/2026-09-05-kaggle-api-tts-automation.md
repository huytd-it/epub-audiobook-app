# Kaggle API TTS Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Tự động hoá toàn bộ vòng đời "export → chạy trên Kaggle → import kết quả" bằng Kaggle Kernels API, thay thế hoàn toàn Google Drive cho mục tiêu Kaggle, xoay vòng nhiều tài khoản Kaggle để tăng thông lượng GPU/tuần.

**Architecture:** Một job type mới `kaggle_tts` trong `app/jobqueue` hiện có (không dựng queue riêng). Handler push kernel qua `app/kaggle_api.py` (HTTP thuần, không phụ thuộc gói `kaggle`), poll trạng thái, tải output, rồi tái dùng nguyên logic import batch package đã có (được tách ra `app/patch_import.py`) bằng cách viết output tải về vào một thư mục cục bộ có đúng hình dạng một batch package Drive. Tài khoản Kaggle và ledger quota GPU sống ở bảng riêng do `app/kaggle_accounts.py` quản lý trực tiếp (không qua `app/repository.py`, giống cách `app/google_drive.py` tự quản `drive_account`). Notebook `colab_kaggle_batch_tts_template.ipynb` được sửa tối thiểu: thêm 1 global `MODE` bên cạnh `IS_KAGGLE` sẵn có.

**Tech Stack:** Python 3.10–3.12, FastAPI, sqlite3, `app.jobqueue`, React + TypeScript (frontend), pytest.

**Spec:** `docs/superpowers/specs/2026-09-05-kaggle-api-tts-automation-design.md`

## Global Constraints

- Chạy test bằng `pytest tests/` — `pytest` trần lội vào `build/`/`.venv/` rồi chết trước khi chạy được test nào.
- Không thêm hàm mới vào `app/repository.py` (đã lớn) trừ 2 việc bắt buộc: thêm cột `kaggle_account_id`/`kaggle_kernel_ref` vào `patch_export` và mở rộng chữ ký `create_patch_export`/`list_patch_exports`/`list_all_patch_exports` cho 2 cột đó. Mọi thứ khác của Kaggle (account, usage ledger, claim/release) sống trong `app/kaggle_accounts.py`, tự làm SQL riêng như `app/google_drive.py` đã làm cho `drive_account`.
- `app/jobqueue/handlers/*` KHÔNG được import từ `app/routes/*` (route module có FastAPI-specific imports không nên bị kéo vào tiến trình worker). Vì vậy phải tách `app/patch_import.py` ra khỏi `app/routes/patches.py` trước khi viết handler `kaggle_tts`.
- Bí mật (Kaggle username/API key) lưu plaintext trong sqlite, giống hệt cách `drive_account`/`drive_oauth_client` đã lưu OAuth token — không thêm mã hoá mới, đây là app single-user cục bộ.
- `JobRescheduled` chỉ được raise bởi `kaggle_tts`; không thay đổi hành vi của bất kỳ job type nào khác.
- Test notebook hiện có (`tests/test_notebook_templates.py`) đã pin `IS_KAGGLE = False` là dòng đầu cell 1 và cấm mọi auto-detect platform — giữ nguyên global đó, chỉ thêm `MODE` bên cạnh.
- Tiếng Việt trong log/UI giữ nguyên phong cách hiện có của repo; code/định danh bằng tiếng Anh.

## File Structure

| File | Trách nhiệm |
|---|---|
| `app/db.py` (sửa) | DDL `kaggle_account`, `kaggle_usage`; migrate 2 cột mới của `patch_export` |
| `app/kaggle_accounts.py` (mới) | CRUD account, claim/release nguyên tử, usage ledger, ước tính quota |
| `app/kaggle_api.py` (mới) | HTTP client thuần cho Kaggle REST API: push/status/output/cancel |
| `app/jobqueue/models.py` (sửa) | thêm `JobRescheduled` |
| `app/jobqueue/store.py` (sửa) | thêm `reschedule()` |
| `app/jobqueue/runner.py` (sửa) | bắt `JobRescheduled` trong `_execute` |
| `app/patch_import.py` (mới, tách từ `app/routes/patches.py`) | `resolve_batch_result`, `build_import_timeline`, `timeline_metadata`, `install_imported_wav`, `safe_batch_path` (pure helpers only — xem ghi chú "Scope thực tế" ở Task 5, không có `import_batch_patch()` orchestrator chung) |
| `app/jobqueue/handlers/kaggle_tts.py` (mới) | handler chính: vòng lặp claim account → push → poll → import → xoay tài khoản |
| `app/jobqueue/backfill.py` (sửa) | `queue.register("kaggle_tts", kaggle_tts.handle, cancellable=True)` |
| `app/drive_export.py` (sửa) | `build_kaggle_export_package()`, tham số `mode` nội bộ cho `build_batch_export_package` |
| `app/assets/colab_kaggle_batch_tts_template.ipynb` (sửa) | thêm global `MODE`, nhánh input/output |
| `app/config.py` (sửa) | setting mới cho Kaggle |
| `app/routes/kaggle.py` (mới) | CRUD account + `/api/ui/kaggle` |
| `app/routes/patches.py` (sửa) | endpoint `export-batch-kaggle`; dùng `app/patch_import.py` thay code inline |
| `app/main.py` (sửa) | `include_router(kaggle.router)` |
| `.env.example` (sửa) | ví dụ setting Kaggle |
| `frontend/src/api.ts` (sửa) | type `KaggleAccount`, hàm gọi API |
| `frontend/src/pages/DrivePage.tsx` hoặc trang mới (sửa/mới) | quản lý account Kaggle |
| `frontend/src/pages/book-detail/ExportPanel.tsx` (sửa) | nút "Kaggle (tự động)" |
| `tests/test_kaggle_accounts.py` (mới) | claim atomic, cooldown, quota math |
| `tests/test_kaggle_api.py` (mới) | client (mock HTTP) |
| `tests/test_jobqueue_reschedule.py` (mới) | `store.reschedule`, fencing |
| `tests/test_patch_import.py` (mới, di dời test từ import hiện có) | logic import tách riêng |
| `tests/test_kaggle_tts_handler.py` (mới) | handler end-to-end với fake `kaggle_api` |
| `tests/test_notebook_templates.py` (sửa) | assert cell `MODE` |

---

### Task 1: Schema — `kaggle_account`, `kaggle_usage`, cột mới trên `patch_export`

**Files:**
- Modify: `app/db.py`
- Test: `tests/test_kaggle_schema.py`

**Interfaces:**
- Produces: bảng `kaggle_account` (id, label, username, api_key, status, cooldown_until, in_use_by_job_id, created_at, updated_at) + unique index trên `username`; bảng `kaggle_usage` (id, account_id, kernel_ref, started_at, finished_at, gpu_seconds, created_at) + index trên `(account_id, started_at DESC)`; `patch_export.kaggle_account_id`, `patch_export.kaggle_kernel_ref`.

- [x] **Step 1: Viết test thất bại**

```python
"""Schema kaggle_account/kaggle_usage + 2 cột mới trên patch_export."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app import db


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def test_kaggle_account_table_has_expected_columns():
    conn = _conn()
    names = {r["name"] for r in conn.execute("PRAGMA table_info(kaggle_account)")}
    assert names == {
        "id", "label", "username", "api_key", "status", "cooldown_until",
        "in_use_by_job_id", "created_at", "updated_at",
    }


def test_kaggle_account_defaults_to_idle():
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO kaggle_account (label, username, api_key, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("acc1", "user1", "key1", now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM kaggle_account WHERE id=?", (cur.lastrowid,)).fetchone()
    assert row["status"] == "idle"
    assert row["in_use_by_job_id"] is None


def test_kaggle_account_username_is_unique():
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO kaggle_account (label, username, api_key, created_at, updated_at) "
        "VALUES ('a', 'dup', 'k1', ?, ?)", (now, now),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO kaggle_account (label, username, api_key, created_at, updated_at) "
            "VALUES ('b', 'dup', 'k2', ?, ?)", (now, now),
        )


def test_kaggle_usage_table_has_expected_columns():
    conn = _conn()
    names = {r["name"] for r in conn.execute("PRAGMA table_info(kaggle_usage)")}
    assert names == {
        "id", "account_id", "kernel_ref", "started_at", "finished_at",
        "gpu_seconds", "created_at",
    }


def test_patch_export_has_kaggle_columns():
    conn = _conn()
    names = {r["name"] for r in conn.execute("PRAGMA table_info(patch_export)")}
    assert {"kaggle_account_id", "kaggle_kernel_ref"} <= names
```

- [x] **Step 2: Chạy test, xác nhận fail**

```bash
pytest tests/test_kaggle_schema.py -v
```

Kỳ vọng: FAIL với `sqlite3.OperationalError: no such table: kaggle_account`.

- [x] **Step 3: Thêm DDL vào `_SCHEMA` trong `app/db.py`**

```sql
CREATE TABLE IF NOT EXISTS kaggle_account (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    label            TEXT NOT NULL,
    username         TEXT NOT NULL,
    api_key          TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'idle',
    cooldown_until   TEXT,
    in_use_by_job_id INTEGER,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_kaggle_account_username ON kaggle_account(username);

CREATE TABLE IF NOT EXISTS kaggle_usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL REFERENCES kaggle_account(id) ON DELETE CASCADE,
    kernel_ref   TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    gpu_seconds  INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kaggle_usage_account ON kaggle_usage(account_id, started_at DESC);
```

- [x] **Step 4: Thêm migrate 2 cột mới của `patch_export` trong `_migrate()`**

Theo đúng pattern đã có (`PRAGMA table_info` rồi `ALTER TABLE ADD COLUMN` nếu thiếu — xem khối tương tự cho `book`/`patch` trong `_migrate()`):

```python
export_existing = {row["name"] for row in conn.execute("PRAGMA table_info(patch_export)")}
if "kaggle_account_id" not in export_existing:
    conn.execute("ALTER TABLE patch_export ADD COLUMN kaggle_account_id INTEGER")
if "kaggle_kernel_ref" not in export_existing:
    conn.execute("ALTER TABLE patch_export ADD COLUMN kaggle_kernel_ref TEXT")
```

- [x] **Step 5: Chạy test, xác nhận pass**

```bash
pytest tests/test_kaggle_schema.py -v
```

- [x] **Step 6: Chạy cả suite**

```bash
pytest tests/ -q
```

Kỳ vọng: không có failure mới so với trước Task 1.

- [x] **Step 7: Commit**

```bash
git add app/db.py tests/test_kaggle_schema.py
git commit -m "feat(kaggle): add kaggle_account/kaggle_usage tables and patch_export columns"
```

---

### Task 2: `app/kaggle_accounts.py` — CRUD, claim/release nguyên tử, quota

**Files:**
- Create: `app/kaggle_accounts.py`
- Test: `tests/test_kaggle_accounts.py`

**Interfaces:**
- Consumes: bảng `kaggle_account`/`kaggle_usage` (Task 1), `settings.kaggle_weekly_gpu_quota_hours`
- Produces:

```python
def create_account(conn, label: str, username: str, api_key: str) -> int
def list_accounts(conn) -> list[dict]          # bao gồm remaining_quota_seconds tính sẵn
def get_account(conn, account_id: int) -> dict | None
def update_account(conn, account_id: int, *, label: str, username: str, api_key: str = "") -> None
def set_disabled(conn, account_id: int, disabled: bool) -> None
def delete_account(conn, account_id: int) -> bool   # False nếu đang in_use_by_job_id
def claim_idle_account(conn, job_id: int) -> dict | None
def release_account(conn, account_id: int, *, cooldown_until: str | None = None) -> None
def remaining_quota_seconds(conn, account_id: int) -> int
def record_usage_start(conn, account_id: int, kernel_ref: str) -> int
def record_usage_finish(conn, usage_id: int, gpu_seconds: int) -> None
def earliest_quota_reset(conn) -> str | None
```

- [x] **Step 1: Viết test thất bại**

```python
"""app.kaggle_accounts: CRUD, claim nguyên tử, cooldown tự hồi phục, quota 7 ngày."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

from app import db, kaggle_accounts as ka


def _conn(tmp_path=None):
    conn = db.connect(str(tmp_path / "app.db") if tmp_path else ":memory:")
    db.init_schema(conn)
    return conn


def _iso(delta_seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


def test_create_and_list_account():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    accounts = ka.list_accounts(conn)
    assert len(accounts) == 1
    assert accounts[0]["id"] == account_id
    assert accounts[0]["status"] == "idle"


def test_claim_moves_account_to_busy_and_stamps_job_id():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    claimed = ka.claim_idle_account(conn, job_id=42)
    assert claimed["id"] == account_id
    assert claimed["status"] == "busy"
    assert ka.get_account(conn, account_id)["in_use_by_job_id"] == 42


def test_claim_skips_busy_and_disabled_accounts():
    conn = _conn()
    ka.create_account(conn, "acc1", "user1", "key1")
    ka.claim_idle_account(conn, job_id=1)  # now busy
    second = ka.create_account(conn, "acc2", "user2", "key2")
    ka.set_disabled(conn, second, True)
    assert ka.claim_idle_account(conn, job_id=2) is None


def test_claim_self_heals_an_expired_cooldown():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.release_account(conn, account_id, cooldown_until=_iso(-10))
    claimed = ka.claim_idle_account(conn, job_id=5)
    assert claimed["id"] == account_id


def test_claim_respects_a_future_cooldown():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.release_account(conn, account_id, cooldown_until=_iso(3600))
    assert ka.claim_idle_account(conn, job_id=5) is None


def test_claim_is_atomic_across_threads(tmp_path):
    conn = _conn(tmp_path)
    ka.create_account(conn, "acc1", "user1", "key1")
    conn.close()

    claimed = []
    lock = threading.Lock()
    start = threading.Barrier(10)

    def worker(n):
        c = db.connect(str(tmp_path / "app.db"))
        start.wait()
        account = ka.claim_idle_account(c, job_id=n)
        if account is not None:
            with lock:
                claimed.append(account["id"])
        c.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 1


def test_delete_refuses_an_account_in_use():
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.claim_idle_account(conn, job_id=1)
    assert ka.delete_account(conn, account_id) is False
    ka.release_account(conn, account_id)
    assert ka.delete_account(conn, account_id) is True


def test_remaining_quota_subtracts_last_7_days(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "kaggle_weekly_gpu_quota_hours", 10)
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    usage_id = ka.record_usage_start(conn, account_id, "user1/slug")
    ka.record_usage_finish(conn, usage_id, gpu_seconds=3600 * 4)
    assert ka.remaining_quota_seconds(conn, account_id) == 3600 * 6


def test_remaining_quota_ignores_usage_older_than_7_days(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "kaggle_weekly_gpu_quota_hours", 10)
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    old = _iso(-8 * 24 * 3600)
    conn.execute(
        "INSERT INTO kaggle_usage (account_id, kernel_ref, started_at, finished_at, gpu_seconds, created_at) "
        "VALUES (?, 'user1/slug', ?, ?, ?, ?)",
        (account_id, old, old, 3600 * 9, _iso()),
    )
    conn.commit()
    assert ka.remaining_quota_seconds(conn, account_id) == 3600 * 10


def test_remaining_quota_never_negative(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "kaggle_weekly_gpu_quota_hours", 1)
    conn = _conn()
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    usage_id = ka.record_usage_start(conn, account_id, "user1/slug")
    ka.record_usage_finish(conn, usage_id, gpu_seconds=3600 * 5)
    assert ka.remaining_quota_seconds(conn, account_id) == 0


def test_earliest_quota_reset_is_none_with_no_usage():
    conn = _conn()
    ka.create_account(conn, "acc1", "user1", "key1")
    assert ka.earliest_quota_reset(conn) is None
```

- [x] **Step 2: Chạy test, xác nhận fail**

```bash
pytest tests/test_kaggle_accounts.py -v
```

Kỳ vọng: FAIL với `ModuleNotFoundError: No module named 'app.kaggle_accounts'`.

- [x] **Step 3: Viết `app/kaggle_accounts.py`**

Điểm quan trọng khi implement:
- `claim_idle_account` dùng đúng 1 câu `UPDATE ... WHERE id = (SELECT id FROM kaggle_account WHERE status='idle' OR (status='cooldown' AND cooldown_until<=?) ORDER BY updated_at ASC LIMIT 1) AND (...) RETURNING *`, giống nguyên lý `jobqueue/store.py::claim`.
- `release_account(..., cooldown_until=None)` set `status='idle'` khi không truyền `cooldown_until`, ngược lại `status='cooldown'`.
- `remaining_quota_seconds` = `weekly_quota_seconds - SUM(gpu_seconds WHERE account_id=? AND started_at >= now-7days)`, clamp về 0. Đọc `settings.kaggle_weekly_gpu_quota_hours` **tại thời điểm gọi** (không cache ở import time) để test `monkeypatch.setattr(settings, ...)` có tác dụng.
- `earliest_quota_reset` = `MIN(started_at) + 7 days` trong số các dòng usage đang tính vào quota (7 ngày gần nhất), qua mọi account; trả `None` nếu không có usage nào.
- `delete_account` trả `False` (không raise) khi `in_use_by_job_id IS NOT NULL` — route sẽ tự map thành HTTP 400.

- [x] **Step 4: Chạy test, xác nhận pass**

```bash
pytest tests/test_kaggle_accounts.py -v
```

- [x] **Step 5: Commit**

```bash
git add app/kaggle_accounts.py tests/test_kaggle_accounts.py
git commit -m "feat(kaggle): add kaggle_accounts module with atomic claim and quota ledger"
```

---

### Task 3: `app/kaggle_api.py` — HTTP client cho Kaggle REST API

**Files:**
- Create: `app/kaggle_api.py`
- Test: `tests/test_kaggle_api.py`

**Interfaces:**

```python
@dataclass
class KaggleAccount:
    username: str
    api_key: str

class KernelStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    ERROR = "error"
    CANCELLED = "cancelled"

def push_kernel(account: KaggleAccount, package_dir: Path, metadata: dict, *, request=_request) -> str
def kernel_status(account: KaggleAccount, kernel_ref: str, *, request=_request) -> KernelStatus
def kernel_output(account: KaggleAccount, kernel_ref: str, dest_dir: Path, *, request=_request) -> list[Path]
def cancel_kernel(account: KaggleAccount, kernel_ref: str, *, request=_request) -> None
```

Ghi chú implementation: `_request` là 1 hàm nội bộ bọc `urllib.request` giống hệt cách `app/tts_api_providers.py::_request` đã làm — **inject được qua tham số `request=` cho mỗi hàm public**, để test không cần mock `urllib` toàn cục mà truyền thẳng 1 fake callable. Auth header cô lập trong đúng 1 hàm `_auth_header(account)` (xem mục "Implementation-time verification required" trong spec — đây là chỗ duy nhất cần sửa nếu Kaggle đổi scheme auth).

- [x] **Step 1: Viết test thất bại**

```python
"""app.kaggle_api: client HTTP thuần cho Kaggle REST API, request được inject để test
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


def test_cancel_kernel_swallows_errors():
    fake = FakeRequest([{"status": 500, "body": "boom"}])
    cancel_kernel(ACCOUNT, "user1/x", request=fake)  # phải không raise
```

- [x] **Step 2: Chạy test, xác nhận fail**

```bash
pytest tests/test_kaggle_api.py -v
```

- [x] **Step 3: Viết `app/kaggle_api.py`**

Base URL `https://www.kaggle.com/api/v1`. `push_kernel` ghi `kernel-metadata.json` + nội dung notebook (đọc từ `package_dir`) vào request tới `/kernels/push`. `kernel_output` gọi `/kernels/output` lấy danh sách file rồi tải từng file (URL trả về trong response) vào `dest_dir`, tạo thư mục con theo `fileName`. `cancel_kernel` gọi `/kernels/{ref}/cancel` hoặc endpoint tương đương, bọc try/except quanh mọi lỗi (best-effort, không raise). Đây là chỗ cần đối chiếu tài liệu Kaggle API hiện hành (xem mục cảnh báo trong spec) trước khi implement — task này KHÔNG bị chặn bởi việc đó vì `FakeRequest` không phụ thuộc hình dạng thật của Kaggle, chỉ cần tự nhất quán nội bộ; việc đối chiếu API thật là một bước riêng trước khi dùng thật (xem Task 12).

- [x] **Step 4: Chạy test, xác nhận pass**

```bash
pytest tests/test_kaggle_api.py -v
```

- [x] **Step 5: Commit**

```bash
git add app/kaggle_api.py tests/test_kaggle_api.py
git commit -m "feat(kaggle): add raw HTTP client for Kaggle kernels API"
```

---

### Task 4: `JobRescheduled` — primitive mới trong jobqueue

**Files:**
- Modify: `app/jobqueue/models.py`, `app/jobqueue/store.py`, `app/jobqueue/runner.py`
- Test: `tests/test_jobqueue_reschedule.py`

**Interfaces:**

```python
class JobRescheduled(Exception):
    def __init__(self, next_retry_at: str, message: str | None = None): ...

def store.reschedule(conn, job_id: int, next_retry_at: str, message: str | None = None,
                      *, worker_id: str | None = None) -> bool
```

- [x] **Step 1: Viết test thất bại**

```python
"""store.reschedule: đưa job về pending tại thời điểm chỉ định, không đụng attempt_count;
runner bắt JobRescheduled thay vì coi là lỗi."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app import db
from app.jobqueue import store
from app.jobqueue.models import PENDING


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _future(seconds=3600):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_reschedule_returns_job_to_pending_without_touching_attempt_count():
    conn = _conn()
    job_id = store.enqueue(conn, "kaggle_tts")
    store.claim(conn, "kaggle_tts", "w")
    target = _future()
    assert store.reschedule(conn, job_id, target, "no quota") is True
    job = store.get(conn, job_id)
    assert job.status == PENDING
    assert job.attempt_count == 1          # không reset, không tăng thêm
    assert job.next_retry_at == target
    assert job.error_message == "no quota"


def test_reschedule_is_fenced_like_finish_and_fail():
    conn = _conn()
    job_id = store.enqueue(conn, "kaggle_tts")
    a = store.claim(conn, "kaggle_tts", "kaggle_tts#A")
    from datetime import timedelta as _td
    conn.execute("UPDATE job SET heartbeat_at=? WHERE id=?",
                 ((datetime.now(timezone.utc) - _td(hours=1)).isoformat(), job_id))
    conn.commit()
    store.reap_stale(conn, older_than_seconds=120)
    store.claim(conn, "kaggle_tts", "kaggle_tts#B")
    assert store.reschedule(conn, job_id, _future(), worker_id=a.worker_id) is False


def test_claim_skips_a_rescheduled_job_before_its_time():
    conn = _conn()
    job_id = store.enqueue(conn, "kaggle_tts")
    store.claim(conn, "kaggle_tts", "w")
    store.reschedule(conn, job_id, _future(), worker_id="w")
    assert store.claim(conn, "kaggle_tts", "w2") is None
```

- [x] **Step 2: Chạy test, xác nhận fail**

```bash
pytest tests/test_jobqueue_reschedule.py -v
```

- [x] **Step 3: Thêm `JobRescheduled` vào `app/jobqueue/models.py`**

```python
class JobRescheduled(Exception):
    """Job không lỗi và không xong — nó đang chờ một tài nguyên bên ngoài (quota GPU
    Kaggle) hồi phục tại một thời điểm biết trước. Khác JobFatalError/retry thường:
    không tiêu attempt_count, không dùng công thức backoff 600s-cap."""
    def __init__(self, next_retry_at: str, message: str | None = None):
        super().__init__(message or f"rescheduled until {next_retry_at}")
        self.next_retry_at = next_retry_at
        self.message = message
```

- [x] **Step 4: Thêm `reschedule()` vào `app/jobqueue/store.py`**

```python
def reschedule(
    conn: sqlite3.Connection, job_id: int, next_retry_at: str,
    message: str | None = None, *, worker_id: str | None = None,
) -> bool:
    now = _now()
    guard, extra = _fence(worker_id)
    cur = conn.execute(
        f"""UPDATE job SET status='pending', next_retry_at=?, error_message=?,
                           worker_id=NULL, updated_at=? WHERE id=?{guard}""",
        [next_retry_at, message, now, job_id] + extra,
    )
    conn.commit()
    return cur.rowcount > 0
```

- [x] **Step 5: Bắt `JobRescheduled` trong `app/jobqueue/runner.py::_execute`**

Thêm nhánh **trước** `except Exception as exc:` hiện có:

```python
        except JobRescheduled as exc:
            ctx.log(f"Rescheduled: {exc.message or exc}", level=logging.INFO)
            ctx.flush(); store.reschedule(
                conn, job.id, exc.next_retry_at, exc.message, worker_id=job.worker_id,
            )
```

Và import `JobRescheduled` cùng dòng với `HandlerSpec, JobFatalError` ở đầu file.

- [x] **Step 6: Chạy test, xác nhận pass**

```bash
pytest tests/test_jobqueue_reschedule.py -v
```

- [x] **Step 7: Chạy cả suite jobqueue để chắc không phá gì**

```bash
pytest tests/test_jobqueue_store.py tests/test_jobqueue_models.py tests/ -k jobqueue -q
```

- [x] **Step 8: Commit**

```bash
git add app/jobqueue/models.py app/jobqueue/store.py app/jobqueue/runner.py tests/test_jobqueue_reschedule.py
git commit -m "feat(queue): add JobRescheduled for jobs blocked on an external resource"
```

---

### Task 5: Tách `app/patch_import.py` khỏi `app/routes/patches.py`

**Files:**
- Create: `app/patch_import.py`
- Modify: `app/routes/patches.py` (gọi module mới thay vì code inline)
- Test: `tests/test_patch_import.py` (di dời assertion liên quan từ test hiện có nếu trùng)

**Interfaces:**

```python
def safe_batch_path(batch_root: Path, relative: str) -> Path | None
def resolve_batch_result(patch_folder: Path, patch_id: int) -> Path | None
def build_import_timeline(chunk_paths: list[Path], metadata: list[dict], pause_ms: int) -> dict | None
def timeline_metadata(manifest: dict) -> list[dict]
def install_imported_wav(source: Path, audio_path: Path, timeline: dict | None = None) -> None
```

**Scope thực tế (đã điều chỉnh khi implement — DONE):** chỉ 5 pure helper trên (+ `_atomic_copy` private) di chuyển sang `app/patch_import.py`. Bản kế hoạch gốc còn định viết thêm một `import_batch_patch()`/`ImportOutcome` bọc toàn bộ orchestration của `import_patch_from_drive` (locked_conn, `_warm_thumbnail(request, ...)`, `repository.update_patch_export`, `RedirectResponse`...) — khi bắt tay vào mới thấy phần đó gắn chặt vào FastAPI request/response và transaction của route, ép nó thành một hàm dùng chung cho cả route lẫn job handler sẽ tạo ra chỗ vòng vo không cần thiết mà lợi ích không tương xứng. Quyết định: **bỏ `import_batch_patch()`**, giữ route `import_patch_from_drive` với orchestration inline như cũ (chỉ gọi 5 hàm pure qua `patch_import.*` thay vì hàm private nội bộ). Task 8 (`kaggle_tts` handler) sẽ tự viết orchestration của nó, gọi thẳng `patch_import.resolve_batch_result`/`patch_import.install_imported_wav` cộng với `repository`/`on_patch_audio_ready` riêng — xem ghi chú trong Task 8.

**Đây là refactor thuần** — hành vi phải giữ nguyên 100%. Không viết lại thuật toán, chỉ di chuyển.

- [x] **Step 1: Viết test cho hành vi hiện có (bắt trước khi di chuyển) — chạy trên code hiện tại để xác nhận PASS ngay, làm bằng chứng "không đổi hành vi"**

Copy các test case liên quan từ `tests/test_export_reference_required.py` và bất kỳ test hiện có nào gọi `_resolve_batch_result`/`_install_imported_wav`/`_build_import_timeline` (grep `_install_imported_wav\|_resolve_batch_result\|_build_import_timeline` trong `tests/`) sang `tests/test_patch_import.py`, đổi import từ `app.routes.patches` sang `app.patch_import` với tên public mới (bỏ dấu gạch dưới đầu). Giữ nguyên input/expected của từng case.

- [x] **Step 2: Chạy test mới trỏ vào `app.patch_import`, xác nhận fail vì module chưa tồn tại**

```bash
pytest tests/test_patch_import.py -v
```

Kỳ vọng: FAIL với `ModuleNotFoundError: No module named 'app.patch_import'`.

- [x] **Step 3: Tạo `app/patch_import.py`** — di chuyển nguyên văn `_safe_batch_path`, `_resolve_batch_result`, `_build_import_timeline`, `_timeline_metadata`, `_atomic_copy`, `_install_imported_wav` từ `app/routes/patches.py` sang đây, đổi tên bỏ dấu gạch dưới đầu cho 5 hàm public (giữ `_atomic_copy` private). Không viết `import_batch_patch()` — xem "Scope thực tế" ở trên.

- [x] **Step 4: Sửa `app/routes/patches.py` để gọi module mới** — mọi lời gọi `_safe_batch_path`/`_resolve_batch_result`/`_build_import_timeline`/`_timeline_metadata`/`_install_imported_wav` (trong `import_patch_from_drive` và 2 chỗ khác) đổi sang `patch_import.<tên không gạch dưới>`, orchestration của route giữ nguyên 100% (locked_conn, thumbnail warming, `update_patch_export`, `RedirectResponse` không đổi). Xoá 6 hàm private đã di chuyển khỏi `patches.py`.

- [x] **Step 5: Chạy toàn bộ test liên quan, xác nhận pass và không có regression**

```bash
pytest tests/test_patch_import.py tests/test_export_reference_required.py tests/ -k "export or import" -q
```

- [x] **Step 6: Chạy cả suite**

```bash
pytest tests/ -q
```

Kỳ vọng: không có failure mới so với trước Task 5.

- [x] **Step 7: Commit**

```bash
git add app/patch_import.py app/routes/patches.py tests/test_patch_import.py
git commit -m "refactor(patches): extract batch-import helpers into app/patch_import.py"
```

---

### Task 6: Notebook — thêm global `MODE` bên cạnh `IS_KAGGLE`

**Files:**
- Modify: `app/assets/colab_kaggle_batch_tts_template.ipynb`
- Modify: `tests/test_notebook_templates.py`

**Interfaces:**
- Cell 1 có thêm `MODE = "drive"` (giá trị mặc định trong file gốc; export builder sẽ thay bằng `"kaggle_native"` khi cần, xem Task 7).
- Mọi cell mount Drive / đọc `GDRIVE_CREDS` được bọc thêm điều kiện `MODE == "drive"` (kết hợp với `IS_KAGGLE` đã có).
- Cell đọc input / ghi output dùng `INPUT_ROOT`/`OUTPUT_ROOT` tính theo `MODE`.

- [x] **Step 1: Viết test thất bại**

Thêm vào `tests/test_notebook_templates.py`:

```python
def test_mode_is_a_manual_global_set_in_cell_1():
    for template in TEMPLATES:
        cells = _code_cells(template)
        assert 'MODE = "drive"' in cells[0], (
            f"{template.name}: cell 1 phải khai báo MODE = \"drive\" mặc định"
        )


def test_kaggle_native_mode_never_touches_drive_only_symbols():
    """Khi MODE == 'kaggle_native', không cell nào được gọi mount()/GDRIVE_CREDS trực
    tiếp ngoài nhánh if MODE == 'drive': đã có."""
    for template in TEMPLATES:
        for cell in _code_cells(template):
            if "GDRIVE_CREDS" in cell or "drive.mount" in cell:
                assert 'if MODE == "drive"' in cell or "MODE == 'drive'" in cell, (
                    f"{template.name}: cell tham chiếu Drive phải được bọc bởi "
                    "MODE == 'drive'"
                )


def test_input_and_output_roots_branch_on_mode():
    for template in TEMPLATES:
        cells = _code_cells(template)
        joined = "\n".join(cells)
        assert "INPUT_ROOT" in joined and "OUTPUT_ROOT" in joined
        assert '"kaggle_native"' in joined
```

- [x] **Step 2: Chạy test, xác nhận fail**

```bash
pytest tests/test_notebook_templates.py -v
```

- [x] **Step 3: Sửa notebook**

Mở `app/assets/colab_kaggle_batch_tts_template.ipynb`, cell 1 (nơi `IS_KAGGLE = False` đang sống) thêm ngay dòng kế:

```python
MODE = "drive"  # "drive" | "kaggle_native" -- chỉ có ý nghĩa khi IS_KAGGLE=True;
                # Colab luôn ứng xử như "drive" vì không có push/pull API tương đương
```

Mọi cell hiện có điều kiện Drive-only (tìm bằng `IS_KAGGLE` guard + `GDRIVE_CREDS`/`drive.mount`) đổi từ `if IS_KAGGLE: ...` (hoặc tương đương) sang thêm điều kiện `and MODE == "drive"` ở đúng những nhánh làm việc với Drive — **không đổi nhánh xử lý Colab-vs-Kaggle khác** (ví dụ import `google.colab` guard vẫn chỉ dựa vào `IS_KAGGLE`).

Thêm 1 cell mới (hoặc chèn vào cell đọc manifest hiện có) resolve root:

```python
if MODE == "kaggle_native":
    INPUT_ROOT = Path("/kaggle/input/epub-tts-batch")
    OUTPUT_ROOT = Path("/kaggle/working")
else:
    INPUT_ROOT = DRIVE_BATCH_FOLDER
    OUTPUT_ROOT = DRIVE_BATCH_FOLDER
```

Cell 8 (chunk/merge/pause/timeline) đổi mọi đường dẫn đang đọc/ghi trực tiếp `DRIVE_BATCH_FOLDER` sang `INPUT_ROOT`/`OUTPUT_ROOT` tương ứng (đọc manifest từ `INPUT_ROOT`, ghi `result/` vào `OUTPUT_ROOT`) — không đổi logic chunk/merge/pause/timeline/SKIP_EXISTING bên trong.

- [x] **Step 4: Chạy test, xác nhận pass**

```bash
pytest tests/test_notebook_templates.py -v
```

- [x] **Step 5: Xác nhận notebook vẫn là JSON hợp lệ và mở được**

```bash
python -c "import json; json.load(open('app/assets/colab_kaggle_batch_tts_template.ipynb', encoding='utf-8'))"
```

- [x] **Step 6: Commit**

```bash
git add app/assets/colab_kaggle_batch_tts_template.ipynb tests/test_notebook_templates.py
git commit -m "feat(notebook): add MODE flag for Kaggle-native input/output alongside Drive"
```

---

### Task 7: `app/drive_export.py` — `build_kaggle_export_package()`

**Files:**
- Modify: `app/drive_export.py`
- Test: `tests/test_kaggle_export_package.py`

**Interfaces:**

```python
def build_batch_export_package(..., mode: str = "drive", ...) -> tuple[Path, dict]   # tham số mode mới, mặc định giữ hành vi cũ
def build_kaggle_export_package(conn, patches, *, model_id, voice_id=None, max_chars=0,
                                 with_effects=False, hf_token=None) -> tuple[Path, dict]
```

- [x] **Step 1: Viết test thất bại**

```python
"""build_kaggle_export_package: package không có gdrive_creds/GDRIVE_CREDS, notebook
đặt MODE=kaggle_native, các file cần thiết (manifest, reference) vẫn đầy đủ như package
Drive."""
from __future__ import annotations

import json

import pytest

from app import drive_export


def test_kaggle_package_sets_mode_kaggle_native(tmp_path, conn_with_book_and_patches):
    conn, patches = conn_with_book_and_patches
    package_dir, manifest = drive_export.build_kaggle_export_package(
        conn, patches, model_id="voxcpm2",
    )
    notebook = (package_dir / "colab_kaggle_batch_tts_template.ipynb").read_text(encoding="utf-8")
    assert 'MODE = "kaggle_native"' in notebook or '\\"kaggle_native\\"' in notebook


def test_kaggle_package_never_bakes_a_gdrive_secret(tmp_path, conn_with_book_and_patches):
    conn, patches = conn_with_book_and_patches
    package_dir, _ = drive_export.build_kaggle_export_package(conn, patches, model_id="voxcpm2")
    notebook = (package_dir / "colab_kaggle_batch_tts_template.ipynb").read_text(encoding="utf-8")
    assert "GDRIVE_CREDS" not in notebook or notebook.count('"__GDRIVE_CREDS__"') == 0


def test_kaggle_package_still_writes_manifest_and_reference(tmp_path, conn_with_book_and_patches):
    conn, patches = conn_with_book_and_patches
    package_dir, manifest = drive_export.build_kaggle_export_package(conn, patches, model_id="voxcpm2")
    assert (package_dir / "batch_manifest.json").is_file()
    assert manifest["patch_count"] == len(patches)
    assert (package_dir / manifest["reference_wav"]).is_file()


def test_drive_package_still_defaults_to_drive_mode(tmp_path, conn_with_book_and_patches):
    conn, patches = conn_with_book_and_patches
    package_dir, _ = drive_export.build_batch_export_package(conn, patches, model_id="voxcpm2")
    notebook = (package_dir / "colab_kaggle_batch_tts_template.ipynb").read_text(encoding="utf-8")
    assert '"kaggle_native"' not in notebook
```

Ghi chú: fixture `conn_with_book_and_patches` — tái dùng helper dựng book/patch có sẵn trong `tests/test_export_reference_required.py` hoặc `conftest.py` nếu đã có; nếu chưa, thêm 1 fixture cục bộ trong file test này theo đúng cách các test export hiện có tự dựng dữ liệu (xem `tests/test_export_reference_required.py` để copy khuôn).

- [x] **Step 2: Chạy test, xác nhận fail**

```bash
pytest tests/test_kaggle_export_package.py -v
```

- [x] **Step 3: Sửa `app/drive_export.py`**

Thêm tham số `mode: str = "drive"` vào `build_batch_export_package`. Khi thay thế placeholder notebook, dùng `notebook_src.replace('MODE = "drive"', f'MODE = "{mode}"')` thay vì chỉ thay `__GDRIVE_CREDS__`. Khi `mode == "kaggle_native"`, **không** gọi phần baked-Drive-creds (`creds_literal`/`__GDRIVE_CREDS__` để trống hẳn, không nhận `gdrive_creds` làm tham số).

Viết `build_kaggle_export_package` như một wrapper mỏng gọi `build_batch_export_package(..., mode="kaggle_native")` không truyền `gdrive_creds`, cùng docstring giải thích vì sao (không cần secret, không cần tài khoản Drive nào).

- [x] **Step 4: Chạy test, xác nhận pass**

```bash
pytest tests/test_kaggle_export_package.py -v
```

- [x] **Step 5: Chạy lại toàn bộ test export hiện có để chắc đường Drive không đổi hành vi**

```bash
pytest tests/ -k "export or drive_export" -q
```

- [x] **Step 6: Commit**

```bash
git add app/drive_export.py tests/test_kaggle_export_package.py
git commit -m "feat(kaggle): add build_kaggle_export_package building a Drive-free batch package"
```

---

### Task 8: `app/jobqueue/handlers/kaggle_tts.py` — handler chính

> **Lưu ý khi bắt tay vào task này:** Task 5 đã bỏ `patch_import.import_batch_patch()` (xem "Scope thực tế" ở Task 5) — mọi chỗ dưới đây viết `kaggle_tts.patch_import.import_batch_patch(...)` chỉ là placeholder từ bản kế hoạch gốc. Handler thật sẽ tự viết orchestration của nó: gọi `patch_import.resolve_batch_result(...)` + `patch_import.install_imported_wav(...)` (2 hàm pure có thật) cộng với `repository.mark_patch_done`/`on_patch_audio_ready`/cập nhật `patch_export` riêng, tương tự những gì `import_patch_from_drive` làm nhưng không đi qua `locked_conn`/HTTP. Viết lại test ở Step 1 cho khớp trước khi implement, đừng copy nguyên văn pseudocode `import_batch_patch` dưới đây.

**Files:**
- Create: `app/jobqueue/handlers/kaggle_tts.py`
- Test: `tests/test_kaggle_tts_handler.py`

**Interfaces:**

```python
def handle(ctx: JobContext) -> dict | None
```

Payload: `{"book_id": int, "patch_ids": list[int], "model_id": str, "voice_id": str | None, "max_chars": int, "with_effects": bool}`.

Handler nhận `kaggle_api`/`kaggle_accounts`/`drive_export`/`patch_import` như module-level imports (không dependency injection phức tạp) — test dùng `monkeypatch` để thay các hàm cấp module bằng fake, đúng phong cách test handler khác trong repo (xem `tests/` cho `video.handle`/`youtube_upload.handle` nếu có ví dụ tương tự, nếu không thì theo mẫu dưới).

- [x] **Step 1: Viết test thất bại**

```python
"""kaggle_tts.handle: vòng lặp claim account -> push -> poll -> import -> xoay account,
không chạm mạng thật (mọi hàm kaggle_api/kaggle_accounts bị monkeypatch)."""
from __future__ import annotations

from pathlib import Path

import pytest

from app import db
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.joblog import JobLogger
from app.jobqueue.handlers import kaggle_tts
from app.jobqueue.models import JobRescheduled
from app.kaggle_api import KernelStatus


def _ctx(conn, job_type="kaggle_tts", payload=None):
    job_id = store.enqueue(conn, job_type, payload=payload or {})
    job = store.claim(conn, job_type, "w")
    return JobContext(job, conn, JobLogger(job.id, job_type), lambda: False)


def test_handle_completes_when_one_kernel_run_imports_everything(tmp_path, monkeypatch):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    from app import kaggle_accounts as ka
    account_id = ka.create_account(conn, "acc1", "user1", "key1")

    monkeypatch.setattr(kaggle_tts.drive_export, "build_kaggle_export_package",
                         lambda *a, **k: (tmp_path, {"patches": [{"patch_id": 1, "result_wav": "result/1_001.wav"}]}))
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda *a, **k: "user1/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.COMPLETE)
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_output", lambda *a, **k: [])

    imported = []
    monkeypatch.setattr(kaggle_tts.patch_import, "import_batch_patch",
                         lambda *a, **k: imported.append(1) or type("R", (), {"installed": True})())
    monkeypatch.setattr(kaggle_tts, "_missing_patch_ids", lambda *a, **k: [])

    ctx = _ctx(conn, payload={"book_id": 1, "patch_ids": [1], "model_id": "voxcpm2"})
    result = kaggle_tts.handle(ctx)
    assert imported == [1]
    assert ka.get_account(conn, account_id)["status"] == "idle"


def test_handle_rotates_to_a_second_account_when_first_runs_out_of_quota(monkeypatch, tmp_path):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    from app import kaggle_accounts as ka
    ka.create_account(conn, "acc1", "user1", "key1")
    ka.create_account(conn, "acc2", "user2", "key2")

    calls = {"n": 0}
    def fake_push(account, *a, **k):
        calls["n"] += 1
        return f"{account.username}/slug"
    monkeypatch.setattr(kaggle_tts.drive_export, "build_kaggle_export_package",
                         lambda *a, **k: (tmp_path, {"patches": [{"patch_id": 1, "result_wav": "result/1_001.wav"},
                                                                   {"patch_id": 2, "result_wav": "result/1_002.wav"}]}))
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", fake_push)
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.COMPLETE)
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_output", lambda *a, **k: [])
    monkeypatch.setattr(kaggle_tts.patch_import, "import_batch_patch",
                         lambda conn, patch, *a, **k: type("R", (), {"installed": patch.id == 1})())
    # patch 1 nhập được ở lượt account đầu; patch 2 chỉ nhập được sau khi xoay sang account 2
    calls_to_missing = {"n": 0}
    def fake_missing(conn, book_id, patch_ids):
        calls_to_missing["n"] += 1
        return [] if calls_to_missing["n"] > 1 else [2]
    monkeypatch.setattr(kaggle_tts, "_missing_patch_ids", fake_missing)
    # account 1 hết quota ngay sau lượt đầu
    monkeypatch.setattr(kaggle_tts.kaggle_accounts, "remaining_quota_seconds",
                         lambda conn, account_id: 0 if account_id == 1 else 3600)

    ctx = _ctx(conn, payload={"book_id": 1, "patch_ids": [1, 2], "model_id": "voxcpm2"})
    kaggle_tts.handle(ctx)
    assert calls["n"] >= 2


def test_handle_raises_job_rescheduled_when_no_account_has_quota(monkeypatch, tmp_path):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    from app import kaggle_accounts as ka
    account_id = ka.create_account(conn, "acc1", "user1", "key1")
    ka.claim_idle_account(conn, job_id=999)  # busy, giả lập account đang bận nơi khác
    ka.release_account(conn, account_id, cooldown_until="2099-01-01T00:00:00+00:00")

    ctx = _ctx(conn, payload={"book_id": 1, "patch_ids": [1], "model_id": "voxcpm2"})
    with pytest.raises(JobRescheduled):
        kaggle_tts.handle(ctx)


def test_handle_returns_early_when_cancelled(monkeypatch, tmp_path):
    conn = db.connect(":memory:")
    db.init_schema(conn)
    from app import kaggle_accounts as ka
    ka.create_account(conn, "acc1", "user1", "key1")
    monkeypatch.setattr(kaggle_tts.drive_export, "build_kaggle_export_package",
                         lambda *a, **k: (tmp_path, {"patches": [{"patch_id": 1, "result_wav": "result/1_001.wav"}]}))
    monkeypatch.setattr(kaggle_tts.kaggle_api, "push_kernel", lambda *a, **k: "user1/slug")
    monkeypatch.setattr(kaggle_tts.kaggle_api, "kernel_status", lambda *a, **k: KernelStatus.RUNNING)
    monkeypatch.setattr(kaggle_tts.kaggle_api, "cancel_kernel", lambda *a, **k: None)

    job_id = store.enqueue(conn, "kaggle_tts", payload={"book_id": 1, "patch_ids": [1], "model_id": "voxcpm2"})
    job = store.claim(conn, "kaggle_tts", "w")
    ctx = JobContext(job, conn, JobLogger(job.id, "kaggle_tts"), lambda: True)  # should_cancel() luôn True
    result = kaggle_tts.handle(ctx)
    assert result is None
```

- [x] **Step 2: Chạy test, xác nhận fail**

```bash
pytest tests/test_kaggle_tts_handler.py -v
```

- [x] **Step 3: Viết `app/jobqueue/handlers/kaggle_tts.py`**

Theo đúng lifecycle đã tả trong spec (mục "Job type `kaggle_tts`"): vòng lặp claim → build package (chỉ patch còn thiếu) → push → poll (heartbeat + should_cancel mỗi vòng) → output → import từng patch → ghi usage → nếu còn thiếu và account còn quota thì lặp lại với cùng account; nếu account hết quota thì release (cooldown) và claim account khác; nếu không account nào rảnh thì raise `JobRescheduled(kaggle_accounts.earliest_quota_reset(ctx.conn) or <+6h>, ...)`. Viết `_missing_patch_ids(conn, book_id, patch_ids) -> list[int]` như hàm module-level (kiểm tra `patch.status`/audio đã có chưa) để test có thể monkeypatch độc lập với I/O thật.

Import ở đầu module: `from app import kaggle_api, kaggle_accounts, drive_export, patch_import` (import module, không import từng hàm) — để test monkeypatch đúng theo `kaggle_tts.kaggle_api.push_kernel` như trong test trên.

- [x] **Step 4: Chạy test, xác nhận pass**

```bash
pytest tests/test_kaggle_tts_handler.py -v
```

- [x] **Step 5: Commit**

```bash
git add app/jobqueue/handlers/kaggle_tts.py tests/test_kaggle_tts_handler.py
git commit -m "feat(kaggle): add kaggle_tts job handler with account rotation and quota rescheduling"
```

---

### Task 9: Đăng ký `kaggle_tts` trong `backfill.py` + concurrency theo số account

**Files:**
- Modify: `app/jobqueue/backfill.py`, `app/config.py`
- Test: `tests/test_jobqueue_backfill.py` (mở rộng)

**Interfaces:**
- `configured_concurrency(conn)` (đã có trong `backfill.py`, xem `test_jobqueue_backfill.py` hiện tại) SHALL cộng thêm `kaggle_tts=<số kaggle_account chưa disabled>` nếu `QUEUE_CONCURRENCY` không tự đặt `kaggle_tts` tường minh.

- [x] **Step 1: Đọc `configured_concurrency` hiện có trong `app/jobqueue/backfill.py` để biết đúng chữ ký/hành vi trước khi sửa (không đoán).**

- [x] **Step 2: Viết test thất bại**

```python
def test_kaggle_tts_concurrency_matches_enabled_account_count():
    from app import db, kaggle_accounts as ka
    from app.jobqueue.backfill import configured_concurrency
    conn = db.connect(":memory:")
    db.init_schema(conn)
    ka.create_account(conn, "a1", "u1", "k1")
    a2 = ka.create_account(conn, "a2", "u2", "k2")
    ka.set_disabled(conn, a2, True)
    concurrency = configured_concurrency(conn)
    assert concurrency["kaggle_tts"] == 1


def test_explicit_queue_concurrency_overrides_the_account_count(monkeypatch):
    from app import db
    from app.config import settings
    from app.jobqueue.backfill import configured_concurrency
    monkeypatch.setattr(settings, "queue_concurrency", "kaggle_tts=7")
    conn = db.connect(":memory:")
    db.init_schema(conn)
    assert configured_concurrency(conn)["kaggle_tts"] == 7
```

- [x] **Step 3: Sửa `configured_concurrency` để tính thêm `kaggle_tts` khi chưa bị `QUEUE_CONCURRENCY` đặt tường minh, và `queue.register("kaggle_tts", kaggle_tts.handle, cancellable=True)` trong `build_queue`.**

- [x] **Step 4: Chạy test, xác nhận pass**

```bash
pytest tests/test_jobqueue_backfill.py -v
```

- [x] **Step 5: Commit**

```bash
git add app/jobqueue/backfill.py app/config.py tests/test_jobqueue_backfill.py
git commit -m "feat(kaggle): register kaggle_tts handler with account-count-based concurrency"
```

---

### Task 10: Config + `.env.example`

**Files:**
- Modify: `app/config.py`, `.env.example`

- [x] **Step 1: Thêm setting vào `app/config.py`** (sau khối `queue_*` hiện có):

```python
    # Kaggle Kernels API automation
    kaggle_poll_interval_seconds: int = 30
    kaggle_max_session_hours: int = 9
    kaggle_weekly_gpu_quota_hours: int = 30
```

- [x] **Step 2: Thêm ví dụ vào `.env.example`** cạnh khối Hugging Face/Drive hiện có, giải thích ngắn gọn rằng tài khoản Kaggle được thêm qua trang settings (không qua biến môi trường) — chỉ 3 số trên là cấu hình qua env.

- [x] **Step 3: Chạy test cấu hình hiện có (nếu có test load settings) để chắc không phá gì**

```bash
pytest tests/ -k config -q
```

- [x] **Step 4: Commit**

```bash
git add app/config.py .env.example
git commit -m "feat(kaggle): add poll interval / session cap / weekly quota settings"
```

---

### Task 11: Routes — `app/routes/kaggle.py` + endpoint export-batch-kaggle

**Files:**
- Create: `app/routes/kaggle.py`
- Modify: `app/routes/patches.py`, `app/main.py`
- Test: `tests/test_kaggle_routes.py`

**Interfaces:**
- `GET /api/ui/kaggle` -> `{"accounts": [...]}`, mỗi account có `remaining_quota_hours` tính sẵn.
- `POST /kaggle/accounts` (`label`, `username`, `api_key`)
- `POST /kaggle/accounts/{id}/edit`
- `POST /kaggle/accounts/{id}/delete` -> 400 nếu đang `in_use_by_job_id`
- `POST /kaggle/accounts/{id}/toggle`
- `POST /books/{book_id}/patches/export-batch-kaggle` (`patch_ids`, `model_id`, `voice_id`, `max_chars`, `with_effects`) -> JSON `{"job_id": int}`

- [x] **Step 1: Viết test thất bại**

```python
"""Routes Kaggle: CRUD account qua form, enqueue kaggle_tts qua export-batch-kaggle."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_create_list_and_delete_account(client: TestClient):
    resp = client.post("/kaggle/accounts", data={"label": "acc1", "username": "u1", "api_key": "k1"})
    assert resp.status_code in (200, 303)
    data = client.get("/api/ui/kaggle").json()
    assert len(data["accounts"]) == 1
    account_id = data["accounts"][0]["id"]
    resp = client.post(f"/kaggle/accounts/{account_id}/delete")
    assert resp.status_code in (200, 303)


def test_delete_refuses_an_account_in_use(client: TestClient, conn):
    from app import kaggle_accounts as ka
    account_id = ka.create_account(conn, "acc1", "u1", "k1")
    ka.claim_idle_account(conn, job_id=1)
    resp = client.post(f"/kaggle/accounts/{account_id}/delete")
    assert resp.status_code == 400


def test_export_batch_kaggle_enqueues_a_job(client: TestClient, book_with_patches):
    book_id, patch_ids = book_with_patches
    resp = client.post(
        f"/books/{book_id}/patches/export-batch-kaggle",
        data={"patch_ids": patch_ids, "model_id": "voxcpm2"},
    )
    assert resp.status_code == 200
    assert "job_id" in resp.json()


def test_export_batch_kaggle_dedupes_per_book(client: TestClient, book_with_patches):
    book_id, patch_ids = book_with_patches
    first = client.post(f"/books/{book_id}/patches/export-batch-kaggle",
                         data={"patch_ids": patch_ids, "model_id": "voxcpm2"}).json()
    second = client.post(f"/books/{book_id}/patches/export-batch-kaggle",
                          data={"patch_ids": patch_ids, "model_id": "voxcpm2"}).json()
    assert first["job_id"] == second["job_id"]
```

Ghi chú: `client`/`conn`/`book_with_patches` — tái dùng fixture đã có trong `conftest.py`/các test route khác (`tests/test_export_reference_required.py`, test route drive/patches hiện có) theo đúng khuôn của repo; nếu tên khác, đối chiếu file conftest thật trước khi viết.

- [x] **Step 2: Chạy test, xác nhận fail**

```bash
pytest tests/test_kaggle_routes.py -v
```

- [x] **Step 3: Viết `app/routes/kaggle.py`** theo đúng pattern Form/Redirect + JSON aggregate của `app/routes/drive.py` (xem `drive_create_client`/`drive_delete_client`/`drive_kaggle_credentials` làm khuôn), gọi thẳng `app.kaggle_accounts`.

- [x] **Step 4: Thêm endpoint `export-batch-kaggle` vào `app/routes/patches.py`**, tái dùng `_load_batch_patches`/`_save_export_audio_settings` đã có, enqueue qua `jobqueue.store.enqueue(conn, "kaggle_tts", payload={...}, book_id=book_id, dedupe_key=f"kaggle_tts:book={book_id}")`, trả `JSONResponse({"job_id": job_id or đã có sẵn từ find_live_by_dedupe})`.

- [x] **Step 5: `app.include_router(kaggle.router)` trong `app/main.py`**

- [x] **Step 6: Chạy test, xác nhận pass**

```bash
pytest tests/test_kaggle_routes.py -v
```

- [x] **Step 7: Chạy cả suite**

```bash
pytest tests/ -q
```

- [x] **Step 8: Commit**

```bash
git add app/routes/kaggle.py app/routes/patches.py app/main.py tests/test_kaggle_routes.py
git commit -m "feat(kaggle): add account CRUD routes and export-batch-kaggle endpoint"
```

---

### Task 12: Xác minh chi tiết Kaggle API thật (không viết code — chốt lại `app/kaggle_api.py` nội bộ)

**Files:**
- Modify: `app/kaggle_api.py` (chỉ phần nội bộ, không đổi public interface đã test ở Task 3)

Task này ban đầu định "không viết code", chỉ đối chiếu tài liệu — nhưng việc đối chiếu tự nó lật ra lỗi thiết kế nghiêm trọng (kernel push KHÔNG thể đính file tuỳ ý, chỉ gửi được đúng 1 file notebook qua field `text`; auth là Basic chứ không phải Bearer; toàn bộ call là POST tới path dạng RPC `{service}.{Service}/{Method}`, không phải REST-with-query-params; giá trị status là UPPER_SNAKE_CASE) — nên đã sửa thẳng vào `app/kaggle_api.py` + `app/jobqueue/handlers/kaggle_tts.py` (thêm `create_dataset`/`_upload_blob` cho payload manifest+reference clip, giờ bắt buộc chứ không phải mở rộng tuỳ chọn) và test tương ứng, dựa trên đối chiếu **mã nguồn chính thức** của Kaggle (`github.com/Kaggle/kaggle-cli`, `github.com/Kaggle/kaggle-sdk-python`), không phải đoán.

- [x] Đối chiếu tài liệu/mã nguồn Kaggle API — đã đọc trực tiếp `kaggle_api_extended.py`, `kaggle_http_client.py`, và các file `types/*.py` sinh từ `kagglesdk` qua `gh api repos/Kaggle/...`. Kết quả đầy đủ ghi trong docstring đầu `app/kaggle_api.py` và mục "UPDATE (Task 12...)" của design doc.
- [x] Cập nhật `_auth_header()`, base URL, toàn bộ path/method/body của `push_kernel`/`kernel_status`/`kernel_output`, thêm `create_dataset`/`_upload_blob`. Giữ nguyên chữ ký public `push_kernel`/`kernel_status`/`kernel_output`/`cancel_kernel` (test Task 3 chỉ cần viết lại nội dung assert, không đổi cách gọi từ `kaggle_tts.py`).
- [x] Wire `create_dataset` vào `app/jobqueue/handlers/kaggle_tts.py`: tạo dataset từ `package_dir` trước khi push, truyền `dataset_sources=[dataset_ref]` vào metadata.
- [x] Ghi lại phát hiện vào design doc (mục mới "UPDATE (Task 12...)"), bao gồm phần **CHƯA** xác minh được (cần tài khoản thật): shape thật của response `kernel_output`, license dataset có được chấp nhận không, cách lấy `kernel_session_id` số để `cancel_kernel` thật sự huỷ được kernel (hiện là no-op có chủ đích, xem docstring).
- [ ] **Việc duy nhất còn lại, cần con người:** test thủ công 1 lần với 1 tài khoản Kaggle thật, 1 batch nhỏ (1 patch, vài chunk) — xác nhận: tạo dataset thành công, push kernel thành công, poll ra đúng trạng thái, `kernel_output` tải đúng file, `patch_import.resolve_batch_result`/`install_imported_wav` cài đặt được WAV. Không thể tự làm được trong phiên này (không có tài khoản/API key Kaggle thật).
- [x] Commit riêng, không gộp với Task 3 (commit đã tách: sửa `kaggle_api.py`/test Task 3 giữ nguyên ở commit của Task 3; các sửa đổi Task 12 nằm ở commit riêng của Task 12).

---

### Task 13: Frontend — trang quản lý account + nút export Kaggle

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/pages/DrivePage.tsx` (thêm tab "Kaggle") hoặc tạo `frontend/src/pages/KagglePage.tsx` mới + route — quyết định tại lúc code theo cái nào đọc tự nhiên hơn một khi đã thấy danh sách account thật trên UI (spec để ngỏ, xem mục Frontend).
- Modify: `frontend/src/pages/book-detail/ExportPanel.tsx`

**Interfaces (`api.ts`):**

```typescript
export type KaggleAccount = {
  id: number;
  label: string;
  username: string;
  status: "idle" | "busy" | "cooldown" | "disabled";
  remaining_quota_hours: number;
  created_at: string;
};
```

- [x] **Step 1: Thêm type `KaggleAccount` vào `frontend/src/api.ts`**, theo đúng khuôn `DriveAccount` đã có.

- [x] **Step 2: Thêm UI quản lý account** (danh sách + form thêm/sửa + nút xoá/toggle), gọi `/api/ui/kaggle` + `postForm("/kaggle/accounts", ...)` v.v., theo đúng khuôn `DrivePage.tsx`'s `loadData`/`handleCreateTarget`/`handleDeleteTarget`.

- [x] **Step 3: Thêm nút "Kaggle (tự động)" trong `ExportPanel.tsx`** cạnh 2 nút export hiện có (Drive/zip) — cùng form chọn patch + model + voice + max_chars + with_effects, gọi `postJson("/books/{id}/patches/export-batch-kaggle", ...)`, hiện link tới job vừa enqueue trên trang Queue (route đã có sẵn, không cần sửa Queue page).

- [x] **Step 4: Chạy dev server, tự tay kiểm tra** (theo hướng dẫn "For UI or frontend changes" — phải mở trình duyệt thật, không chỉ dựa vào type-check):
  - Trang account: thêm 1 account giả, thấy nó xuất hiện trong danh sách với trạng thái `idle`; xoá được.
  - `ExportPanel`: nút "Kaggle (tự động)" hiện ra, bấm enqueue được job (kiểm tra job xuất hiện trên trang Queue) khi có ít nhất 1 account đã cấu hình; báo lỗi rõ ràng khi chưa có account nào.

- [x] **Step 5: `npm run build` (hoặc lệnh build/typecheck hiện có của frontend) để chắc không có lỗi TypeScript**

- [x] **Step 6: Commit**

```bash
git add frontend/src/api.ts frontend/src/pages/DrivePage.tsx frontend/src/pages/book-detail/ExportPanel.tsx
git commit -m "feat(kaggle): add account management UI and Kaggle export button"
```

---

## Final check

- [x] `pytest tests/ -q` — toàn bộ suite pass, không skip test mới thêm.
- [x] Đối chiếu lại spec: mọi mục "SHALL" trong `docs/superpowers/specs/2026-09-05-kaggle-api-tts-automation-design.md` có tương ứng ít nhất 1 task/test ở trên.
- [x] Xác nhận đường Drive/Colab hiện có (zip, Drive Desktop, Drive API) chạy y hệt trước — không có test nào trong các file cũ bị sửa ngoài phạm vi refactor ở Task 5.
