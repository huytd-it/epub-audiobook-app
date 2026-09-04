"""Export/import the YouTube upload queue as an editable JSON or CSV sheet.

The point of this module is round-tripping: `export_records` produces exactly the
columns `parse_records` reads back, so a user can dump the queue, edit titles /
descriptions / tags / privacy / playlist in a spreadsheet, and re-import.

Only `EDITABLE_FIELDS` are ever written back; the rest of the columns are carried
along for orientation. `apply_import` never touches a row the upload worker may be
writing to at the same moment (status 'uploading').
"""
from __future__ import annotations

import csv
import io
import json
import sqlite3
from datetime import datetime, timezone

from app.youtube_metadata import YOUTUBE_DESCRIPTION_LIMIT, YOUTUBE_TITLE_LIMIT

#: Column order of an exported sheet. `id` is the join key on import.
EXPORT_COLUMNS = [
    "id",
    "title",
    "description",
    "tags",
    "privacy_status",
    "playlist_id",
    "video_path",
    "not_for_kids",
    "ai_labels_enabled",
    "altered_content",
    "status",
    "youtube_video_id",
    "created_at",
]

#: Fields an import may write back. Everything else in the sheet is read-only.
EDITABLE_FIELDS = ("title", "description", "tags", "privacy_status", "playlist_id", "video_path",
                   "not_for_kids", "ai_labels_enabled", "altered_content")

#: Sheet fields that hold a true/false flag rather than free text.
BOOLEAN_FIELDS = ("not_for_kids", "ai_labels_enabled", "altered_content")

PRIVACY_VALUES = ("private", "unlisted", "public")

#: The worker owns rows in these statuses; importing over them would race it.
LOCKED_STATUSES = ("uploading",)

IMPORT_MODES = ("update", "upsert")

MAX_TAGS_CHARS = 500


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snapshot_playlist_id(raw) -> str:
    """Playlist target the worker will actually use, out of metadata_snapshot."""
    if not raw:
        return ""
    try:
        snapshot = json.loads(raw)
    except (TypeError, ValueError):
        return ""
    if not isinstance(snapshot, dict):
        return ""
    config = snapshot.get("automation", {}).get("youtube", {})
    return config.get("playlist_id") or ""


def _tags_text(raw) -> str:
    """youtube_uploads.tags is a JSON list; the sheet uses a comma-separated string."""
    if raw in (None, ""):
        return ""
    if isinstance(raw, list):
        return ", ".join(str(t) for t in raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return str(raw)
    if isinstance(parsed, list):
        return ", ".join(str(t) for t in parsed)
    return str(parsed)


def split_tags(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def row_to_record(row) -> dict:
    """One youtube_uploads row -> one exported sheet row."""
    data = dict(row)
    return {
        "id": data["id"],
        "title": data.get("title") or "",
        "description": data.get("description") or "",
        "tags": _tags_text(data.get("tags")),
        "privacy_status": data.get("privacy_status") or "private",
        # Prefer the queued intent; the column is only filled in after publishing.
        "playlist_id": _snapshot_playlist_id(data.get("metadata_snapshot")) or (data.get("playlist_id") or ""),
        "video_path": data.get("video_path") or "",
        "not_for_kids": "true" if data.get("not_for_kids", 1) else "false",
        "ai_labels_enabled": "true" if data.get("ai_labels_enabled", 0) else "false",
        # altered_content = disclosure "Sử dụng AI" (containsSyntheticMedia) — default true
        # cho các dòng cũ chưa có cột này, để export không lật ngược hàng loạt.
        "altered_content": "true" if data.get("altered_content", 1) else "false",
        "status": data.get("status") or "",
        "youtube_video_id": data.get("youtube_video_id") or "",
        "created_at": data.get("created_at") or "",
    }


def export_records(conn: sqlite3.Connection, ids: list[int] | None = None,
                   statuses: list[str] | None = None) -> list[dict]:
    """Exported sheet rows, newest first. `ids`/`statuses` narrow the selection."""
    sql = "SELECT * FROM youtube_uploads"
    params: list = []
    where: list[str] = []
    if ids:
        where.append(f"id IN ({','.join('?' * len(ids))})")
        params.extend(ids)
    if statuses:
        where.append(f"status IN ({','.join('?' * len(statuses))})")
        params.extend(statuses)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    return [row_to_record(row) for row in conn.execute(sql, params).fetchall()]


def records_to_csv(records: list[dict]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow({key: record.get(key, "") for key in EXPORT_COLUMNS})
    return buffer.getvalue()


def records_to_json(records: list[dict]) -> str:
    return json.dumps(
        {"kind": "youtube_uploads", "version": 1, "exported_at": _now_iso(), "uploads": records},
        ensure_ascii=False,
        indent=2,
    )


def detect_format(filename: str | None, fallback: str = "json") -> str:
    name = (filename or "").lower()
    if name.endswith(".csv"):
        return "csv"
    if name.endswith(".json"):
        return "json"
    return fallback


def parse_records(raw: bytes | str, fmt: str) -> list[dict]:
    """Read an edited sheet back. Accepts the exported wrapper or a bare list."""
    if fmt not in ("json", "csv"):
        raise ValueError("format must be 'json' or 'csv'")
    if isinstance(raw, bytes):
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError(
                "File không phải UTF-8 (thường do Excel lưu bằng 'CSV (Comma delimited)' thay vì "
                "'CSV UTF-8 (Comma delimited)', làm hỏng emoji/tiếng Việt trong mô tả). "
                f"Hãy lưu lại đúng định dạng CSV UTF-8, hoặc dùng JSON. Chi tiết: {exc}"
            ) from exc
    else:
        text = raw
    if fmt == "csv":
        # Excel writes \r\n line endings, including inside quoted multi-line cells
        # (e.g. a description with a chapter list). csv.DictReader on a plain string
        # buffer does not do Python's universal-newline translation, so those \r\n
        # pairs would otherwise land in the stored description verbatim and drift the
        # text every time it round-trips through a spreadsheet. Normalize once here.
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        rows = list(csv.DictReader(io.StringIO(text)))
        if rows and not any(key in (rows[0] or {}) for key in EXPORT_COLUMNS):
            raise ValueError("CSV thieu dong tieu de (header) hop le")
        return [{(k or "").strip(): v for k, v in row.items() if k} for row in rows]

    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"JSON khong hop le: {exc}") from exc
    if isinstance(payload, dict):
        payload = payload.get("uploads", payload.get("items"))
    if not isinstance(payload, list):
        raise ValueError("JSON phai la danh sach ban ghi hoac co khoa 'uploads'")
    if any(not isinstance(item, dict) for item in payload):
        raise ValueError("Moi ban ghi trong JSON phai la mot object")
    return payload


def _clean(value) -> str:
    return "" if value is None else str(value).strip()


_TRUE_TEXT = ("true", "1", "yes")
_FALSE_TEXT = ("false", "0", "no")


def _parse_bool(value, default: bool) -> bool:
    """Parse a sheet boolean cell. Blank (missing or "") falls back to `default` -
    the row's normal default when creating, or explicit "false" (see
    `_normalize_bool_text`, called with default=False) when editing an existing row."""
    text = _clean(value).lower()
    if text in _TRUE_TEXT:
        return True
    if text in _FALSE_TEXT:
        return False
    return default


def _normalize_bool_text(value) -> str:
    return "true" if _parse_bool(value, default=False) else "false"


def _validate(field: str, value: str) -> str:
    """Return an error message for a rejected value, or '' when it is fine."""
    if field == "title":
        if not value:
            return "Tiêu đề không được để trống"
        if len(value) > YOUTUBE_TITLE_LIMIT:
            return f"Tiêu đề vượt quá {YOUTUBE_TITLE_LIMIT} ký tự"
    if field == "description" and len(value) > YOUTUBE_DESCRIPTION_LIMIT:
        return f"Mô tả vượt quá {YOUTUBE_DESCRIPTION_LIMIT} ký tự"
    if field == "privacy_status" and value not in PRIVACY_VALUES:
        return f"privacy_status phải là một trong: {', '.join(PRIVACY_VALUES)}"
    if field == "tags" and len(", ".join(split_tags(value))) > MAX_TAGS_CHARS:
        return f"Danh sách tags vượt quá {MAX_TAGS_CHARS} ký tự"
    if field == "video_path" and not value:
        return "Đường dẫn video không được để trống"
    if field in BOOLEAN_FIELDS and value != "" and value.lower() not in (*_TRUE_TEXT, *_FALSE_TEXT):
        return f"{field} phải là true/false (hoặc 1/0)"
    return ""


def _changes(record: dict, current: dict) -> tuple[dict, list[str]]:
    """Fields the sheet actually changes, plus validation errors."""
    changes: dict[str, str] = {}
    errors: list[str] = []
    for field in EDITABLE_FIELDS:
        if field not in record:
            continue  # a column left out of the sheet means "leave alone"
        value = _clean(record[field])
        if field == "tags":
            value = ", ".join(split_tags(value))
        error = _validate(field, value)
        if error:
            errors.append(error)
            continue
        if field in BOOLEAN_FIELDS:
            value = _normalize_bool_text(value)
        if value != _clean(current.get(field)):
            changes[field] = value
    return changes, errors


def _apply_changes(conn: sqlite3.Connection, upload_id: int, changes: dict) -> None:
    assignments: list[str] = []
    params: list = []
    for field, value in changes.items():
        if field == "tags":
            assignments.append("tags=?")
            params.append(json.dumps(split_tags(value)))
        elif field in BOOLEAN_FIELDS:
            assignments.append(f"{field}=?")
            params.append(1 if value == "true" else 0)
        elif field == "playlist_id":
            # The worker reads its playlist target out of metadata_snapshot (see
            # youtube.postprocess_upload); the playlist_id column is a result field.
            snapshot = json.dumps({"automation": {"youtube": {
                "playlist_mode": "existing", "playlist_id": value,
            }}}) if value else None
            assignments.append("metadata_snapshot=?")
            params.append(snapshot)
        else:
            assignments.append(f"{field}=?")
            params.append(value)
    if not assignments:
        return
    params.append(upload_id)
    conn.execute(f"UPDATE youtube_uploads SET {', '.join(assignments)} WHERE id=?", params)


def _result(row_no: int, upload_id, status: str, *, changes=None, message: str = "",
            warning: str = "") -> dict:
    return {
        "row": row_no,
        "id": upload_id,
        "status": status,
        "changes": changes or {},
        "message": message,
        "warning": warning,
    }


def apply_import(conn: sqlite3.Connection, records: list[dict], *, mode: str = "update",
                 dry_run: bool = False, create=None) -> dict:
    """Write an edited sheet back to youtube_uploads.

    `mode='update'` only touches rows whose `id` exists. `mode='upsert'` additionally
    queues a new upload for every row with a blank `id`, delegating to `create(conn,
    payload)` so the caller owns the job-queue wiring. `dry_run=True` reports the same
    per-row verdicts without writing anything.
    """
    if mode not in IMPORT_MODES:
        raise ValueError(f"mode must be one of: {', '.join(IMPORT_MODES)}")
    if not isinstance(records, list):
        raise ValueError("records must be a list")

    results: list[dict] = []
    written = False
    for index, record in enumerate(records, start=1):
        raw_id = _clean(record.get("id"))
        if not raw_id:
            outcome = _import_new(conn, index, record, mode=mode, dry_run=dry_run, create=create)
            results.append(outcome)
            if outcome["status"] == "created" and outcome["id"] is not None:
                written = True
            continue

        try:
            upload_id = int(float(raw_id))
        except ValueError:
            results.append(_result(index, raw_id, "error", message=f"id không hợp lệ: {raw_id!r}"))
            continue

        row = conn.execute("SELECT * FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
        if row is None:
            results.append(_result(index, upload_id, "error", message="Không tìm thấy bản ghi upload"))
            continue

        current = row_to_record(row)
        if current["status"] in LOCKED_STATUSES:
            results.append(_result(index, upload_id, "skipped",
                                   message=f"Đang upload ({current['status']}), không thể sửa"))
            continue

        changes, errors = _changes(record, current)
        if errors:
            results.append(_result(index, upload_id, "error", message="; ".join(errors)))
            continue
        if not changes:
            results.append(_result(index, upload_id, "unchanged"))
            continue

        warning = ""
        if current["status"] == "done":
            warning = "Video đã lên YouTube: sửa đổi chỉ áp dụng cho bản ghi trong ứng dụng"
        if not dry_run:
            _apply_changes(conn, upload_id, changes)
            written = True
        results.append(_result(index, upload_id, "updated", changes=changes, warning=warning))

    if written:
        conn.commit()

    counts = {key: 0 for key in ("updated", "created", "unchanged", "skipped", "error")}
    for item in results:
        counts[item["status"]] = counts.get(item["status"], 0) + 1
    return {
        "mode": mode,
        "dry_run": dry_run,
        "total": len(results),
        "counts": counts,
        "results": results,
    }


def _import_new(conn: sqlite3.Connection, index: int, record: dict, *, mode: str,
                dry_run: bool, create) -> dict:
    """A sheet row without an id: queue a brand new upload (upsert mode only)."""
    if mode != "upsert":
        return _result(index, None, "error",
                       message="Thiếu cột id (chọn chế độ 'Thêm mới' để tạo bản ghi)")
    title = _clean(record.get("title"))
    video_path = _clean(record.get("video_path"))
    errors = [e for e in (_validate("title", title), _validate("video_path", video_path)) if e]
    for field in ("description", "privacy_status", "tags", *BOOLEAN_FIELDS):
        if field in record and _clean(record[field]):
            error = _validate(field, _clean(record[field]))
            if error:
                errors.append(error)
    if errors:
        return _result(index, None, "error", message="; ".join(errors))

    payload = {
        "video_path": video_path,
        "title": title,
        "description": _clean(record.get("description")),
        "tags": split_tags(record.get("tags")),
        "privacy_status": _clean(record.get("privacy_status")) or "private",
        "playlist_id": _clean(record.get("playlist_id")),
        "not_for_kids": _parse_bool(record.get("not_for_kids"), default=True),
        "ai_labels_enabled": _parse_bool(record.get("ai_labels_enabled"), default=False),
        "altered_content": _parse_bool(record.get("altered_content"), default=True),
    }
    if dry_run or create is None:
        return _result(index, None, "created", changes=payload)
    return _result(index, create(conn, payload), "created", changes=payload)
