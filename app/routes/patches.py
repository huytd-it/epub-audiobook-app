from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from app import repository
from app.deps import locked_conn

router = APIRouter()


@router.post("/books/{book_id}/patches/{patch_id}/regenerate")
def regenerate_patch(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        repository.reset_patch(conn, patch_id)
    return RedirectResponse(url=f"/books/{book_id}", status_code=303)
