from __future__ import annotations

import logging
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import repository
from app.config import settings
from app.deps import locked_conn
from app.epub_parser import parse_epub

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
logger = logging.getLogger(__name__)


@router.get("/books", response_class=HTMLResponse)
def list_books(request: Request):
    with locked_conn(request) as conn:
        books = repository.list_books(conn)
        patch_counts = {
            b.id: {
                "total": len(repository.list_patches(conn, b.id)),
                "done": sum(1 for p in repository.list_patches(conn, b.id) if p.status == "done"),
                "pending": sum(1 for p in repository.list_patches(conn, b.id) if p.status == "pending"),
            }
            for b in books
        }
    return templates.TemplateResponse(
        request, "book_list.html", {"books": books, "patch_counts": patch_counts}
    )


@router.get("/books/upload", response_class=HTMLResponse)
def upload_form(request: Request):
    return templates.TemplateResponse(request, "upload.html", {})


@router.post("/books/upload")
async def upload_book(
    request: Request,
    epub_file: UploadFile = File(...),
    patch_size: int = Form(default=10),
    background_image: UploadFile | None = File(default=None),
    voice_clip: UploadFile | None = File(default=None),
    voice_transcript: str | None = Form(default=None),
):
    uploads_dir = Path(settings.data_root) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    tmp_epub_path = uploads_dir / f"_tmp_{epub_file.filename}"
    with open(tmp_epub_path, "wb") as f:
        shutil.copyfileobj(epub_file.file, f)

    tmp_bg_path = None
    if background_image is not None and background_image.filename:
        tmp_bg_path = uploads_dir / f"_tmp_bg_{background_image.filename}"
        with open(tmp_bg_path, "wb") as f:
            shutil.copyfileobj(background_image.file, f)

    tmp_voice_path = None
    if voice_clip is not None and voice_clip.filename:
        tmp_voice_path = uploads_dir / f"_tmp_voice_{voice_clip.filename}"
        with open(tmp_voice_path, "wb") as f:
            shutil.copyfileobj(voice_clip.file, f)

    chapters = parse_epub(str(tmp_epub_path))
    title = Path(epub_file.filename).stem

    with locked_conn(request) as conn:
        book = repository.create_book(
            conn,
            title=title,
            original_filename=epub_file.filename,
            epub_path="",  # finalized below once the book id (and thus its folder name) is known
            patch_size=patch_size,
            chapters=chapters,
            background_image_path=None,
            voice_transcript=voice_transcript or None,
        )

        final_epub_path = uploads_dir / f"{book.id}.epub"
        tmp_epub_path.rename(final_epub_path)

        final_bg_path = None
        if tmp_bg_path is not None:
            final_bg_path = uploads_dir / f"{book.id}_bg{Path(tmp_bg_path).suffix}"
            tmp_bg_path.rename(final_bg_path)

        final_voice_path = None
        if tmp_voice_path is not None:
            final_voice_path = uploads_dir / f"{book.id}_voice{Path(tmp_voice_path).suffix}"
            tmp_voice_path.rename(final_voice_path)

        conn.execute(
            "UPDATE book SET epub_path = ?, background_image_path = ?, voice_clip_path = ? WHERE id = ?",
            (
                str(final_epub_path),
                str(final_bg_path) if final_bg_path else None,
                str(final_voice_path) if final_voice_path else None,
                book.id,
            ),
        )
        conn.commit()

    return RedirectResponse(url=f"/books/{book.id}", status_code=303)


@router.get("/books/{book_id}", response_class=HTMLResponse)
def book_detail(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        patch_list = repository.list_patches(conn, book_id)
        rules = repository.list_replace_rules(conn, book_id)
        chapters = repository.list_chapters(conn, book_id)
        last_error = repository.get_last_error_for_book(conn, book_id)
        video_job = repository.get_book_job(conn, book_id, "video")
    has_active_patches = any(p.status in ("pending", "processing") for p in patch_list)
    return templates.TemplateResponse(
        request, "book_detail.html", {
            "book": book,
            "patches": patch_list,
            "rules": rules,
            "chapters": chapters,
            "last_error": last_error,
            "video_job": video_job,
            "has_active_patches": has_active_patches,
        }
    )


@router.post("/books/{book_id}/video")
def trigger_video(request: Request, book_id: int):
    """Enqueue a video book_job. Video generation is now handled by the worker
    (background, non-blocking). If the book has no final audio yet, or a video
    book_job already exists in any status, this is a no-op that just redirects."""
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None or not book.final_audio_path:
            return RedirectResponse(url=f"/books/{book_id}", status_code=303)
        if not book.background_image_path:
            return RedirectResponse(url=f"/books/{book_id}", status_code=303)
        existing = repository.get_book_job(conn, book_id, "video")
        if existing is not None:
            return RedirectResponse(url=f"/books/{book_id}", status_code=303)
        repository.enqueue_book_job(conn, book_id, "video")

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/delete")
def delete_book(request: Request, book_id: int):
    with locked_conn(request) as conn:
        ok = repository.delete_book(conn, book_id, settings.data_root)
    if not ok:
        raise HTTPException(status_code=404, detail=f"book {book_id} not found")
    return RedirectResponse(url="/books", status_code=303)


def _parse_ids(raw: str | None) -> list[int]:
    """Parse a comma-separated list of integer ids, ignoring empty / non-integer tokens."""
    if not raw:
        return []
    out: list[int] = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.append(int(token))
        except ValueError:
            continue
    return out


@router.get("/books/{book_id}/chapters/preview")
def preview_chapters(
    request: Request,
    book_id: int,
    ids: str | None = Query(default=None, description="Comma-separated chapter_index values"),
    preview_chars: int = Query(default=500, ge=1, le=100_000),
):
    """Return a JSON list of {chapter_index, title, char_count, text_excerpt} for the
    requested chapters. Unknown indices are silently skipped."""
    if ids is None or ids.strip() == "":
        raise HTTPException(status_code=400, detail="'ids' query parameter is required")

    indices = _parse_ids(ids)
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        chapters = repository.get_chapters_by_indices(conn, book_id, indices)

    return JSONResponse([
        {
            "chapter_index": ch.chapter_index,
            "title": ch.title,
            "char_count": ch.char_count,
            "text_excerpt": ch.text[:preview_chars],
        }
        for ch in chapters
    ])


@router.get("/books/{book_id}/chapters/{chapter_index}/text", response_class=PlainTextResponse)
def get_chapter_text(request: Request, book_id: int, chapter_index: int):
    """Return the full text of a single chapter as text/plain."""
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        text = repository.get_chapter_text(conn, book_id, chapter_index)
    if text is None:
        raise HTTPException(status_code=404, detail=f"chapter {chapter_index} not found")
    return PlainTextResponse(text)


@router.get("/books/{book_id}/chapters/preview-ui", response_class=HTMLResponse)
def preview_chapters_ui(
    request: Request,
    book_id: int,
    ids: str | None = Query(default=None),
    range_start: int | None = Query(default=None),
    range_end: int | None = Query(default=None),
):
    """Server-rendered preview page. Selection sources, in priority order:
    1. `ids` (comma-separated chapter_index values, possibly with a range to expand)
    2. `range_start` + `range_end` (inclusive indices)
    3. Individual checkboxes submitted as repeated `ids` values
    """
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        all_chapters = repository.list_chapters(conn, book_id)

    requested: list[int] = []
    if ids:
        requested.extend(_parse_ids(ids))
    if range_start is not None and range_end is not None and range_end >= range_start:
        requested.extend(range(range_start, range_end + 1))

    seen: set[int] = set()
    selected_indices: list[int] = []
    for idx in requested:
        if idx not in seen:
            seen.add(idx)
            selected_indices.append(idx)
    selected_indices.sort()

    previewed: list = []
    if selected_indices:
        with locked_conn(request) as conn:
            previewed = repository.get_chapters_by_indices(conn, book_id, selected_indices)

    return templates.TemplateResponse(
        request,
        "chapter_preview.html",
        {
            "book": book,
            "all_chapters": all_chapters,
            "previewed": previewed,
            "selected_indices": selected_indices,
            "range_start": range_start,
            "range_end": range_end,
        },
    )


# ---------------------------------------------------------------------------
# Chapter exclude
# ---------------------------------------------------------------------------


@router.post("/books/{book_id}/chapters/{chapter_index}/exclude")
def toggle_chapter_exclude(
    request: Request,
    book_id: int,
    chapter_index: int,
    excluded: str = Form(default="true"),
):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        repository.set_chapter_excluded(
            conn, book_id, chapter_index, excluded.lower() != "false"
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Replace rules
# ---------------------------------------------------------------------------


@router.get("/books/{book_id}/replace-rules")
def list_rules(request: Request, book_id: int):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        rules = repository.list_replace_rules(conn, book_id)
    return JSONResponse([
        {"id": r.id, "book_id": r.book_id, "find": r.find, "replace": r.replace,
         "is_regex": r.is_regex, "position": r.position}
        for r in rules
    ])


@router.post("/books/{book_id}/replace-rules")
def create_rule(
    request: Request,
    book_id: int,
    find: str = Form(...),
    replace: str = Form(default=""),
    is_regex: str = Form(default="false"),
    position: int = Form(default=0),
):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        try:
            rule = repository.create_replace_rule(
                conn, book_id, find, replace, is_regex.lower() == "true", position
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        repository.reset_done_patches_for_book(conn, book_id)
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/replace-rules/{rule_id}/edit")
def edit_rule(
    request: Request,
    book_id: int,
    rule_id: int,
    find: str = Form(...),
    replace: str = Form(default=""),
    is_regex: str = Form(default="false"),
    position: int = Form(default=0),
):
    with locked_conn(request) as conn:
        try:
            updated = repository.update_replace_rule(
                conn, rule_id, find=find, replace=replace,
                is_regex=is_regex.lower() == "true", position=position,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if updated is None:
            raise HTTPException(status_code=404, detail=f"rule {rule_id} not found")
        repository.reset_done_patches_for_book(conn, book_id)
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/replace-rules/{rule_id}/delete")
def delete_rule(request: Request, book_id: int, rule_id: int):
    with locked_conn(request) as conn:
        if repository.delete_replace_rule(conn, rule_id):
            repository.reset_done_patches_for_book(conn, book_id)
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


# ---------------------------------------------------------------------------
# Patch rebuild + preview actions
# ---------------------------------------------------------------------------


@router.post("/books/{book_id}/patches/rebuild")
async def rebuild_patches(request: Request, book_id: int):
    body = await request.json()
    ranges_raw = body.get("ranges", [])
    reset_done = body.get("reset_done", True)
    ranges: list[tuple[int, int]] = []
    for item in ranges_raw:
        if isinstance(item, list) and len(item) == 2:
            ranges.append((item[0], item[1]))
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        try:
            patches = repository.rebuild_patches(conn, book_id, ranges, reset_done)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse([
        {"patch_index": p.patch_index, "chapter_start": p.chapter_start,
         "chapter_end": p.chapter_end, "name": p.name, "chunk_count": p.chunk_count,
         "status": p.status}
        for p in patches
    ])


@router.post("/books/{book_id}/patches/auto-build")
async def auto_build_patches(
    request: Request,
    book_id: int,
):
    body = await request.form()
    start_chapter_str = body.get("start_chapter")
    end_chapter_str = body.get("end_chapter")
    patch_size_str = body.get("patch_size")

    try:
        start_chapter = int(start_chapter_str) if start_chapter_str else None
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="start_chapter is required and must be an integer")
    if start_chapter is None:
        raise HTTPException(status_code=400, detail="start_chapter is required")
    end_chapter = None
    if end_chapter_str is not None and end_chapter_str.strip() != "":
        try:
            end_chapter = int(end_chapter_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="end_chapter must be an integer")
    patch_size = None
    if patch_size_str is not None and patch_size_str.strip() != "":
        try:
            patch_size = int(patch_size_str)
        except ValueError:
            raise HTTPException(status_code=400, detail="patch_size must be an integer")

    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        try:
            repository.auto_build_patches(conn, book_id, start_chapter, end_chapter, patch_size)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.get("/books/{book_id}/patches/{patch_id}/text", response_class=PlainTextResponse)
def get_patch_text(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        text = repository.build_patch_text(conn, patch)
    return PlainTextResponse(text)


@router.get("/books/{book_id}/patches/{patch_id}/audio")
def get_patch_audio(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = repository.get_patch(conn, patch_id)
        if patch is None or patch.book_id != book_id:
            raise HTTPException(status_code=404, detail="patch not found")
        if patch.status != "done" or not patch.audio_path:
            raise HTTPException(status_code=404, detail="audio not available")
        path = patch.audio_path
    return FileResponse(path, media_type="audio/wav")


@router.get("/books/{book_id}/patches/build", response_class=HTMLResponse)
def patch_builder_page(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        chapters = repository.list_chapters(conn, book_id)
        patches = repository.list_patches(conn, book_id)
    return templates.TemplateResponse(
        request, "patch_builder.html",
        {"book": book, "chapters": chapters, "patches": patches},
    )


@router.post("/books/{book_id}/patches/build")
async def patch_builder_submit(request: Request, book_id: int):
    body = await request.form()
    excluded_list = body.getlist("excluded")
    excluded_set = {int(x) for x in excluded_list if x.isdigit()}
    range_starts = body.getlist("range_start")
    range_ends = body.getlist("range_end")
    ranges: list[tuple[int, int]] = []
    for rs, re_ in zip(range_starts, range_ends):
        try:
            s, e = int(rs), int(re_)
            if s <= e:
                ranges.append((s, e))
        except ValueError:
            continue

    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail=f"book {book_id} not found")
        for ch in repository.list_chapters(conn, book_id):
            new_excluded = ch.chapter_index in excluded_set
            if new_excluded != ch.is_excluded:
                repository.set_chapter_excluded(
                    conn, book_id, ch.chapter_index, new_excluded
                )
        if ranges:
            try:
                repository.rebuild_patches(conn, book_id, ranges, reset_done=True)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)
