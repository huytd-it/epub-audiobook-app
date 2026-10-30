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
