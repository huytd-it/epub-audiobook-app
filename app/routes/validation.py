"""Validation + incremental EPUB update endpoints.

Validation answers "will this book survive TTS?" before any GPU time is spent; the
re-import endpoints answer "the source EPUB gained chapters, how do I take them without
rebuilding the book?".
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app import repository
from app.config import settings
from app.deps import locked_conn
from app.epub_parser import parse_epub
from app.validation import (
    TITLE_FIXABLE,
    analyze_chapter_spans,
    analyze_numbering,
    canonical_chapter_title,
    check_title_format,
    numbering_flags,
    summarize,
    summarize_spans,
    validate_chapter_text,
    validate_chapters,
    validate_patch_plan,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["validation"])


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


async def _recompute_covering_patches(request: Request, book_id: int, chapter_index: int, limit: int) -> list[dict]:
    """Recompute chunk_count for every patch covering chapter_index whose plan actually
    depends on chapter text — skips patches with a Text Studio override (clean_text)
    and patches currently being synthesized (worker-owned)."""
    with locked_conn(request) as conn:
        candidates = [
            patch
            for patch in repository.list_patches_covering_chapter(conn, book_id, chapter_index)
            if not patch.clean_text and patch.status != "processing"
        ]
        plan_inputs = {patch.id: repository.fetch_patch_chunk_inputs(conn, patch, limit) for patch in candidates}

    results = []
    for patch in candidates:
        chunk_count = len(await asyncio.to_thread(repository.build_chunk_plan_from_inputs, plan_inputs[patch.id]))
        results.append({"patch_id": patch.id, "patch_index": patch.patch_index, "chunk_count": chunk_count})

    if results:
        with locked_conn(request) as conn:
            for item in results:
                repository.update_patch_chunk_count(conn, item["patch_id"], item["chunk_count"])

    return results


def _chapter_patch_summaries(conn, book_id: int, chapter_index: int) -> list[dict]:
    return [
        {
            "patch_id": patch.id,
            "patch_index": patch.patch_index,
            "name": patch.name,
            "status": patch.status,
            "has_clean_text": bool(patch.clean_text),
            "chunk_count": patch.chunk_count,
        }
        for patch in repository.list_patches_covering_chapter(conn, book_id, chapter_index)
    ]


@router.get("/books/{book_id}/validation")
def book_validation(request: Request, book_id: int, max_chars: int | None = None):
    """Validate every chapter and every patch of a book.

    Patch validation runs the real chunk plan, so what it measures is exactly what the
    TTS worker would be asked to speak.
    """
    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        repository.backfill_chapter_metadata(conn, book_id)
        chapters = repository.list_chapters(conn, book_id)
        patches = repository.list_patches(conn, book_id)
        limit = max_chars or settings.tts_max_chars
        plans = {
            patch.id: repository.fetch_patch_chunk_inputs(conn, patch, limit)
            for patch in patches
        }

    chapter_reports = validate_chapters(chapters, max_chars=limit)
    patch_reports = []
    for patch in patches:
        plan = repository.build_chunk_plan_from_inputs(plans[patch.id])
        patch_reports.append(
            validate_patch_plan(
                patch_id=patch.id,
                patch_index=patch.patch_index,
                chapter_start=patch.chapter_start,
                chapter_end=patch.chapter_end,
                plan=plan,
                max_chars=limit,
                chapter_reports=chapter_reports,
            )
        )

    return JSONResponse(
        {
            "book_id": book_id,
            "max_chars": limit,
            "summary": summarize(chapter_reports, patch_reports),
            "numbering": analyze_numbering(chapter_reports),
            "chapters": [
                report.as_dict() for report in chapter_reports if report.severity != "ok"
            ],
            "patches": [report.as_dict() for report in patch_reports],
        }
    )


@router.get("/books/{book_id}/patches/{patch_id}/validation")
def patch_validation(request: Request, book_id: int, patch_id: int, max_chars: int | None = None):
    """Per-chunk detail for one patch: every chunk that would break TTS, with its text."""
    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="Không tìm thấy patch.")
        limit = max_chars or patch.max_chars or settings.tts_max_chars
        inputs = repository.fetch_patch_chunk_inputs(conn, patch, limit)
        chapters = repository.get_chapters_in_range(conn, book_id, patch.chapter_start, patch.chapter_end)

    plan = repository.build_chunk_plan_from_inputs(inputs)
    chapter_reports = validate_chapters(chapters, max_chars=limit)
    report = validate_patch_plan(
        patch_id=patch.id,
        patch_index=patch.patch_index,
        chapter_start=patch.chapter_start,
        chapter_end=patch.chapter_end,
        plan=plan,
        max_chars=limit,
        chapter_reports=chapter_reports,
    )

    hard_limit = int(limit * 1.5)
    bad_chunks = [
        {
            "index": index,
            "chars": len(chunk.get("text") or ""),
            "chapter_index": chunk.get("chapter_index"),
            "reason": (
                "empty"
                if not (chunk.get("text") or "").strip()
                else "oversized"
                if len(chunk.get("text") or "") > hard_limit
                else "unspeakable"
            ),
            "excerpt": (chunk.get("text") or "")[:200],
        }
        for index, chunk in enumerate(plan)
        if not (chunk.get("text") or "").strip()
        or len(chunk.get("text") or "") > hard_limit
        or not any(character.isalnum() for character in (chunk.get("text") or ""))
    ]

    return JSONResponse(
        {
            **report.as_dict(),
            "chapters": [item.as_dict() for item in chapter_reports if item.severity != "ok"],
            "bad_chunks": bad_chunks[:100],
        }
    )


@router.get("/books/{book_id}/reimport/preview")
def reimport_preview(request: Request, book_id: int):
    """Diff the book's stored EPUB against its chapters without writing anything."""
    with locked_conn(request) as conn:
        book = _require_book(conn, book_id)
        repository.backfill_chapter_metadata(conn, book_id)
        epub_path = book.epub_path

    if not epub_path or not Path(epub_path).exists():
        raise HTTPException(status_code=400, detail="Không tìm thấy file EPUB gốc của sách.")

    parsed = parse_epub(epub_path)
    with locked_conn(request) as conn:
        plan = repository.diff_chapters_against_epub(conn, book_id, parsed)
    return JSONResponse(plan)


@router.post("/books/{book_id}/reimport")
async def reimport(
    request: Request,
    book_id: int,
    epub_file: UploadFile | None = File(default=None),
    update_changed: bool = Form(default=False),
):
    """Take new chapters from an EPUB (uploaded, or the book's stored one).

    Chapters already in the book keep their index, so existing patches — and the audio
    already rendered for them — stay valid. Chapters whose text changed are only rewritten
    with update_changed, and never when a completed patch covers them.
    """
    with locked_conn(request) as conn:
        book = _require_book(conn, book_id)
        repository.backfill_chapter_metadata(conn, book_id)
        stored_path = book.epub_path

    uploaded_path: Path | None = None
    if epub_file is not None and epub_file.filename:
        uploads_dir = Path(settings.data_root) / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        uploaded_path = uploads_dir / f"_tmp_reimport_{book_id}.epub"
        with open(uploaded_path, "wb") as handle:
            shutil.copyfileobj(epub_file.file, handle)
        source = uploaded_path
    else:
        if not stored_path or not Path(stored_path).exists():
            raise HTTPException(status_code=400, detail="Không tìm thấy file EPUB gốc của sách.")
        source = Path(stored_path)

    try:
        parsed = parse_epub(str(source))
        with locked_conn(request) as conn:
            result = repository.append_new_chapters(
                conn, book_id, parsed, update_changed=bool(update_changed)
            )
        # An uploaded EPUB becomes the book's source once its chapters are merged in.
        if uploaded_path is not None and result["inserted"]:
            final_path = Path(stored_path) if stored_path else uploaded_path
            shutil.move(str(uploaded_path), str(final_path))
            uploaded_path = None
            with locked_conn(request) as conn:
                conn.execute("UPDATE book SET epub_path = ? WHERE id = ?", (str(final_path), book_id))
                conn.commit()
    finally:
        if uploaded_path is not None:
            uploaded_path.unlink(missing_ok=True)

    return JSONResponse(result)


@router.get("/books/{book_id}/patches/extend/preview")
def extend_preview(request: Request, book_id: int, patch_size: int | None = None):
    """Patches that would be appended for chapters no patch covers yet."""
    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        try:
            planned = repository.preview_extend_patches(conn, book_id, patch_size)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
        uncovered = repository.uncovered_chapter_indices(conn, book_id)
    return JSONResponse({"patches": planned, "uncovered_chapters": len(uncovered)})


@router.post("/books/{book_id}/patches/extend")
def extend(request: Request, book_id: int, patch_size: int | None = Form(default=None)):
    """Append patches for uncovered chapters without touching existing patches or audio."""
    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        try:
            created = repository.extend_patches(conn, book_id, patch_size)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
    return JSONResponse(
        {
            "created": len(created),
            "patch_indices": [patch.patch_index for patch in created],
        }
    )


# ---------------------------------------------------------------------------
# Chapter-level validation, detail, edit — powers the Mục lục tab.
# ---------------------------------------------------------------------------


@router.get("/books/{book_id}/chapters/validation")
async def chapters_validation(request: Request, book_id: int, max_chars: int | None = None):
    """Validate every chapter — cheap enough to run on every page load, unlike
    /books/{id}/validation which also builds every patch's chunk plan."""
    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        repository.backfill_chapter_metadata(conn, book_id)
        chapters = repository.list_chapters(conn, book_id)
        limit = max_chars or settings.tts_max_chars

    reports = await asyncio.to_thread(validate_chapters, chapters, max_chars=limit)
    flags = numbering_flags(reports)

    titles = {"canonical": 0, "fixable": 0, "no_name": 0, "unknown": 0}
    for report in reports:
        titles[report.title_state] = titles.get(report.title_state, 0) + 1

    return JSONResponse(
        {
            "book_id": book_id,
            "max_chars": limit,
            "summary": summarize(reports, []),
            "numbering": analyze_numbering(reports),
            "titles": titles,
            "chapters": [
                {**report.as_dict(), "numbering_flag": flags.get(report.chapter_index)}
                for report in reports
            ],
        }
    )


@router.get("/books/{book_id}/chapters/{chapter_index}")
async def chapter_detail(request: Request, book_id: int, chapter_index: int, analyze: int = 1):
    """Full chapter text + validation report + highlight spans, for the edit modal."""
    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        chapter = _require_chapter(conn, book_id, chapter_index)
        limit = settings.tts_max_chars
        patches = _chapter_patch_summaries(conn, book_id, chapter_index)

    report = await asyncio.to_thread(
        validate_chapter_text,
        chapter_index=chapter.chapter_index,
        title=chapter.title,
        text=chapter.text,
        is_excluded=chapter.is_excluded,
        max_chars=limit,
    )
    spans = await asyncio.to_thread(
        analyze_chapter_spans, chapter.text, max_chars=limit, include_style=bool(analyze)
    )

    return JSONResponse(
        {
            "id": chapter.id,
            "chapter_index": chapter.chapter_index,
            "title": chapter.title,
            "text": chapter.text,
            "char_count": chapter.char_count,
            "is_excluded": chapter.is_excluded,
            "chapter_no": chapter.chapter_no,
            "title_state": report.title_state,
            "suggested_title": report.suggested_title,
            "max_chars": limit,
            "report": report.as_dict(),
            "spans": [span.as_dict() for span in spans],
            "span_totals": summarize_spans(spans),
            "patches": patches,
        }
    )


@router.put("/books/{book_id}/chapters/{chapter_index}")
async def update_chapter(request: Request, book_id: int, chapter_index: int):
    """Save chapter edits and recompute every derived field + covering patches' chunk
    counts, so the numbers shown afterwards are never stale."""
    body = await request.json()
    title = body.get("title")
    text = body.get("text")
    is_excluded = body.get("is_excluded")
    if title is not None and len(title) > 400:
        raise HTTPException(status_code=400, detail="Tiêu đề quá dài (tối đa 400 ký tự).")

    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        _require_chapter(conn, book_id, chapter_index)
        repository.update_chapter(
            conn, book_id, chapter_index, title=title, text=text, is_excluded=is_excluded
        )
        chapter = repository.get_chapter(conn, book_id, chapter_index)
        limit = settings.tts_max_chars

    patches_recomputed = await _recompute_covering_patches(request, book_id, chapter_index, limit)

    report = await asyncio.to_thread(
        validate_chapter_text,
        chapter_index=chapter.chapter_index,
        title=chapter.title,
        text=chapter.text,
        is_excluded=chapter.is_excluded,
        max_chars=limit,
    )
    spans = await asyncio.to_thread(analyze_chapter_spans, chapter.text, max_chars=limit)

    with locked_conn(request) as conn:
        patches = _chapter_patch_summaries(conn, book_id, chapter_index)

    return JSONResponse(
        {
            "ok": True,
            "title": chapter.title,
            "text": chapter.text,
            "char_count": chapter.char_count,
            "is_excluded": chapter.is_excluded,
            "chapter_no": chapter.chapter_no,
            "title_state": report.title_state,
            "suggested_title": report.suggested_title,
            "report": report.as_dict(),
            "spans": [span.as_dict() for span in spans],
            "span_totals": summarize_spans(spans),
            "patches": patches,
            "patches_recomputed": patches_recomputed,
        }
    )


@router.post("/books/{book_id}/chapters/{chapter_index}/analyze")
async def analyze_chapter_draft(request: Request, book_id: int, chapter_index: int):
    """Re-analyse a draft title/text without writing anything — powers "Phân tích lại"
    while the user is still editing."""
    try:
        body = await request.json()
    except Exception:
        body = {}

    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        chapter = _require_chapter(conn, book_id, chapter_index)
        limit = settings.tts_max_chars

    title = body.get("title") if body.get("title") is not None else chapter.title
    text = body.get("text") if body.get("text") is not None else chapter.text

    report = await asyncio.to_thread(
        validate_chapter_text,
        chapter_index=chapter_index,
        title=title,
        text=text,
        is_excluded=chapter.is_excluded,
        max_chars=limit,
    )
    spans = await asyncio.to_thread(analyze_chapter_spans, text, max_chars=limit)

    return JSONResponse(
        {
            "report": report.as_dict(),
            "spans": [span.as_dict() for span in spans],
            "span_totals": summarize_spans(spans),
            "title_state": report.title_state,
            "suggested_title": report.suggested_title,
        }
    )


@router.get("/books/{book_id}/chapters/title-normalize/preview")
async def title_normalize_preview(request: Request, book_id: int):
    """Which chapter titles can be auto-rewritten to "Chương N: Tên", and which ones
    need a manual fix because no name or no number could be found."""
    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        chapters = repository.list_chapters(conn, book_id)

    items = []
    skipped_items = []
    for chapter in chapters:
        state, number, name = check_title_format(chapter.title)
        if state == TITLE_FIXABLE:
            items.append(
                {
                    "chapter_index": chapter.chapter_index,
                    "current": chapter.title,
                    "suggested": canonical_chapter_title(number, name),
                    "chapter_no": number,
                }
            )
        elif state != "canonical":
            skipped_items.append(
                {"chapter_index": chapter.chapter_index, "title": chapter.title, "reason": state}
            )

    return JSONResponse(
        {
            "total": len(chapters),
            "fixable": len(items),
            "skipped": len(skipped_items),
            "items": items,
            "skipped_items": skipped_items,
        }
    )


@router.post("/books/{book_id}/chapters/title-normalize")
async def title_normalize_apply(request: Request, book_id: int):
    """Rewrite the given chapters' titles to the canonical "Chương N: Tên" shape.

    The server recomputes each suggestion from the stored title — it never trusts a
    client-supplied title — and silently skips any index that isn't actually fixable,
    so a stale client-side preview can't widen what gets written.
    """
    body = await request.json()
    indices = body.get("chapter_indices") or []
    if not isinstance(indices, list) or not indices:
        raise HTTPException(status_code=400, detail="Chưa chọn chương nào.")

    with locked_conn(request) as conn:
        _require_book(conn, book_id)
        chapters = {c.chapter_index: c for c in repository.get_chapters_by_indices(conn, book_id, indices)}

        updates: list[tuple[int, str]] = []
        for chapter_index in indices:
            chapter = chapters.get(chapter_index)
            if chapter is None:
                continue
            state, number, name = check_title_format(chapter.title)
            if state == TITLE_FIXABLE:
                updates.append((chapter_index, canonical_chapter_title(number, name)))

        repository.set_chapter_titles(conn, book_id, updates)
        limit = settings.tts_max_chars

    patches_recomputed: list[dict] = []
    seen_patch_ids: set[int] = set()
    for chapter_index, _ in updates:
        for item in await _recompute_covering_patches(request, book_id, chapter_index, limit):
            if item["patch_id"] not in seen_patch_ids:
                seen_patch_ids.add(item["patch_id"])
                patches_recomputed.append(item)

    return JSONResponse(
        {
            "updated": len(updates),
            "chapters": [{"chapter_index": index, "title": title} for index, title in updates],
            "patches_recomputed": patches_recomputed,
        }
    )
