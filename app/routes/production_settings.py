"""Project default settings (production defaults) + per-book effective resolution API.

GET /production-settings returns the global defaults; with ``book_id`` it also
returns each group's mode (inherit/custom) and the effective value for that book.
POST /production-settings saves global defaults (partial per-group update).

Modes and effective values live here so the frontend has one place to resolve
them; the backend pipeline (preflight, snapshots, metadata, normalization,
enqueue paths) uses the same ``app.production_defaults`` helpers, so a book
renders with exactly what this endpoint reports.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app import repository
from app.deps import locked_conn
from app.production_defaults import (GROUPS, get_effective_audio_config,
                                     get_effective_normalization_config,
                                     get_effective_video_config,
                                     get_effective_youtube_config,
                                     get_global_production_defaults,
                                     get_group_mode, parse_book_config, set_book_group_mode_db,
                                     save_global_production_defaults)

router = APIRouter()


def _book_effective(conn, book, group: str) -> dict:
    if group == "audio":
        return get_effective_audio_config(conn, book)
    if group == "normalization":
        return get_effective_normalization_config(conn, book)
    if group == "video":
        return get_effective_video_config(conn, book)
    return get_effective_youtube_config(conn, book)


@router.get("/production-settings")
def get_production_settings(request: Request, book_id: int = 0):
    with locked_conn(request) as conn:
        config = get_global_production_defaults(conn)
        result = {
            "schema_version": config["schema_version"],
            "updated_at": config["updated_at"],
            "defaults": {group: config[group] for group in GROUPS},
        }
        if book_id:
            book = repository.get_book(conn, book_id)
            if book is None:
                raise HTTPException(404, "book not found")
            modes = {group: get_group_mode(parse_book_config(book), group, book=book)
                     for group in GROUPS}
            result.update({
                "book_id": book_id,
                "modes": modes,
                "effective": {group: _book_effective(conn, book, group) for group in GROUPS},
            })
        return result


@router.post("/production-settings")
async def save_production_settings(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "payload must be an object")
    deltas = body.get("groups") if isinstance(body.get("groups"), dict) else body
    if not isinstance(deltas, dict) or not any(group in deltas for group in GROUPS):
        raise HTTPException(400, "no valid production setting group provided")
    try:
        with locked_conn(request) as conn:
            saved = save_global_production_defaults(conn, deltas)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "schema_version": saved["schema_version"],
        "updated_at": saved["updated_at"],
        "defaults": {group: saved[group] for group in GROUPS},
    }


@router.post("/books/{book_id}/production-settings-mode")
async def save_book_production_settings_mode(request: Request, book_id: int):
    body = await request.json()
    group = body.get("group") if isinstance(body, dict) else None
    mode = body.get("mode") if isinstance(body, dict) else None
    if group not in GROUPS or mode not in {"inherit", "custom"}:
        raise HTTPException(400, "invalid group or mode")
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        set_book_group_mode_db(conn, book_id, group, mode)
        book = repository.get_book(conn, book_id)
        return {
            "group": group,
            "mode": mode,
            "effective": _book_effective(conn, book, group),
        }
