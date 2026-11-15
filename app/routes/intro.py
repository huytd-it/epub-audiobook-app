"""Intro động & CTA ngẫu nhiên — preview + per-book + global settings."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.deps import locked_conn
from app import repository
from app.production_defaults import (
    get_effective_intro_config,
    get_global_production_defaults,
    save_book_intro_config,
    save_global_production_defaults,
)
from app.intro_templates import collect_warnings, estimated_seconds, render_template, validate_template

router = APIRouter()


@router.get("/production-defaults/intro")
def get_intro_defaults(request: Request):
    with locked_conn(request) as conn:
        cfg = get_global_production_defaults(conn)["intro"]
        return cfg


@router.put("/production-defaults/intro")
async def put_intro_defaults(request: Request):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "payload must be an object")
    try:
        with locked_conn(request) as conn:
            saved = save_global_production_defaults(conn, {"intro": body})
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return saved["intro"]


@router.get("/books/{book_id}/intro-settings")
def get_book_intro_settings(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        from app.production_defaults import get_group_mode, parse_book_config

        cfg = get_effective_intro_config(conn, book)
        mode = get_group_mode(parse_book_config(book), "intro", book=book)
        return {"config": cfg, "mode": mode}


@router.put("/books/{book_id}/intro-settings")
async def put_book_intro_settings(request: Request, book_id: int):
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "payload must be an object")
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(404, "book not found")
        # Blocking warnings when enabled template references empty vars (channel_name/author)
        # Caller may pass confirm=true to force save despite warnings.
        confirm = bool(body.pop("confirm", False)) if isinstance(body.get("confirm"), bool) else False
        # validate before save so errors surface as 400
        from app.production_defaults import validate_intro_config

        try:
            validated = validate_intro_config(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        # Check empty-var warnings that would make rendered intro/CTA blank-ish
        warnings: list[str] = []
        channel_name = str(validated.get("channel_name") or "")
        author = str(getattr(book, "author", "") or "")
        for enabled_key, tpl_key in (("intro_enabled", "intro_template"), ("cta_enabled", "cta_template")):
            if not validated.get(enabled_key):
                continue
            tpl = str(validated.get(tpl_key) or "")
            if not tpl.strip():
                continue
            vars_for_check = {
                "channel_name": channel_name,
                "book_title": getattr(book, "title", "") or "",
                "chapter_title": "Chương 1",
                "author": author,
            }
            w = collect_warnings(tpl, vars_for_check)
            # Only block on missing-variable warnings, not duration
            for msg in w:
                if "Thiếu dữ liệu" in msg:
                    warnings.append(f"{tpl_key}: {msg}")
        if warnings and not confirm:
            raise HTTPException(400, "; ".join(warnings))
        try:
            saved = save_book_intro_config(conn, book_id, validated)
            conn.commit()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"config": saved, "mode": "custom", "warnings": warnings}


@router.post("/intro/preview")
async def intro_preview(request: Request):
    """Preview rendered template with real book data.

    Body: {template, book_id?, vars_override?}
    Returns: {rendered, warnings, estimated_seconds}
    """
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "payload must be an object")
    template = str(body.get("template") or "")
    errs = validate_template(template)
    if errs:
        raise HTTPException(400, "; ".join(errs))
    book_id = body.get("book_id")
    vars_override = body.get("vars_override") if isinstance(body.get("vars_override"), dict) else {}
    variables: dict = {
        "channel_name": "",
        "book_title": "",
        "chapter_title": "",
        "author": "",
    }
    if book_id is not None:
        try:
            book_id = int(book_id)
        except (TypeError, ValueError):
            raise HTTPException(400, "book_id must be an integer") from None
        with locked_conn(request) as conn:
            book = repository.get_book(conn, book_id)
            if book is None:
                raise HTTPException(404, "book not found")
            from app.production_defaults import get_effective_intro_config

            intro_cfg = get_effective_intro_config(conn, book)
            variables["channel_name"] = str(intro_cfg.get("channel_name") or "")
            variables["book_title"] = getattr(book, "title", "") or ""
            variables["author"] = getattr(book, "author", "") or ""
            # chapter_title: first chapter title of first patch, or first chapter
            try:
                patches = repository.list_patches(conn, book_id)
                if patches:
                    p0 = patches[0]
                    chs = repository.get_chapters_in_range(conn, book_id, p0.chapter_start, p0.chapter_end)
                    if chs:
                        variables["chapter_title"] = chs[0].title or ""
                if not variables["chapter_title"]:
                    chs = repository.list_chapters(conn, book_id)
                    if chs:
                        variables["chapter_title"] = chs[0].title or ""
            except Exception:
                pass
    # vars_override wins
    for k in list(variables.keys()):
        if k in vars_override:
            variables[k] = str(vars_override[k] or "")
    rendered = render_template(template, variables)
    warnings = collect_warnings(template, variables)
    return {
        "rendered": rendered,
        "warnings": warnings,
        "estimated_seconds": round(estimated_seconds(rendered), 2),
        "variables": variables,
    }
