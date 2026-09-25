"""Generative-AI routes: content drafts + per-book cover thumbnails.

Two layers (both extensible without touching this file):

- Text tasks registered in ``app/ai_content.py`` (``youtube_content``,
  ``thumbnail_prompt``, ...) are dispatched by ``POST /ai/generate``.
- The per-book cover goes through ``POST /books/{id}/ai/thumbnail`` and is
  stored under ``data/ai_thumbnails/<book_id>/``.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app import ai_content, ai_providers, repository
from app.deps import locked_conn

logger = logging.getLogger(__name__)
router = APIRouter()


def _book_or_404(conn, book_id: int):
    book = repository.get_book(conn, book_id)
    if book is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy sách")
    return book


@router.get("/ai/providers")
def ai_providers_list():
    """Catalog + status (keys never leave the server)."""
    return {"providers": ai_providers.list_providers(),
            "tasks": ai_content.list_tasks()}


@router.get("/books/{book_id}/ai/status")
def ai_book_status(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = _book_or_404(conn, book_id)
        context = ai_content.build_book_context(conn, book)
        try:
            saved = json.loads(book.ai_content_json or "{}") if book.ai_content_json else {}
        except (ValueError, TypeError):
            saved = {}
    return {
        "configured": ai_providers.is_configured(),
        "providers": ai_providers.list_providers(),
        "tasks": ai_content.list_tasks(),
        "context": context,
        "saved_content": saved if isinstance(saved, dict) else {},
        "ai_thumbnail_path": book.ai_thumbnail_path,
        "cover_image_path": book.cover_image_path,
    }


@router.post("/books/{book_id}/ai/generate")
async def ai_generate(request: Request, book_id: int):
    """Run a registered text task (default: youtube_content).

    Body: {"task": "youtube_content", "provider": null, "model": null,
           "language": "vi", "apply": true, "save": true}
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body phải là JSON object")
    task_name = str(data.get("task") or "youtube_content")
    provider_id = data.get("provider") or None
    model = data.get("model") or None
    params = {k: v for k, v in data.items() if k not in {"task", "provider", "model"}}
    params.setdefault("save", True)
    if not ai_providers.is_configured(provider_id if isinstance(provider_id, str) else None):
        raise HTTPException(
            status_code=400,
            detail="Chưa cấu hình AI: đặt OPENAI_API_KEY trong .env (xem .env.example mục Generative AI).",
        )
    with locked_conn(request) as conn:
        book = _book_or_404(conn, book_id)
        try:
            summary = ai_content.run_task(
                conn, book, task_name, params=params,
                provider_id=provider_id if isinstance(provider_id, str) else None,
                model=model if isinstance(model, str) else None,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            logger.warning("ai_generate failed for book %s: %s", book_id, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(summary)


@router.post("/books/{book_id}/ai/thumbnail")
async def ai_generate_thumbnail(request: Request, book_id: int):
    """Generate ONE cover for the whole book (per-book thumbnail).

    Body: {"prompt": null (auto from book metadata), "style": null,
           "size": "1280x720", "save": true}
    """
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Body phải là JSON object")
    if not ai_providers.is_configured(data.get("provider") if isinstance(data.get("provider"), str) else None):
        raise HTTPException(
            status_code=400,
            detail="Chưa cấu hình AI: đặt OPENAI_API_KEY trong .env (xem .env.example mục Generative AI).",
        )
    size = str(data.get("size") or "1280x720")
    if size not in {"1024x1024", "1280x720", "1920x1080", "1024x1792", "1792x1024"}:
        raise HTTPException(status_code=400, detail=f"size không hợp lệ: {size}")
    with locked_conn(request) as conn:
        book = _book_or_404(conn, book_id)
        try:
            result = ai_content.generate_book_thumbnail(
                conn, book,
                prompt=data.get("prompt") if isinstance(data.get("prompt"), str) else None,
                provider_id=data.get("provider") if isinstance(data.get("provider"), str) else None,
                model=data.get("model") if isinstance(data.get("model"), str) else None,
                style=data.get("style") if isinstance(data.get("style"), str) else None,
                size=size,
                save=bool(data.get("save", True)),
            )
        except (ValueError, RuntimeError) as exc:
            logger.warning("ai_thumbnail failed for book %s: %s", book_id, exc)
            raise HTTPException(status_code=502, detail=str(exc)) from exc
    return JSONResponse(result)


@router.get("/books/{book_id}/ai/thumbnail")
def ai_get_thumbnail(request: Request, book_id: int):
    """Serve the latest AI-generated book cover."""
    with locked_conn(request) as conn:
        book = _book_or_404(conn, book_id)
        path = book.ai_thumbnail_path or ""
    if not path or not Path(path).is_file():
        raise HTTPException(status_code=404, detail="Chưa có ảnh bìa AI cho sách này")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})
