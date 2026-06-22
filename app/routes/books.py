from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app import repository
from app.config import settings
from app.deps import locked_conn
from app.epub_parser import parse_epub
from app.video_gen import generate_video

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


@router.get("/books", response_class=HTMLResponse)
def list_books(request: Request):
    with locked_conn(request) as conn:
        books = repository.list_books(conn)
        patch_counts = {
            b.id: {
                "total": len(repository.list_patches(conn, b.id)),
                "done": sum(1 for p in repository.list_patches(conn, b.id) if p.status == "done"),
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
    return templates.TemplateResponse(
        request, "book_detail.html", {"book": book, "patches": patch_list}
    )


@router.post("/books/{book_id}/video")
def trigger_video(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None or not book.final_audio_path:
            return RedirectResponse(url=f"/books/{book_id}", status_code=303)
        bg_image = book.background_image_path or settings.default_background_image
        out_path = str(Path(book.final_audio_path).parent / "final.mp4")

    generate_video(book.final_audio_path, bg_image, out_path, use_nvenc=settings.use_nvenc)

    with locked_conn(request) as conn:
        repository.set_book_final_video(conn, book_id, out_path)

    return RedirectResponse(url=f"/books/{book_id}", status_code=303)


@router.post("/books/{book_id}/delete")
def delete_book(request: Request, book_id: int):
    with locked_conn(request) as conn:
        repository.delete_book(conn, book_id, settings.data_root)
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
