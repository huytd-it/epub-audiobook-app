"""Toàn cảnh patch của MỌI sách cho trang "Quản lý Patch": phân loại từng patch theo
bước còn thiếu (TTS dở / lỗi, video/upload chưa xong) và đẩy tiếp chúng vào queue.

Không thêm trạng thái mới vào DB — phase được suy ra mỗi lần đọc từ patch,
patch_pipeline, youtube_uploads và job live, nên không thể lệch với nguồn sự thật."""
from __future__ import annotations

import logging
import math
import sqlite3
from collections import defaultdict

from app.jobqueue.backfill import enqueue_pending_patch_jobs
from app.patch_publishing import continue_patch_automation

logger = logging.getLogger(__name__)

LIVE_JOB_STATUSES = ("pending", "running", "cancelling")
LIVE_UPLOAD_STATUSES = ("pending", "claiming", "uploading", "waiting_for_rerender")

UNFINISHED_PHASES = frozenset({"tts_partial", "tts_failed", "publish_failed", "publish_pending"})
GROUPS: dict[str, frozenset[str] | None] = {
    "unfinished": UNFINISHED_PHASES,
    "not_started": frozenset({"not_started"}),
    "queued": frozenset({"queued"}),
    "complete": frozenset({"complete"}),
    "all": None,
}


def classify_patch(row: dict, live_job: dict | None) -> str:
    """Một phase duy nhất cho patch. `row` là một dòng của _OVERVIEW_SQL."""
    status = row["status"]
    if live_job is not None or status == "processing" or row["upload_status_row"] in LIVE_UPLOAD_STATUSES:
        return "queued"
    if status != "done":
        if row["next_chunk_index"] > 0:
            return "tts_partial"
        return "tts_failed" if status == "failed" else "not_started"
    upload_on = bool(row["auto_upload_youtube"])
    create_on = upload_on or bool(row["auto_create_video"])
    if not create_on:
        return "complete"
    # "published" ≠ đã upload: chỉ row youtube_uploads done (có video id) mới tính.
    uploaded = row["upload_status_row"] == "done" and bool(row["youtube_video_id"])
    if upload_on and uploaded:
        return "complete"
    if not upload_on and row["video_status"] == "done":
        return "complete"
    if "failed" in (row["video_status"], row["upload_status"], row["upload_status_row"]):
        return "publish_failed"
    return "publish_pending"


_OVERVIEW_SQL = """
SELECT p.id, p.book_id, p.patch_index, p.name, p.status, p.chapter_start, p.chapter_end,
       p.chapter_no_start, p.chapter_no_end, p.chunk_count, p.next_chunk_index,
       p.error_message, p.audio_path, p.updated_at,
       b.title AS book_title, b.updated_at AS book_updated_at,
       b.auto_create_video, b.auto_upload_youtube,
       pp.stage, pp.video_status, pp.upload_status, pp.last_error,
       yu.status AS upload_status_row, yu.youtube_video_id, yu.error_message AS upload_error
  FROM patch p
  JOIN book b ON b.id = p.book_id
  LEFT JOIN patch_pipeline pp ON pp.patch_id = p.id
  LEFT JOIN youtube_uploads yu ON yu.id = pp.youtube_upload_id
"""


def _live_jobs(conn: sqlite3.Connection) -> dict[int, dict]:
    placeholders = ",".join("?" for _ in LIVE_JOB_STATUSES)
    rows = conn.execute(
        f"""SELECT id, patch_id, job_type, status, progress_current, progress_total
              FROM job WHERE patch_id IS NOT NULL AND status IN ({placeholders})
             ORDER BY id""",
        LIVE_JOB_STATUSES,
    ).fetchall()
    # Job mới nhất thắng nếu một patch có nhiều job live (TTS rồi patch_video).
    return {int(r["patch_id"]): dict(r) for r in rows}


def _load(conn: sqlite3.Connection, where: str = "", params: tuple = ()) -> list[dict]:
    jobs = _live_jobs(conn)
    items = []
    for raw in conn.execute(f"{_OVERVIEW_SQL} {where}", params).fetchall():
        row = dict(raw)
        live = jobs.get(int(row["id"]))
        row["phase"] = classify_patch(row, live)
        row["live_job"] = live
        items.append(row)
    return items


def _item(row: dict) -> dict:
    return {
        "patch_id": row["id"], "book_id": row["book_id"], "book_title": row["book_title"],
        "patch_index": row["patch_index"], "name": row["name"] or "",
        "chapter_start": row["chapter_start"], "chapter_end": row["chapter_end"],
        "chapter_no_start": row["chapter_no_start"], "chapter_no_end": row["chapter_no_end"],
        "status": row["status"], "phase": row["phase"],
        "chunk_count": row["chunk_count"], "next_chunk_index": row["next_chunk_index"],
        "error_message": row["error_message"] or row["upload_error"] or row["last_error"],
        "pipeline": None if row["stage"] is None else {
            "stage": row["stage"], "video_status": row["video_status"],
            "upload_status": row["upload_status_row"] or row["upload_status"],
        },
        "youtube_video_id": row["youtube_video_id"],
        "live_job": row["live_job"],
        "updated_at": row["updated_at"],
    }


def list_patch_overview(conn: sqlite3.Connection, *, group: str = "unfinished",
                        book_id: int | None = None, q: str | None = None,
                        page: int = 1, per_page: int = 50) -> dict:
    if group not in GROUPS:
        raise ValueError(f"group không hợp lệ: {group}")
    rows = _load(conn)
    books = sorted({(r["book_id"], r["book_title"]) for r in rows}, key=lambda b: b[1].casefold())
    if book_id is not None:
        rows = [r for r in rows if r["book_id"] == book_id]
    needle = (q or "").strip().casefold()
    if needle:
        rows = [r for r in rows
                if needle in (r["book_title"] or "").casefold() or needle in (r["name"] or "").casefold()]
    counts = {name: (len(rows) if phases is None else sum(r["phase"] in phases for r in rows))
              for name, phases in GROUPS.items()}
    phases = GROUPS[group]
    if phases is not None:
        rows = [r for r in rows if r["phase"] in phases]
    # Sách mới cập nhật lên đầu; trong một sách giữ thứ tự patch (sort ổn định).
    rows.sort(key=lambda r: (r["book_id"], r["patch_index"]))
    rows.sort(key=lambda r: (r["book_updated_at"] or ""), reverse=True)
    total = len(rows)
    per_page = max(1, min(200, per_page))
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    return {
        "items": [_item(r) for r in rows[start:start + per_page]],
        "counts": counts, "total": total, "page": page, "per_page": per_page,
        "total_pages": total_pages,
        "books": [{"id": bid, "title": title} for bid, title in books],
    }


def split_continue_targets(conn: sqlite3.Connection, patch_ids: list[int]) -> dict:
    """Chia patch được chọn thành: cần TTS (gom theo sách), cần đẩy video/upload, bỏ qua."""
    ids = list(dict.fromkeys(int(i) for i in patch_ids))
    tts: dict[int, list[int]] = defaultdict(list)
    publish: list[dict] = []
    skipped: list[dict] = []
    if not ids:
        return {"tts": {}, "publish": [], "skipped": []}
    placeholders = ",".join("?" for _ in ids)
    found = {r["id"]: r for r in _load(conn, f"WHERE p.id IN ({placeholders})", tuple(ids))}
    for patch_id in ids:
        row = found.get(patch_id)
        if row is None:
            skipped.append({"patch_id": patch_id, "reason": "not_found"})
        elif row["phase"] == "queued":
            skipped.append({"patch_id": patch_id, "reason": "already_queued"})
        elif row["phase"] == "complete":
            skipped.append({"patch_id": patch_id, "reason": "complete"})
        elif row["status"] == "done":
            publish.append({"patch_id": patch_id, "book_id": row["book_id"],
                            "audio_path": row["audio_path"]})
        else:
            tts[row["book_id"]].append(patch_id)
    return {"tts": dict(tts), "publish": publish, "skipped": skipped}


def enqueue_tts_targets(conn: sqlite3.Connection, tts: dict[int, list[int]]) -> dict:
    """TTS theo cấu hình hiệu lực của từng sách (không truyền engine) — snapshot
    .tts_request.json của lần chạy dở thắng, nên chunk đã có được dùng lại đúng giọng.
    Cố ý KHÔNG dùng retry_all_failed_patches_for_book: hàm đó xoá thư mục chunk."""
    queued, skipped = 0, []
    for book_id, ids in tts.items():
        try:
            n = enqueue_pending_patch_jobs(conn, book_id, patch_ids=ids, missing_audio_only=True)
        except ValueError as exc:  # engine mặc định không hợp lệ, ...
            skipped.extend({"patch_id": i, "reason": "tts_config_invalid", "error": str(exc)} for i in ids)
            continue
        queued += n
        if n < len(ids):
            # Phần còn lại: đã có audio trên đĩa hoặc trùng dedupe với job live.
            skipped.append({"book_id": book_id, "reason": "tts_not_queued", "count": len(ids) - n})
    return {"queued": queued, "skipped": skipped}


def continue_publish_targets(conn: sqlite3.Connection, publish: list[dict]) -> dict:
    stats = {"enqueued_render": 0, "enqueued_upload": 0, "already_live": 0}
    skipped = []
    for target in publish:
        try:
            outcome = continue_patch_automation(
                conn, target["patch_id"], book_id=target["book_id"], audio_path=target["audio_path"])
        except Exception as exc:  # noqa: BLE001 - một patch lỗi không chặn phần còn lại
            logger.exception("continue automation failed for patch %s", target["patch_id"])
            skipped.append({"patch_id": target["patch_id"], "reason": "error", "error": str(exc)[:500]})
            continue
        if outcome["outcome"] == "skipped":
            skipped.append({"patch_id": target["patch_id"], "reason": outcome["reason"],
                            "error": outcome.get("error")})
        else:
            stats[outcome["outcome"]] += 1
    return {**stats, "skipped": skipped}
