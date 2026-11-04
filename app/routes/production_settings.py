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

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request

from app import db as app_db
from app import repository
from app.config import settings
from app.deps import locked_conn
from app.production_defaults import (GROUPS, get_effective_audio_config,
                                     get_effective_branding_config,
                                     get_effective_normalization_config,
                                     get_effective_video_config,
                                     get_effective_youtube_config,
                                     get_global_production_defaults,
                                     get_group_mode, parse_book_config, set_book_group_mode_db,
                                     save_global_production_defaults,
                                     bulk_apply_group,
                                     save_book_branding_config)

router = APIRouter()
logger = logging.getLogger(__name__)


def invalidate_branding_artifacts(conn, book_id: int) -> list[int]:
    """Delete cached thumbnails/podcast cover baked with the old branding and
    mark those patches' thumbnails pending so the worker regenerates them.
    Returns the purged patch ids. The caller commits."""
    from app import image_overlay

    purged_patch_ids = []
    for patch in repository.list_patches(conn, book_id):
        overlay_path = image_overlay.get_patch_overlay_path(book_id, patch.patch_index)
        if overlay_path.exists():
            overlay_path.unlink()
            purged_patch_ids.append(patch.id)
    cover_path = image_overlay.get_podcast_cover_path(book_id)
    if cover_path.exists():
        cover_path.unlink()
    if purged_patch_ids:
        conn.execute(
            f"UPDATE patch_pipeline SET thumbnail_status='pending' WHERE patch_id IN ({','.join(['?'] * len(purged_patch_ids))})",
            purged_patch_ids,
        )
    return purged_patch_ids


def _book_effective(conn, book, group: str) -> dict:
    if group == "audio":
        return get_effective_audio_config(conn, book)
    if group == "normalization":
        return get_effective_normalization_config(conn, book)
    if group == "video":
        return get_effective_video_config(conn, book)
    if group == "branding":
        return get_effective_branding_config(conn, book)
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


@router.post("/books/{book_id}/branding-config")
async def save_book_branding(request: Request, book_id: int):
    """Save per-book branding config into automation_config['branding'].

    Validates the branding payload, stores it atomically with the custom mode
    flag, invalidates cached thumbnails/podcast covers, and returns the
    effective branding and a list of patch IDs whose artifacts were purged so
    the frontend can trigger regeneration.
    """
    body = await request.json()
    if not isinstance(body, dict) or "branding" not in body:
        raise HTTPException(400, "branding payload is required")
    branding_input = body["branding"]
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        try:
            validated = save_book_branding_config(conn, book_id, branding_input)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

        purged_patch_ids = invalidate_branding_artifacts(conn, book_id)
        conn.commit()
        book = repository.get_book(conn, book_id)

    return {
        "status": "ok",
        "effective": get_effective_branding_config(conn, book),
        "mode": "custom",
        "purged_patch_ids": purged_patch_ids,
    }


@router.get("/production-settings/books")
def list_books_production_modes(request: Request):
    """Every book with its per-group mode, for the bulk-apply picker."""
    with locked_conn(request) as conn:
        rows = conn.execute("SELECT id FROM book ORDER BY id DESC").fetchall()
        items = []
        for row in rows:
            book = repository.get_book(conn, row["id"])
            if book is None:
                continue
            config = parse_book_config(book)
            items.append({
                "id": book.id,
                "title": book.title,
                "status": book.status,
                "modes": {group: get_group_mode(config, group, book=book) for group in GROUPS},
            })
    return {"items": items}


@router.post("/production-settings/bulk-apply")
async def bulk_apply_production_settings(request: Request):
    """Apply one settings group to many books at once.

    Body: ``{"group": "video", "book_ids": [1, 2], "mode": "inherit" | "values",
    "values": {...}}``. ``inherit`` points the books back at the global defaults;
    ``values`` overwrites only the given fields on each book (stored as custom).
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "payload must be an object")
    group = body.get("group")
    mode = body.get("mode")
    book_ids = body.get("book_ids")
    values = body.get("values") or {}
    if group not in GROUPS or mode not in {"inherit", "values"}:
        raise HTTPException(400, "invalid group or mode")
    if not isinstance(book_ids, list) or not book_ids or not all(
        isinstance(value, int) and not isinstance(value, bool) for value in book_ids
    ):
        raise HTTPException(400, "book_ids must be a non-empty list of ids")
    if not isinstance(values, dict):
        raise HTTPException(400, "values must be an object")
    try:
        with locked_conn(request) as conn:
            result = bulk_apply_group(conn, group, book_ids, mode, values)
            if group == "branding":
                for book_id in result["updated"]:
                    invalidate_branding_artifacts(conn, book_id)
                conn.commit()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

    if group == "video" and result["updated"]:
        # Same as the per-book save: a now-valid video config lets waiting patches run.
        def _reconcile():
            from app.patch_publishing import reconcile_patch_automation
            with app_db.connect(settings.db_path) as conn:
                for book_id in result["updated"]:
                    reconcile_patch_automation(conn, book_id=book_id)
        try:
            await asyncio.to_thread(_reconcile)
        except Exception:
            logger.exception("automation reconcile failed after bulk video-config apply")
    return result
