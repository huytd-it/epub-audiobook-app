"""Ingest chunk WAV đã chép sẵn vào data/books/{book_id}/... qua queue.

Thay vì đẩy 24GB qua HTTP multipart (tràn RAM/starlette/nginx), người vận hành chép
thư mục chunk (0001/chunk_000.wav ...) trực tiếp vào ổ đĩa server — ví dụ:

    data/books/42/_staging/
        0001/chunk_000.wav
        0001/chunk_001.wav
        0015 - Chương 15/chunk_000.wav

Job này quét thư mục đó, chia thành batch ~80MB/~48 file, gọi
chapter_chunk_upload.install_chapter_chunks từng batch, ghi vào
kho chunk theo chương (data/books/{id}/chapters/NNNN). Lỗi ở một batch/chương
chỉ ghi log và bỏ qua — không xóa file nguồn.
"""
from __future__ import annotations

import re
from pathlib import Path

from contextlib import nullcontext

from app import chapter_chunk_upload as upload, repository
from app.config import settings
from app.jobqueue.models import JobFatalError
from app.production_defaults import get_effective_audio_config, get_effective_normalization_options

CHAPTER_DIR = re.compile(r"^(\d+)")
CHUNK_FILE = re.compile(r"^(?:chunk_)?(\d+)\.wav$", re.IGNORECASE)

BATCH_MAX_FILES = 48
BATCH_MAX_BYTES = 80 * 1024 * 1024

# Thư mục hệ thống bên trong data/books/{id} không bao giờ là nguồn ingest
OWN_DIRS = {"chapters", "audio", "videos", "patches", "patch_videos", "processed", "rejected", ".branding", "backup_audio"}


def _find_chapter_groups(root: Path) -> dict[int, dict[int, Path]]:
    """Quét root (đệ quy) tìm chunk_*.wav, nhóm theo chapter_index.

    Trả về {chapter_index: {chunk_index: Path}}
    Chỉ nhận file có cha là thư mục bắt đầu bằng số (0001, 0012 - Tên).
    Khi root chính là data/books/{id}, bỏ qua toàn bộ OWN_DIRS để tránh quét
    kho đã cài (chapters/) hoặc artefact khác.
    """
    grouped: dict[int, dict[int, Path]] = {}
    for wav in root.rglob("*.wav"):
        if not wav.is_file():
            continue
        try:
            rel_parts = wav.relative_to(root).parts
        except ValueError:
            rel_parts = wav.parts
        if any(part in OWN_DIRS for part in rel_parts):
            continue
        parent_name = wav.parent.name
        m_dir = CHAPTER_DIR.match(parent_name)
        m_file = CHUNK_FILE.match(wav.name)
        if not m_dir or not m_file:
            continue
        chapter_no = int(m_dir.group(1))
        if chapter_no < 1:
            continue
        chapter_index = chapter_no - 1
        chunk_index = int(m_file.group(1))
        grouped.setdefault(chapter_index, {})
        if chunk_index not in grouped[chapter_index]:
            grouped[chapter_index][chunk_index] = wav
    return grouped


def _build_batches(grouped: dict[int, dict[int, Path]]) -> list[list[tuple[int, list[tuple[int, Path]]]]]:
    """Chia grouped thành batch ≤ BATCH_MAX_FILES và ≤ BATCH_MAX_BYTES.

    Mỗi element batch là list[(chapter_index, [(chunk_index, Path), ...]), ...]
    Chương quá lớn được tách thành nhiều slice.
    """
    # Chuyển grouped thành list[chapter_index, sorted list[(chunk_index, Path, size)]]
    chapters: list[tuple[int, list[tuple[int, Path, int]]]] = []
    for ci, chunk_map in grouped.items():
        items = []
        for idx, path in sorted(chunk_map.items()):
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            items.append((idx, path, size))
        chapters.append((ci, items))
    chapters.sort(key=lambda x: x[0])

    # Tách chương quá lớn thành slice
    slices: list[tuple[int, list[tuple[int, Path]]]] = []
    for ci, items in chapters:
        if len(items) <= BATCH_MAX_FILES and sum(s for _, _, s in items) <= BATCH_MAX_BYTES:
            slices.append((ci, [(idx, p) for idx, p, _ in items]))
            continue
        offset = 0
        while offset < len(items):
            count = 0
            bytes_sum = 0
            while offset + count < len(items):
                _, _, sz = items[offset + count]
                if count + 1 > BATCH_MAX_FILES or (bytes_sum + sz > BATCH_MAX_BYTES and count > 0):
                    break
                bytes_sum += sz
                count += 1
            if count == 0:
                count = 1
            chunk = [(idx, p) for idx, p, _ in items[offset:offset + count]]
            slices.append((ci, chunk))
            offset += count

    # Bin-pack slices vào batch
    batches: list[list[tuple[int, list[tuple[int, Path]]]]] = []
    cur: list[tuple[int, list[tuple[int, Path]]]] = []
    cur_files = 0
    cur_bytes = 0
    for ci, chunk_list in slices:
        b = sum(p.stat().st_size for _, p in chunk_list) if chunk_list else 0
        # stat lại (đã có nhưng tính nhanh)
        try:
            b = sum(p.stat().st_size for _, p in chunk_list)
        except OSError:
            b = 0
        fits_files = cur_files + len(chunk_list) <= BATCH_MAX_FILES
        fits_bytes = cur_bytes + b <= BATCH_MAX_BYTES
        if cur and (not fits_files or not fits_bytes):
            batches.append(cur)
            cur = []
            cur_files = 0
            cur_bytes = 0
        cur.append((ci, chunk_list))
        cur_files += len(chunk_list)
        cur_bytes += b
    if cur:
        batches.append(cur)
    return batches


def handle(ctx) -> dict:
    payload = ctx.job.payload or {}
    book_id = payload.get("book_id") or ctx.job.book_id
    if book_id is None:
        raise JobFatalError("payload thiếu book_id")
    book_id = int(book_id)
    source_subdir = (payload.get("source_subdir") or "").strip().strip("/\\")
    replace = bool(payload.get("replace"))
    merge_patches = bool(payload.get("merge_patches", True))

    # Xác định root quét
    base = Path(settings.data_root) / "books" / str(book_id)
    if source_subdir:
        # Chặn path traversal
        candidate = (base / source_subdir).resolve()
        try:
            candidate.relative_to(base.resolve())
        except ValueError:
            raise JobFatalError(f"source_subdir không hợp lệ: {source_subdir}")
        root = candidate
    else:
        # Mặc định: thử các vị trí staging phổ biến, fallback về base
        for cand in ["_staging", "incoming", "import", "_chunks_staging"]:
            p = base / cand
            if p.is_dir():
                root = p
                break
        else:
            root = base

    if not root.is_dir():
        raise JobFatalError(f"thư mục nguồn không tồn tại: {root}")

    ctx.log(f"chapter_chunk_ingest book={book_id} root={root} replace={replace} merge={merge_patches}")

    # Load book + config
    book = repository.get_book(ctx.conn, book_id)
    if book is None:
        raise JobFatalError(f"book {book_id} không tồn tại")
    opts = get_effective_normalization_options(ctx.conn, book)
    rules = repository.list_replace_rules(ctx.conn, book_id)
    limit = int(get_effective_audio_config(ctx.conn, book).get("max_chars") or 400)

    grouped = _find_chapter_groups(root)
    if not grouped:
        ctx.log(f"không tìm thấy chunk hợp lệ trong {root} (định dạng 0001/chunk_000.wav)", level=20)
        return {"ok": True, "scanned": str(root), "chapters_found": 0, "installed": 0, "batches": 0, "errors": ["không tìm thấy chunk"]}

    # Lọc chương không tồn tại, kiểm tra patch đang processing
    chapters: dict[int, object] = {}
    busy: set[int] = set()
    for ci in list(grouped.keys()):
        ch = repository.get_chapter(ctx.conn, book_id, ci)
        if ch is None:
            ctx.log(f"bỏ qua chương {ci + 1}: sách không có chương này ({len(grouped[ci])} file)", level=30)
            grouped.pop(ci)
            continue
        chapters[ci] = ch
        if any(p.status == "processing" for p in repository.list_patches_covering_chapter(ctx.conn, book_id, ci)):
            busy.add(ci)

    if not grouped:
        return {"ok": True, "scanned": str(root), "chapters_found": 0, "installed": 0, "batches": 0, "errors": ["không có chương hợp lệ"]}

    batches = _build_batches(grouped)
    total_batches = len(batches)
    total_files = sum(len(chunk_map) for chunk_map in grouped.values())
    ctx.log(f"tìm thấy {len(grouped)} chương · {total_files} file · chia {total_batches} batch (~{BATCH_MAX_BYTES // (1024*1024)}MB/{BATCH_MAX_FILES} file)")
    ctx.progress(0, total_batches, phase="scanning")

    reports: list[dict] = []
    installed_total = 0
    errors: list[str] = []
    touched_chapters: set[int] = set()

    for bi, batch in enumerate(batches):
        if ctx.should_cancel():
            ctx.log("job bị hủy, dừng ingest", level=30)
            break
        ctx.progress(bi, total_batches, phase="ingesting")
        # Gom batch theo chapter_index để gọi install_chapter_chunks từng chương
        # (một batch có thể chứa nhiều slice của nhiều chương, nhưng mỗi chương trong
        # batch chỉ có một slice — install_chapter_chunks sẽ merge với chunk cũ trên đĩa)
        batch_by_chapter: dict[int, list[tuple[int, Path]]] = {}
        for ci, chunk_list in batch:
            batch_by_chapter.setdefault(ci, []).extend(chunk_list)

        for ci, chunk_list in batch_by_chapter.items():
            if ci in busy:
                r = {"chapter_index": ci, "chapter_no": ci + 1, "title": getattr(chapters.get(ci), "title", None),
                     "received": len(chunk_list), "installed": 0, "rejected": [], "missing": [], "complete": False,
                     "status": "error", "detail": "có patch phủ chương đang tạo audio — bỏ qua batch này"}
                reports.append(r)
                errors.append(f"chương {ci+1}: đang processing")
                continue
            chapter = chapters.get(ci)
            if chapter is None:
                continue
            # Mở file handle cho từng chunk trong slice này
            files: dict[int, tuple[str, object]] = {}
            open_handles: list[object] = []
            try:
                for chunk_index, path in chunk_list:
                    try:
                        fh = open(path, "rb")
                        open_handles.append(fh)
                        # label giữ relative path để báo cáo
                        try:
                            label = str(path.relative_to(root))
                        except ValueError:
                            label = path.name
                        files[chunk_index] = (label, fh)
                    except OSError as exc:
                        ctx.log(f"không mở được {path}: {exc}", level=30)
                        errors.append(f"{path.name}: {exc}")
                if not files:
                    continue
                try:
                    report = upload.install_chapter_chunks(book_id, chapter, opts, rules, limit, files, replace=replace)
                except Exception as exc:
                    ctx.log(f"lỗi cài chương {ci+1}: {exc}", level=40)
                    errors.append(f"chương {ci+1}: {exc}")
                    # Không xóa file nguồn — chỉ skip
                    reports.append({"chapter_index": ci, "chapter_no": ci+1, "title": getattr(chapter, "title", None),
                                    "received": len(files), "installed": 0, "rejected": [], "missing": [], "complete": False,
                                    "status": "error", "detail": str(exc)})
                    continue
                reports.append(report)
                if report.get("installed"):
                    installed_total += int(report["installed"])
                    touched_chapters.add(ci)
                if report.get("status") == "error":
                    errors.append(f"chương {ci+1}: {report.get('detail')}")
                ctx.log(f"chương {ci+1}: {report.get('status')} installed={report.get('installed')} missing={report.get('missing')}")
            finally:
                for fh in open_handles:
                    try:
                        fh.close()
                    except Exception:
                        pass
        ctx.progress(bi + 1, total_batches, phase="ingesting")

    ctx.progress(total_batches, total_batches, phase="merging" if merge_patches else "done")

    merged = 0
    merge_details: list[dict] = []
    if merge_patches and touched_chapters:
        # Chỉ ghép những chương đã đủ chunk
        complete = [r["chapter_index"] for r in reports if r.get("complete")]
        if complete:
            from app.patch_publishing import enqueue_patch_video, fetch_thumbnail_inputs, warm_patch_thumbnail
            from app.routes.patches import _result_patch_publish_state

            # Thu thập patch phủ các chương complete
            seen: dict[int, dict] = {}
            for ci in complete:
                for p in repository.list_patches_covering_chapter(ctx.conn, book_id, ci):
                    seen.setdefault(p.id, {"patch_id": p.id, "patch_index": p.patch_index, "name": p.name, "status": p.status})
            # Trạng thái publish hiện tại
            states = {pid: _result_patch_publish_state(ctx.conn, pid) for pid in seen}
            patches = {pid: repository.get_patch(ctx.conn, pid) for pid in seen}
            for pid, info in seen.items():
                patch = patches.get(pid)
                if patch is None or patch.status == "processing":
                    merge_details.append({**info, "result": "skipped", "detail": "patch đang tạo audio"})
                    continue
                if states.get(pid) == "active":
                    merge_details.append({**info, "result": "skipped", "detail": "patch đang tạo/upload video"})
                    continue
                try:
                    # Handler đã giữ conn qua locked_conn; assemble cần một factory trả về context manager.
                    # Dùng nullcontext để không double-lock.
                    audio_path, total = upload.assemble_patch_from_stores(lambda: nullcontext(ctx.conn), patch, limit)
                    try:
                        inputs = fetch_thumbnail_inputs(ctx.conn, patch.id)
                        warm_patch_thumbnail(inputs)
                    except Exception:
                        pass
                    repository.update_patch_chunk_count(ctx.conn, patch.id, total)
                    repository.mark_patch_done(ctx.conn, patch.id, audio_path)
                    try:
                        automation = enqueue_patch_video(ctx.conn, patch.id, force_new=True)
                        info2 = {**info, "result": "merged", "publish_status": automation.get("state")}
                    except Exception as exc:
                        info2 = {**info, "result": "merged", "publish_status": "enqueue_failed", "publish_error": str(exc)}
                    merge_details.append(info2)
                    merged += 1
                except Exception as exc:
                    merge_details.append({**info, "result": "skipped", "detail": str(exc)})

    result = {
        "ok": True,
        "scanned": str(root),
        "chapters_found": len(grouped),
        "batches": total_batches,
        "installed": installed_total,
        "touched_chapters": sorted(touched_chapters),
        "reports": reports,
        "errors": errors[:50],
        "merged": merged,
        "merge_details": merge_details,
    }
    ctx.log(f"ingest xong: installed={installed_total} merged={merged} errors={len(errors)}")
    return result
