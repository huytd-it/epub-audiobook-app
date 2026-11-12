"""Audio TTS theo chương: xem bản chuẩn hoá theo chunk, phát tạm, xoá chunk, ghép lại patch.

Chunk WAV sống trong kho theo chương (app/chunk_store.py), độc lập với patch. Các
endpoint ở đây đọc/xoá trực tiếp kho đó; "ghép lại patch" chỉ reset patch phủ chương
rồi đưa vào hàng đợi TTS — handler sẽ dùng lại mọi chunk còn hợp lệ và chỉ tổng hợp
phần thiếu.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app import audio_merge, chunk_store, repository
from app.deps import locked_conn
from app.production_defaults import get_effective_audio_config, get_effective_normalization_options

router = APIRouter(tags=["chapter-audio"])


def _require_book(conn, book_id: int):
    book = repository.get_book(conn, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy sách.")
    return book


def _require_chapter(conn, book_id: int, chapter_index: int):
    chapter = repository.get_chapter(conn, book_id, chapter_index)
    if chapter is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy chương.")
    return chapter


def _store(book_id: int, chapter_index: int) -> chunk_store.ChunkStore:
    return chunk_store.ChunkStore(chunk_store.get_chapter_chunk_dir(book_id, chapter_index))


def _refuse_if_processing(conn, book_id: int, chapter_indexes: list[int]) -> None:
    """Xoá chunk khi worker đang tổng hợp patch phủ chương sẽ làm hỏng bản ghép."""
    for chapter_index in chapter_indexes:
        for patch in repository.list_patches_covering_chapter(conn, book_id, chapter_index):
            if patch.status == "processing":
                raise HTTPException(
                    status_code=409,
                    detail=f"Patch #{patch.patch_index + 1} đang tạo audio — chờ xong rồi xoá chunk.")


def _affected_patches(conn, book_id: int, chapter_indexes: list[int]) -> list[dict]:
    seen: dict[int, dict] = {}
    for chapter_index in chapter_indexes:
        for patch in repository.list_patches_covering_chapter(conn, book_id, chapter_index):
            seen.setdefault(patch.id, {
                "patch_id": patch.id, "patch_index": patch.patch_index,
                "name": patch.name, "status": patch.status,
            })
    return sorted(seen.values(), key=lambda item: item["patch_index"])


def _chapter_indexes(body) -> list[int]:
    try:
        values = [int(value) for value in (body or {}).get("chapter_indexes") or []]
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="chapter_indexes phải là danh sách số")
    return list(dict.fromkeys(values))


@router.get("/books/{book_id}/chapter-audio")
def chapter_audio_summary(request: Request, book_id: int):
    """Trạng thái kho chunk của mọi chương — chỉ đọc manifest, đủ rẻ cho bảng mục lục."""
    with locked_conn(request) as conn:
        book = _require_book(conn, book_id)
        chapters = repository.list_chapters(conn, book_id)
        opts = get_effective_normalization_options(conn, book)
        rules = repository.list_replace_rules(conn, book_id)
    summary = repository.list_chapter_audio_summary(book_id, chapters, opts, rules)
    return {"chapters": {str(index): item for index, item in summary.items()}}


@router.get("/books/{book_id}/chapter-audio/{chapter_index}")
async def chapter_audio_detail(request: Request, book_id: int, chapter_index: int):
    """Bản chuẩn hoá của chương, tách chunk đúng như lần TTS gần nhất, kèm trạng thái
    từng chunk trên đĩa: done (khớp text) / stale (có file nhưng text đã đổi) / missing."""
    with locked_conn(request) as conn:
        book = _require_book(conn, book_id)
        chapter = _require_chapter(conn, book_id, chapter_index)
        rules = repository.list_replace_rules(conn, book_id)
        opts = get_effective_normalization_options(conn, book)
        audio_config = get_effective_audio_config(conn, book)
        patches = _affected_patches(conn, book_id, [chapter_index])

    store = _store(book_id, chapter_index)
    manifest = store.manifest()
    limit = int(manifest.get("max_chars") or audio_config.get("max_chars") or 400)
    inputs = {"limit": limit, "clean_text": None, "chapters": [chapter], "rules": rules, "opts": opts}
    plan = await asyncio.to_thread(repository.build_chunk_plan_from_inputs, inputs)
    pauses = audio_merge.build_pause_plan(plan, audio_config["chunk_pause_ms"], audio_config["chapter_pause_ms"])

    chunks = []
    for index, (item, pause_ms) in enumerate(zip(plan, pauses)):
        entry = manifest["chunks"].get(str(index)) or {}
        on_disk = store.path(index).is_file()
        if on_disk and entry.get("text") == item["text"]:
            status = "done"
        elif on_disk:
            status = "stale"
        else:
            status = "missing"
        rate = int(entry.get("sample_rate") or 0)
        chunks.append({
            "index": index,
            "text": item["text"],
            "pause_ms": pause_ms,
            "status": status,
            "duration": round(int(entry.get("frames") or 0) / rate, 2) if rate and on_disk else None,
            "url": f"/books/{book_id}/chapter-audio/{chapter_index}/chunks/{index}" if on_disk else None,
        })
    orphans = sum(
        1 for path in (store.dir.glob("chunk_*.wav") if store.dir.is_dir() else [])
        if not path.stem.split("_", 1)[-1].isdigit() or int(path.stem.split("_", 1)[-1]) >= len(plan)
    )
    return {
        "chapter_index": chapter_index,
        "title": plan[0]["chapter_title"] if plan else chapter.title,
        "is_excluded": bool(chapter.is_excluded),
        "max_chars": limit,
        "engine": manifest.get("engine"),
        "voice": manifest.get("voice"),
        "directory": f"{book_id}/chapters/{store.dir.name}",
        "chunks": chunks,
        "orphans": orphans,
        "patches": patches,
    }


@router.get("/books/{book_id}/chapter-audio/{chapter_index}/chunks/{index}")
def serve_chapter_chunk(book_id: int, chapter_index: int, index: int):
    if chapter_index < 0 or index < 0:
        raise HTTPException(status_code=400, detail="index không hợp lệ")
    path = _store(book_id, chapter_index).path(index)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="chunk not found")
    return FileResponse(str(path), media_type="audio/wav")


@router.delete("/books/{book_id}/chapter-audio/{chapter_index}/chunks/{index}")
def delete_chapter_chunk(request: Request, book_id: int, chapter_index: int, index: int):
    with locked_conn(request) as conn:
        _require_chapter(conn, book_id, chapter_index)
        _refuse_if_processing(conn, book_id, [chapter_index])
        affected = _affected_patches(conn, book_id, [chapter_index])
    if index < 0 or not _store(book_id, chapter_index).delete(index):
        raise HTTPException(status_code=404, detail="chunk not found")
    return {"deleted": 1, "affected_patches": affected}


@router.delete("/books/{book_id}/chapter-audio/{chapter_index}")
def delete_chapter_audio(request: Request, book_id: int, chapter_index: int):
    with locked_conn(request) as conn:
        _require_chapter(conn, book_id, chapter_index)
        _refuse_if_processing(conn, book_id, [chapter_index])
        affected = _affected_patches(conn, book_id, [chapter_index])
    return {"deleted": _store(book_id, chapter_index).clear(), "affected_patches": affected}


@router.post("/books/{book_id}/chapter-audio/delete")
async def delete_chapters_audio(request: Request, book_id: int):
    indexes = _chapter_indexes(await request.json())
    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        _refuse_if_processing(conn, book_id, indexes)
        affected = _affected_patches(conn, book_id, indexes)
    deleted = sum(_store(book_id, index).clear() for index in indexes if index >= 0)
    return {"deleted": deleted, "chapters": len(indexes), "affected_patches": affected}


@router.post("/books/{book_id}/chapter-audio/rebuild-patches")
async def rebuild_chapter_patches(request: Request, book_id: int):
    """Ghép lại các patch phủ những chương này: reset audio/video của patch (chunk theo
    chương được giữ nguyên) rồi đưa vào hàng đợi TTS với giọng đã lưu của patch, hoặc
    cấu hình sách. Job chỉ tổng hợp chunk thiếu/lệch, phần còn lại chỉ là ghép."""
    from app.jobqueue.backfill import enqueue_pending_patch_jobs

    indexes = _chapter_indexes(await request.json())
    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        affected = _affected_patches(conn, book_id, indexes)
        reset_ids = [item["patch_id"] for item in affected
                     if item["status"] != "processing" and repository.reset_patch(conn, item["patch_id"])]
        queued = enqueue_pending_patch_jobs(conn, book_id, patch_ids=reset_ids) if reset_ids else 0
    return {
        "queued": queued,
        "patches": [item for item in affected if item["patch_id"] in reset_ids],
        "skipped_processing": sum(1 for item in affected if item["status"] == "processing"),
    }


# Một lượt chọn thư mục có thể mang hàng nghìn chunk: frontend chia lô theo chương,
# nhưng vẫn nới trần mặc định 1000 file của Starlette cho chương rất dài.
_UPLOAD_MAX_FILES = 5000


@router.post("/books/{book_id}/chapter-audio/upload")
async def upload_chapter_chunks(request: Request, book_id: int):
    """Nạp thư mục chunk WAV làm sẵn vào kho chunk theo chương.

    multipart: ``files`` (tên file = đường dẫn tương đối ``…/0001/chunk_000.wav``, xem
    app/chapter_chunk_upload.py), ``replace`` ("true" = xoá kho của chương trước khi
    nạp). Chunk tách theo chuẩn hoá + max_chars hiện tại của sách. Mỗi chương báo cáo
    riêng; một chương lỗi không chặn chương khác."""
    from app import chapter_chunk_upload as upload

    form = await request.form(max_files=_UPLOAD_MAX_FILES)
    replace = str(form.get("replace") or "").lower() in {"1", "true", "on", "yes"}
    rejected: list[dict] = []
    grouped: dict[int, dict[int, tuple[str, object]]] = {}
    for item in form.getlist("files"):
        if not hasattr(item, "file"):
            continue
        label = item.filename or ""
        parsed = upload.parse_upload_path(label)
        if isinstance(parsed, str):
            rejected.append({"filename": label, "reason": parsed})
            continue
        chapter_index, chunk_index = parsed
        if chunk_index in grouped.setdefault(chapter_index, {}):
            rejected.append({"filename": label, "reason": f"trùng chunk {chunk_index:03d} của chương {chapter_index + 1}"})
            continue
        grouped[chapter_index][chunk_index] = (label, item.file)

    with locked_conn(request) as conn:
        book = _require_book(conn, book_id)
        opts = get_effective_normalization_options(conn, book)
        rules = repository.list_replace_rules(conn, book_id)
        limit = int(get_effective_audio_config(conn, book).get("max_chars") or 400)
        chapters = {}
        busy = set()
        for chapter_index in grouped:
            chapter = repository.get_chapter(conn, book_id, chapter_index)
            if chapter is not None:
                chapters[chapter_index] = chapter
                if any(p.status == "processing"
                       for p in repository.list_patches_covering_chapter(conn, book_id, chapter_index)):
                    busy.add(chapter_index)

    reports = []
    try:
        for chapter_index in sorted(grouped):
            files = grouped[chapter_index]
            chapter = chapters.get(chapter_index)
            base = {"chapter_index": chapter_index, "chapter_no": chapter_index + 1,
                    "received": len(files), "installed": 0, "rejected": [], "missing": [], "complete": False}
            if chapter is None:
                reports.append({**base, "status": "error", "detail": f"sách không có chương {chapter_index + 1}"})
                continue
            if chapter_index in busy:
                reports.append({**base, "title": chapter.title, "status": "error",
                                "detail": "có patch phủ chương đang tạo audio — chờ xong rồi nạp"})
                continue
            reports.append(await asyncio.to_thread(
                upload.install_chapter_chunks, book_id, chapter, opts, rules, limit, files, replace=replace))
    finally:
        await form.close()

    with locked_conn(request) as conn:
        touched = [r["chapter_index"] for r in reports if r.get("installed")]
        affected = _affected_patches(conn, book_id, touched)
    return {
        "max_chars": limit,
        "chapters": reports,
        "rejected": rejected,
        "installed": sum(int(r.get("installed") or 0) for r in reports),
        "affected_patches": affected,
    }


@router.post("/books/{book_id}/chapter-audio/merge-patches")
async def merge_patches_from_chapter_chunks(request: Request, book_id: int):
    """Ghép trực tiếp các patch phủ những chương này từ kho chunk theo chương (không qua
    hàng đợi TTS, nên chunk nạp từ ngoài không bị tổng hợp lại). Patch còn thiếu chunk
    được bỏ qua kèm lý do; patch ghép xong được đánh dấu done rồi qua cổng tự động hoá
    video như khi import kết quả."""
    from app import chapter_chunk_upload as upload
    from app.patch_publishing import enqueue_patch_video, fetch_thumbnail_inputs, warm_patch_thumbnail
    from app.routes.patches import _result_patch_publish_state

    indexes = _chapter_indexes(await request.json())
    with locked_conn(request) as conn:
        book = _require_book(conn, book_id)
        limit = int(get_effective_audio_config(conn, book).get("max_chars") or 400)
        affected = _affected_patches(conn, book_id, indexes)
        states = {item["patch_id"]: _result_patch_publish_state(conn, item["patch_id"]) for item in affected}
        patches = {item["patch_id"]: repository.get_patch(conn, item["patch_id"]) for item in affected}

    results = []
    for item in affected:
        row = dict(item)
        patch = patches[item["patch_id"]]
        if patch is None or patch.status == "processing":
            results.append({**row, "result": "skipped", "detail": "patch đang tạo audio"})
            continue
        if states[patch.id] == "active":
            results.append({**row, "result": "skipped", "detail": "patch đang tạo hoặc upload video"})
            continue
        try:
            audio_path, total = await asyncio.to_thread(
                upload.assemble_patch_from_stores, lambda: locked_conn(request), patch, limit)
        except (upload.ChapterChunkUploadError, OSError, RuntimeError) as exc:
            results.append({**row, "result": "skipped", "detail": str(exc)})
            continue
        with locked_conn(request) as conn:
            inputs = fetch_thumbnail_inputs(conn, patch.id)
        await asyncio.to_thread(warm_patch_thumbnail, inputs)
        with locked_conn(request) as conn:
            repository.update_patch_chunk_count(conn, patch.id, total)
            repository.mark_patch_done(conn, patch.id, audio_path)
            try:
                automation = enqueue_patch_video(conn, patch.id, force_new=True)
                row["publish_status"] = automation.get("state")
            except Exception as exc:  # audio đã cài, video có thể chạy lại thủ công
                row["publish_status"] = "enqueue_failed"
                row["publish_error"] = str(exc)
        results.append({**row, "result": "merged"})
    return {
        "merged": sum(1 for r in results if r["result"] == "merged"),
        "patches": results,
    }
