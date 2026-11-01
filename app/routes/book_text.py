"""Text-only book bundle REST API (move a book's text to another machine)."""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app import repository
from app.book_text_bundle import (
    bundle_filename,
    export_book_text,
    import_book_text,
    validate_bundle,
)
from app.deps import locked_conn

logger = logging.getLogger(__name__)

router = APIRouter()

# A text bundle is JSON: generous, but still bounded so a bad upload cannot eat
# all memory before validation runs.
_MAX_BUNDLE_BYTES = 200 * 1024 * 1024


@router.get("/books/{book_id}/text-bundle")
def download_text_bundle(request: Request, book_id: int):
    """Download the book's text layer as a portable JSON file."""
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="Không tìm thấy sách")
        if not conn.execute(
            "SELECT 1 FROM chapter WHERE book_id = ? LIMIT 1", (book_id,)
        ).fetchone():
            raise HTTPException(status_code=400, detail="Sách chưa có chương nào để xuất.")
        try:
            bundle = export_book_text(conn, book_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail="Không tìm thấy sách") from exc
        filename = bundle_filename(book)

    payload = json.dumps(bundle, ensure_ascii=False, indent=2)
    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/text-bundles/import")
async def import_text_bundle(
    request: Request,
    file: UploadFile = File(...),
    mode: str = Form("merge"),
    target_book_id: int | None = Form(None),
    dry_run: bool = Form(False),
):
    """Import a text bundle, merging into the matching book or creating one.

    ``mode=merge`` updates the same book this file came from (created here when
    missing); ``mode=new`` always creates a new book. ``dry_run=1`` previews the
    merge without writing anything.
    """
    if mode not in ("merge", "new"):
        raise HTTPException(status_code=400, detail="mode must be 'merge' or 'new'")
    filename = (file.filename or "").lower()
    if filename and not filename.endswith(".json"):
        raise HTTPException(status_code=400, detail="File phải có đuôi .json")

    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="File rỗng")
    if len(raw) > _MAX_BUNDLE_BYTES:
        raise HTTPException(status_code=400, detail="File quá lớn (giới hạn 200MB)")
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=400, detail="File không phải JSON hợp lệ") from exc

    try:
        validate_bundle(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    with locked_conn(request) as conn:
        if target_book_id is not None and mode == "merge":
            if repository.get_book(conn, target_book_id) is None:
                raise HTTPException(status_code=404, detail="Không tìm thấy sách đích")
        try:
            return import_book_text(
                conn,
                data,
                mode=mode,
                target_book_id=target_book_id,
                dry_run=dry_run,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface DB/bundle errors as 400
            logger.exception("text bundle import failed")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
