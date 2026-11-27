"""Thư mục sync của kernel Kaggle trên Drive dùng chung (setting "Drive cho Kaggle"):
liệt kê kèm dung lượng / trạng thái import, và dọn (chuyển vào thùng rác) các batch đã
import xong. Không bao giờ tự xoá: chỉ chạy khi người dùng bấm, và chỉ "trashed=True"
(khôi phục được trong 30 ngày).

Với scope drive.file app chỉ thấy file do chính OAuth client này tạo (kể cả file kernel
tạo bằng cùng client qua GDRIVE_CREDS), nên không đụng tới dữ liệu khác trên Drive.
Mọi lời gọi đi qua service của google_drive.get_drive_service → egress scope "drive".
"""
from __future__ import annotations

import json

from app import google_drive, kaggle_packages, repository

FOLDER_MIME = "application/vnd.google-apps.folder"
_LIVE = ("pending", "running", "cancelling")


def live_batch_ids(conn) -> set[str]:
    """batch_id của mọi job kaggle_tts đang chạy / chờ / hoãn (hoãn = pending)."""
    out: set[str] = set()
    for row in conn.execute(
        f"SELECT payload_json FROM job WHERE job_type='kaggle_tts' AND status IN ({','.join('?' * len(_LIVE))})",
        _LIVE,
    ):
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except ValueError:
            continue
        if payload.get("package_id"):
            package = kaggle_packages.get(conn, int(payload["package_id"]))
            if package:
                out.add(package["batch_id"])
            continue
        batch_id = kaggle_packages.batch_id_for_payload(payload)
        if batch_id:
            out.add(batch_id)
    return out


def candidates(conn, account_id: int) -> list[dict]:
    """Các batch có thể nằm trên Drive `account_id` (chỉ DB, không gọi mạng)."""
    kaggle_packages.backfill(conn)
    busy = live_batch_ids(conn)
    out = []
    for row in conn.execute(
        "SELECT p.*, b.title AS book_title FROM kaggle_package p JOIN book b ON b.id=p.book_id ORDER BY p.id"
    ).fetchall():
        package = dict(row)
        if package["drive_account_id"] is not None and package["drive_account_id"] != account_id:
            continue  # batch này nằm trên Drive khác
        entries = json.loads(package["patches_json"] or "[]")
        done = 0
        for entry in entries:
            patch = repository.get_patch(conn, int(entry["patch_id"]))
            if patch is not None and patch.status == "done":
                done += 1
        out.append({
            "package_id": package["id"], "batch_id": package["batch_id"], "slug": package["slug"],
            "book_id": package["book_id"], "book_title": package["book_title"],
            "drive_folder_id": package["drive_folder_id"] if package["drive_account_id"] == account_id else None,
            "total": len(entries), "imported_count": done,
            "imported": bool(entries) and done == len(entries),
            "busy": package["batch_id"] in busy,
        })
    return out


def _root_folder_id(service) -> str | None:
    """Thư mục gốc 'EPUB Audiobook Exports' — chỉ tìm, không tạo."""
    files = service.files().list(
        q=("name = 'EPUB Audiobook Exports' and mimeType = 'application/vnd.google-apps.folder' "
           "and trashed = false"),
        fields="files(id)", pageSize=10,
    ).execute().get("files", [])
    return files[0]["id"] if files else None


def folder_usage(service, folder_id: str) -> tuple[int, str | None]:
    """(tổng byte của mọi file con, modifiedTime mới nhất) — đi đệ quy."""
    total, latest = 0, None
    stack = [folder_id]
    while stack:
        for entry in google_drive.list_files(service, stack.pop()):
            modified = entry.get("modifiedTime")
            if modified and (latest is None or modified > latest):
                latest = modified
            if entry.get("mimeType") == FOLDER_MIME:
                stack.append(entry["id"])
            else:
                total += int(entry.get("size") or 0)
    return total, latest


def storage_quota(service) -> dict:
    quota = service.about().get(fields="storageQuota").execute().get("storageQuota") or {}
    as_int = lambda key: int(quota[key]) if quota.get(key) not in (None, "") else None  # noqa: E731
    return {"limit": as_int("limit"), "usage": as_int("usage"),
            "usage_in_drive": as_int("usageInDrive"), "usage_in_drive_trash": as_int("usageInDriveTrash")}


def resolve_folders(service, rows: list[dict]) -> list[dict]:
    """Gắn folder Drive cho từng batch: folder đã lưu trên gói, nếu không thì thư mục
    con của gốc mang tên batch_id (gói CLI) hoặc slug (job kaggle_tts trực tiếp)."""
    by_name: dict[str, dict] = {}
    root = _root_folder_id(service)
    if root:
        for entry in google_drive.list_files(service, root):
            if entry.get("mimeType") == FOLDER_MIME:
                by_name.setdefault(entry["name"], entry)
    out = []
    for row in rows:
        folder_id = row["drive_folder_id"]
        name = None
        if not folder_id:
            match = by_name.get(row["batch_id"]) or by_name.get(row["slug"])
            if match is None:
                continue
            folder_id, name = match["id"], match["name"]
        out.append({**row, "folder_id": folder_id, "folder_name": name or row["batch_id"]})
    return out


def trash_folder(service, folder_id: str) -> None:
    service.files().update(fileId=folder_id, body={"trashed": True}, fields="id").execute()
