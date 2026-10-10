"""Durable Kaggle batches, independent of disposable queue history.

Drive identities are pinned once. Checking/resuming a package never publishes
inputs or resolves a saved folder by name.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from app import drive_export, google_drive, repository
from app.jobqueue import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS kaggle_package (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL UNIQUE,
    book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    slug TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    patches_json TEXT NOT NULL,
    last_job_id INTEGER REFERENCES job(id) ON DELETE SET NULL,
    drive_account_id INTEGER,
    drive_folder_id TEXT,
    kaggle_account_id INTEGER,
    kernel_ref TEXT,
    status TEXT NOT NULL DEFAULT 'incomplete',
    remote_count INTEGER,
    local_count INTEGER NOT NULL DEFAULT 0,
    checked_at TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kaggle_package_book ON kaggle_package(book_id);
"""

LIVE = {"pending", "running", "cancelling"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get(conn, package_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM kaggle_package WHERE id=?", (package_id,)).fetchone()
    return dict(row) if row else None


def update(conn, package_id: int, **fields) -> None:
    allowed = {"last_job_id", "drive_account_id", "drive_folder_id", "kaggle_account_id",
               "kernel_ref", "status", "remote_count", "local_count", "checked_at", "error_message"}
    if not fields.keys() <= allowed:
        raise ValueError("Unknown package field")
    fields["updated_at"] = now()
    conn.execute(
        "UPDATE kaggle_package SET " + ", ".join(f"{key}=?" for key in fields) + " WHERE id=?",
        [*fields.values(), package_id],
    )
    conn.commit()


def register(conn, job) -> dict | None:
    from app.jobqueue.handlers.kaggle_tts import _kernel_slug, _stable_batch_id

    payload = job.payload
    if payload.get("package_id"):
        return get(conn, int(payload["package_id"]))
    book_id = payload.get("book_id")
    book = repository.get_book(conn, book_id) if book_id else None
    if book is None or not payload.get("patch_ids"):
        return None
    slug = _kernel_slug(book.id, payload["patch_ids"])
    batch_id = _stable_batch_id(slug, payload.get("model_id") or "voxcpm2",
                                payload.get("voice_id"), int(payload.get("max_chars") or 0),
                                bool(payload.get("with_effects")))
    entries = []
    for pid in dict.fromkeys(payload["patch_ids"]):
        patch = repository.get_patch(conn, int(pid))
        if patch is None or patch.book_id != book.id:
            return None
        entries.append({"patch_id": patch.id, "patch_index": patch.patch_index,
                        "wav": drive_export.result_wav_name(patch)})
    stamp = now()
    conn.execute(
        """INSERT OR IGNORE INTO kaggle_package
           (batch_id, book_id, title, slug, payload_json, patches_json, last_job_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (batch_id, book.id, book.title, slug, json.dumps(payload), json.dumps(entries),
         job.id, job.created_at or stamp, stamp),
    )
    if job.id is not None:
        conn.execute(
            "UPDATE kaggle_package SET last_job_id=? WHERE batch_id=? AND (last_job_id IS NULL OR last_job_id<?)",
            (job.id, batch_id, job.id),
        )
    conn.commit()
    package = dict(conn.execute("SELECT * FROM kaggle_package WHERE batch_id=?", (batch_id,)).fetchone())
    if not package["kernel_ref"]:
        usage = conn.execute(
            "SELECT account_id, kernel_ref FROM kaggle_usage WHERE kernel_ref LIKE ? ORDER BY id DESC LIMIT 1",
            (f"%/{slug}",),
        ).fetchone()
        if usage:
            update(conn, package["id"], kaggle_account_id=usage["account_id"], kernel_ref=usage["kernel_ref"])
            package = get(conn, package["id"])
    return package


def backfill(conn) -> None:
    # Import old CLI jobs without touching Drive or requiring credentials.
    from app.jobqueue.models import Job
    for row in conn.execute(
        "SELECT * FROM job WHERE job_type='kaggle_tts' AND json_extract(payload_json, '$.package_id') IS NULL ORDER BY id"
    ).fetchall():
        job = Job.from_row(row)
        package = register(conn, job)
        if package is not None and job.status not in LIVE:
            payload = job.payload
            payload.update(package_id=package["id"], resume_existing=True)
            store.update_payload(conn, job.id, payload)


def local_path(package: dict, entry: dict) -> Path:
    return repository.get_patch_audio_path(package["book_id"], entry["patch_index"])


def has_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def result_inventory(service, folder_id: str) -> dict[str, dict]:
    # Merge historical duplicate result folders. Never create/modify any of them.
    inventory = {}
    for entry in google_drive.list_result_files(service, folder_id):
        if entry.get("mimeType") == "application/vnd.google-apps.folder" or int(entry.get("size") or 0) <= 0:
            continue
        name = entry.get("name", "")
        if name not in inventory or entry.get("modifiedTime", "") > inventory[name].get("modifiedTime", ""):
            inventory[name] = entry
    return inventory


def discover_folder(conn, package: dict) -> tuple[object, dict] | tuple[None, dict]:
    """Read-only legacy discovery by manifest batch_id across connected accounts.

    A saved identity must remain accessible: never silently switch account/folder
    after a deletion, permission error, or token failure.
    """
    from app.jobqueue.handlers.kaggle_tts import _find_drive_sync_folder_id

    if package["drive_folder_id"]:
        service = google_drive.get_drive_service(conn, package["drive_account_id"])
        metadata = service.files().get(fileId=package["drive_folder_id"], fields="id,trashed,mimeType").execute()
        if metadata.get("trashed") or metadata.get("mimeType") != "application/vnd.google-apps.folder":
            raise ValueError("Thư mục Drive đã lưu không còn khả dụng. Hãy khôi phục quyền truy cập.")
        return service, package
    for account in google_drive.list_accounts(conn):
        service = google_drive.get_drive_service(conn, account["id"])
        folder_id = _find_drive_sync_folder_id(service, package["batch_id"])
        if folder_id:
            update(conn, package["id"], drive_account_id=account["id"], drive_folder_id=folder_id)
            return service, get(conn, package["id"])
    return None, package


def list_packages(conn, *, search="", book_id=None, status="", model="", page=1,
                  per_page=25, sort="updated_at", direction="desc") -> dict:
    backfill(conn)
    rows = [dict(row) for row in conn.execute(
        """SELECT p.*, b.title AS book_title, j.status AS job_status, j.phase,
                  j.error_message AS job_error, j.next_retry_at
           FROM kaggle_package p JOIN book b ON b.id=p.book_id
           LEFT JOIN job j ON j.id=p.last_job_id"""
    )]
    books = sorted({(r["book_id"], r["book_title"]) for r in rows}, key=lambda b: b[1].casefold())
    models = set()
    items = []
    for row in rows:
        payload = json.loads(row.pop("payload_json"))
        entries = json.loads(row.pop("patches_json"))
        row["model_id"] = payload.get("model_id") or "voxcpm2"
        models.add(row["model_id"])
        row["total"] = len(entries)
        row["local_count"] = sum(has_file(local_path(row, e)) for e in entries)
        if row["job_status"] in LIVE:
            row["status"] = row["job_status"]
        elif row["status"] == "done" and row["local_count"] < row["total"]:
            row["status"] = "incomplete"
        elif row["status"] != "done" and row["job_status"] in {"failed", "cancelled"}:
            row["status"] = row["job_status"]
        row["error_message"] = row["error_message"] or row.pop("job_error")
        row.pop("job_error", None)
        row["can_resume"] = row["job_status"] not in LIVE
        haystack = f"{row['id']} {row['book_title']} {row['batch_id']} {row['kernel_ref'] or ''} {row['model_id']}"
        if search.casefold() not in haystack.casefold():
            continue
        if book_id is not None and row["book_id"] != book_id:
            continue
        if status and row["status"] != status:
            continue
        if model and row["model_id"] != model:
            continue
        items.append(row)
    keys = {"id", "book_title", "model_id", "status", "remote_count", "local_count", "created_at", "updated_at"}
    sort = sort if sort in keys else "updated_at"
    items.sort(key=lambda r: (r[sort] is not None, r[sort] if r[sort] is not None else "", r["id"]),
               reverse=direction == "desc")
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), total_pages)
    return {"items": items[(page - 1) * per_page:page * per_page], "total": total,
            "page": page, "per_page": per_page, "total_pages": total_pages,
            "books": [{"id": bid, "title": title} for bid, title in books], "models": sorted(models)}


def enqueue_action(conn, package_id: int, *, check_only: bool) -> int:
    package = get(conn, package_id)
    if package is None:
        raise LookupError("Không tìm thấy gói Kaggle")
    previous = store.get(conn, package["last_job_id"]) if package["last_job_id"] else None
    if previous and previous.status in LIVE:
        raise ValueError("Gói đang được xử lý. Hãy đợi tác vụ hiện tại kết thúc.")
    payload = json.loads(package["payload_json"])
    payload.update(package_id=package_id, check_only=check_only, resume_existing=True)
    job_id = store.enqueue(conn, "kaggle_tts", payload=payload, book_id=package["book_id"],
                           dedupe_key=f"kaggle_tts:book={package['book_id']}")
    if job_id is None:
        raise ValueError("Ebook này đang có một gói Kaggle khác hoạt động.")
    update(conn, package_id, last_job_id=job_id, error_message=None)
    return job_id
