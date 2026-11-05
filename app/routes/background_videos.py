"""Style: simple, no external service required."""
from __future__ import annotations

import logging
import sqlite3

from fastapi import APIRouter, Form, HTTPException, Request

from app.config import settings
from app.deps import locked_conn
from app.services.background_video import list_clips, pool_stats, write_manifest
from app.services.background_video.keywords import KEYWORD_PRESETS, book_keyword, custom_book_keyword
from app.services.background_video.pexels_client import fetch_pexels, fetch_pixabay
from app.services.background_video.dtk_client import DtkError, DtkNotReady, fetch_via_dtk

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/background-videos", tags=["background-videos"])


@router.get("/presets")
def presets():
    """Keyword presets + TikTok hashtag mapping (per topics / book-specific)."""
    return {"keywords": KEYWORD_PRESETS}


@router.post("/book-keyword")
def suggest_keyword(title: str = Form(default=""), author: str = Form(default=""),
                    subjects: str = Form(default="")):
    return {"keyword": book_keyword(title, author, subjects),
            "custom": custom_book_keyword(title or author)}


@router.get("/pool")
def pool(request: Request, keyword: str | None = None, platform: str | None = None,
         status: str | None = None, limit: int = 200, offset: int = 0):
    with locked_conn(request) as conn:
        rows = list_clips(conn, keyword=keyword, platform=platform, status=status,
                          limit=limit, offset=offset)
        stats = pool_stats(conn)
    return {"clips": rows, "stats": stats}


@router.post("/fetch")
def fetch(
    request: Request,
    keyword: str = Form(default=""),
    platform: str = Form(default="pexels"),
    query: str = Form(default=""),
    limit: int = Form(default=10),
    use_hashtag: bool = Form(default=True),
):
    kw = keyword.strip() or settings.bg_video_default_keyword
    platform_l = (platform or "pexels").strip().lower()
    saved_total = 0
    messages: list[str] = []
    with locked_conn(request) as conn:
        if platform_l in {"pexels", "pexels+pixabay", "all"}:
            res = fetch_pexels(conn, kw, query=query or None, per_page=max(1, min(limit, 40)))
            saved_total += int(res.get("clips_saved") or 0)
            messages.append(f"pexels: {res.get('message')} -> {res.get('clips_saved')}")
        if platform_l in {"pixabay", "pexels+pixabay", "all"}:
            res = fetch_pixabay(conn, kw, query=query or None)
            saved_total += int(res.get("clips_saved") or 0)
            messages.append(f"pixabay: {res.get('message')} -> {res.get('clips_saved')}")
        if platform_l in {"tiktok", "bilibili", "douyin", "all"}:
            try:
                res = fetch_via_dtk(conn, kw, platform=platform_l if platform_l != "all" else "tiktok",
                                    query=query or None, limit=limit, use_hashtag=use_hashtag)
                saved_total += int(res.get("clips_saved") or 0)
                messages.append(f"{res.get('platform')}: {res.get('message')} -> {res.get('clips_saved')}")
            except DtkNotReady as exc:
                messages.append(f"dtk: {exc}")
                raise HTTPException(status_code=503, detail=str(exc)) from exc
            except DtkError as exc:
                messages.append(f"dtk: {exc}")
                raise HTTPException(status_code=502, detail=str(exc)) from exc
        write_manifest(conn, kw)
    return {"keyword": kw, "clips_saved": saved_total, "messages": messages}


@router.post("/ingest-url")
def ingest_url(request: Request, keyword: str = Form(default=""),
               platform: str = Form(default="tiktok"), url: str = Form(default=""),
               content_id: str = Form(default="")):
    """Ingest a single DTK post by URL/id (one-off, manual)."""
    kw = keyword.strip() or settings.bg_video_default_keyword
    if not url.strip() and not content_id.strip():
        raise HTTPException(status_code=400, detail="url or content_id is required")
    with locked_conn(request) as conn:
        try:
            saved = _ingest_one(conn, kw, platform=platform, url=url.strip() or None,
                                content_id=content_id.strip() or None)
        except DtkNotReady as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except DtkError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if saved is None:
            raise HTTPException(status_code=400, detail="post not found / download empty — check URL and DTK pool")
        write_manifest(conn, kw)
    return {"saved": saved}


def _ingest_one(conn: sqlite3.Connection, keyword: str, *, platform: str,
                url: str | None, content_id: str | None):
    from app.services.background_video import ClipCandidate, save_clip
    from app.services.background_video.dtk_client import DtkClient

    client = DtkClient()
    detail = client.get_video(platform, url=url, aweme_id=content_id)
    dl = client.download_media(platform, url=url, content_id=content_id or detail.get("content_id"))
    dl_id = dl.get("download_id")
    if not dl_id:
        return None
    resp = client.download_file(platform, dl_id, "video.mp4")

    def _dl(dest, resp=resp):
        dest.write_bytes(resp.content)
        return dest.stat().st_size if dest.exists() else 0

    clip = ClipCandidate(
        keyword=keyword, platform=platform,
        source_url=url or content_id or "",
        author=str((detail.get("author") or {}).get("nickname") or ""),
        title=str(detail.get("title") or ""),
        tags=[], license_note=f"DTK {platform}: {detail.get('content_id') or ''}".strip(),
        duration_seconds=float(detail.get("duration_seconds") or 0),
        download_to=_dl,
    )
    return save_clip(conn, clip)


@router.post("/pick")
def pick(request: Request, keyword: str = Form(default=""), count: int = Form(default=1),
         seed: int | None = Form(default=None)):
    kw = keyword.strip() or settings.bg_video_default_keyword
    with locked_conn(request) as conn:
        from app.services.background_video import pick_clips

        picked = pick_clips(conn, kw, count=count, seed=seed)
    if not picked:
        raise HTTPException(status_code=404, detail=f"no clips available for keyword {kw!r} — fetch first (POST /background-videos/fetch)")
    return {"keyword": kw, "clips": picked}


@router.post("/sync")
def sync(request: Request, keyword: str = Form(default="")):
    """Sync pool manifest.json for a keyword (debug export)."""
    kw = keyword.strip() or settings.bg_video_default_keyword
    with locked_conn(request) as conn:
        manifest = write_manifest(conn, kw)
    return manifest
