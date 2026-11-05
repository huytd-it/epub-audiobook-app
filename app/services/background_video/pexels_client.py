"""Style: simple, no external service required."""
from __future__ import annotations

import logging
import sqlite3

import httpx

from app.config import settings
from app.services.background_video import ClipCandidate, save_clip
from app.services.background_video.keywords import get_preset

logger = logging.getLogger(__name__)


def _choose_pexels_file(video: dict) -> str | None:
    files = video.get("video_files") or []

    def score(f: dict) -> tuple[int, int, int]:
        w, h = f.get("width") or 0, f.get("height") or 0
        landscape = 1 if w >= h else 0
        penalty = 0 if (w <= 1920 and h <= 1080) else 1
        return (-landscape, penalty, w * h)

    for f in sorted(files, key=score):
        link = f.get("link") or ""
        if link.lower().endswith(".mp4"):
            return link
    return (files[0].get("link") if files else None)


def _stream_to(dest, link: str, timeout: float) -> int:
    with httpx.stream("GET", link, timeout=timeout, follow_redirects=True) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1024 * 256):
                f.write(chunk)
    return dest.stat().st_size if dest.exists() else 0


def fetch_pexels(
    conn: sqlite3.Connection,
    keyword: str,
    *,
    query: str | None = None,
    per_page: int | None = None,
    timeout: float = 60.0,
) -> dict:
    """Fetch Pexels videos for a keyword preset query; save into pool."""
    api_key = settings.pexels_api_key.strip()
    if not api_key:
        return {"clips_saved": 0, "message": "PEXELS_API_KEY not set (in .env)"}
    preset = get_preset(keyword)
    q = (query or (preset.get("queries") or [keyword])[0]).strip()
    per_page = per_page or settings.pexels_per_page or 20
    url = "https://api.pexels.com/v1/videos/search"
    params = {"query": q, "per_page": per_page, "orientation": "landscape"}
    headers = {"Authorization": api_key}
    saved = 0
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("event=bgvideo.pexels_failed keyword=%s error=%s", keyword, exc)
        return {"clips_saved": 0, "message": f"Pexels error: {exc}"}

    for video in payload.get("videos", []):
        link = _choose_pexels_file(video)
        if not link:
            continue
        duration = video.get("duration") or 0
        if settings.bg_video_max_clip_seconds and duration > settings.bg_video_max_clip_seconds:
            continue

        def _dl(dest, link=link, timeout=timeout):
            return _stream_to(dest, link, timeout)

        clip = ClipCandidate(
            keyword=keyword, platform="pexels", source_url=link,
            author=(video.get("user") or {}).get("name") or "",
            title=video.get("url") or "",
            tags=[q],
            license_note="Pexels License",
            duration_seconds=float(duration),
            width=int(video.get("width") or 0),
            height=int(video.get("height") or 0),
            download_to=_dl,
        )
        try:
            row = save_clip(conn, clip)
            saved += 1
            logger.info("event=bgvideo.pexels_saved clip_id=%s keyword=%s", row["id"], keyword)
        except Exception as exc:  # noqa: BLE001
            logger.warning("event=bgvideo.pexels_save_failed keyword=%s error=%s", keyword, exc)
            continue
    return {"clips_saved": saved, "message": q}


def fetch_pixabay(conn: sqlite3.Connection, keyword: str, *, query: str | None = None,
                  timeout: float = 60.0) -> dict:
    """Pixabay videos fallback."""
    api_key = settings.pixabay_api_key.strip()
    if not api_key:
        return {"clips_saved": 0, "message": "PIXABAY_API_KEY not set (in .env)"}
    preset = get_preset(keyword)
    q = (query or (preset.get("queries") or [keyword])[0]).strip()
    url = "https://pixabay.com/api/videos/"
    params = {"key": api_key, "q": q, "per_page": 20, "orientation": "landscape"}
    saved = 0
    try:
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        return {"clips_saved": 0, "message": f"Pixabay error: {exc}"}
    for hit in payload.get("hits", []):
        vids = hit.get("videos") or {}
        link = (vids.get("medium") or {}).get("url") or (vids.get("large") or {}).get("url") or ""
        if not link:
            continue
        duration = hit.get("duration") or 0
        if settings.bg_video_max_clip_seconds and duration > settings.bg_video_max_clip_seconds:
            continue

        def _dl(dest, link=link, timeout=timeout):
            return _stream_to(dest, link, timeout)

        clip = ClipCandidate(
            keyword=keyword, platform="pixabay", source_url=link,
            author=hit.get("user") or "", title=hit.get("pageURL") or "",
            tags=[q], license_note="Pixabay Content License",
            duration_seconds=float(duration),
            width=0, height=0, download_to=_dl,
        )
        try:
            save_clip(conn, clip)
            saved += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("event=bgvideo.pixabay_save_failed keyword=%s error=%s", keyword, exc)
            continue
    return {"clips_saved": saved, "message": q}
