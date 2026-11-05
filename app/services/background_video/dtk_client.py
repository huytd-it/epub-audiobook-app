"""Style: simple, no external service required."""
from __future__ import annotations

import logging
import sqlite3
import time

import httpx

from app.config import settings
from app.services.background_video import ClipCandidate, save_clip
from app.services.background_video.errors import DtkError, DtkNotReady
from app.services.background_video.keywords import get_preset

logger = logging.getLogger(__name__)


class DtkClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None,
                 timeout: float | None = None):
        self.base_url = (base_url or settings.dtk_base_url or "http://127.0.0.1:18080").rstrip("/")
        self.api_key = (api_key if api_key is not None else settings.dtk_api_key).strip()
        self.timeout = timeout or settings.dtk_timeout_seconds

    def _headers(self) -> dict:
        if not self.api_key:
            raise DtkNotReady("DTK_API_KEY not set (in .env) — start DTK sidecar: docker compose -p dtk -f docker/compose.yml up -d")
        return {"X-API-Key": self.api_key, "Content-Type": "application/json"}

    def _envelope(self, resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except ValueError as exc:
            raise DtkError(f"DTK returned non-JSON ({resp.status_code})") from exc
        if body.get("success") is False:
            err = body.get("error") or {}
            code = err.get("code") or "UNKNOWN"
            msg = err.get("message") or code
            if code in {"IDENTITY_POOL_EXHAUSTED", "IDENTITY_LOGIN_REQUIRED",
                        "RATE_LIMITED", "QUEUE_FULL", "UPSTREAM_RISK_CONTROL"}:
                raise DtkNotReady(f"{code}: {msg}")
            raise DtkError(f"{code}: {msg}")
        return body.get("data") or {}

    def _request_with_wait(self, path: str, *, params: dict | None = None,
                           body: dict | None = None, wait: int = 25) -> dict:
        url = f"{self.base_url}{path}"
        qs = dict(params or {})
        if wait > 0:
            qs["wait"] = str(min(30, wait))
        headers = self._headers()
        try:
            with httpx.Client(timeout=self.timeout + 10) as client:
                if body is not None:
                    resp = client.post(url, params=qs, headers=headers, json=body)
                else:
                    resp = client.get(url, params=qs, headers=headers)
        except httpx.ConnectError as exc:
            raise DtkNotReady(f"DTK sidecar unreachable at {self.base_url} — start it: docker compose -p dtk -f docker/compose.yml up -d") from exc
        except httpx.HTTPError as exc:
            raise DtkError(str(exc)) from exc
        data = self._envelope(resp)
        if isinstance(data, dict) and data.get("state") in {"queued", "running"} and data.get("task_id"):
            data = self._poll_task(data["task_id"])
        return data

    def _poll_task(self, task_id: str) -> dict:
        deadline = time.time() + settings.dtk_poll_timeout_seconds
        interval = settings.dtk_poll_interval_seconds
        headers = self._headers()
        url = f"{self.base_url}/api/v1/tasks/{task_id}"
        while time.time() < deadline:
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                raise DtkError(str(exc)) from exc
            data = self._envelope(resp)
            state = data.get("state")
            if state == "done":
                return data.get("data") or {}
            if state == "failed":
                err = data.get("error") or {}
                code = err.get("code") or "TASK_FAILED"
                if code in {"IDENTITY_POOL_EXHAUSTED", "IDENTITY_LOGIN_REQUIRED"}:
                    raise DtkNotReady(f"{code}: {err.get('message') or code}")
                raise DtkError(f"{code}: {err.get('message') or code}")
            time.sleep(interval)
        raise DtkError(f"timed out waiting for DTK task {task_id}")

    def search_posts(self, platform: str, keyword: str, *, count: int = 20) -> list[dict]:
        data = self._request_with_wait(
            f"/api/v1/{platform}/search/posts",
            params={"keyword": keyword, "count": count},
        )
        return data.get("items") or []

    def hashtag_posts(self, tag: str, *, count: int = 20) -> tuple[str, list[dict]]:
        info = self._request_with_wait(
            "/api/v1/tiktok/hashtag", params={"name": tag},
        )
        hashtag_id = info.get("hashtag_id") or info.get("id") or ""
        if not hashtag_id:
            raise DtkError(f"hashtag lookup returned no id for {tag!r}: {info}")
        data = self._request_with_wait(
            "/api/v1/tiktok/hashtag/posts",
            params={"hashtag_id": hashtag_id, "count": count},
        )
        return hashtag_id, (data.get("items") or [])

    def get_video(self, platform: str, *, url: str | None = None, aweme_id: str | None = None) -> dict:
        params: dict = {}
        if url:
            params["url"] = url
        if aweme_id:
            params["aweme_id"] = aweme_id
        return self._request_with_wait(f"/api/v1/{platform}/video", params=params)

    def download_media(self, platform: str, *, url: str | None = None, content_id: str | None = None):
        body: dict = {"platform": platform}
        if url:
            body["url"] = url
        if content_id:
            body["content_id"] = content_id
        return self._request_with_wait("/api/v1/downloads", body=body)

    def download_file(self, platform: str, download_id: str, filename: str) -> httpx.Response:
        url = f"{self.base_url}/api/v1/downloads/{download_id}/files/{filename}"
        with httpx.Client(timeout=self.timeout + 10) as client:
            resp = client.get(url, headers=self._headers())
            resp.raise_for_status()
            return resp


def _content_id(post: dict) -> str:
    return str(post.get("content_id") or post.get("aweme_id") or post.get("video_id") or "")


def _web_url(post: dict) -> str:
    return post.get("web_url") or post.get("url") or ""


def fetch_via_dtk(
    conn: sqlite3.Connection,
    keyword: str,
    *,
    platform: str = "tiktok",
    query: str | None = None,
    limit: int = 10,
    use_hashtag: bool = True,
) -> dict:
    """Ingest up to ``limit`` clips found via DTK search/hashtag into the pool."""
    preset = get_preset(keyword)
    client = DtkClient()
    q_used = query or keyword
    try:
        if use_hashtag and platform == "tiktok":
            tags = preset.get("tiktok_hashtags") or [keyword.replace("-", "")[:20]]
            posts: list[dict] = []
            for tag in tags:
                try:
                    _, items = client.hashtag_posts(tag, count=limit)
                    if items:
                        q_used = f"#{tag}"
                        posts = items[:limit]
                        break
                except (DtkNotReady, DtkError) as exc:
                    logger.info("event=bgvideo.dtk_hashtag_failed tag=%s error=%s", tag, exc)
                    continue
            if not posts:
                posts = client.search_posts("tiktok", q_used, count=limit)
        else:
            posts = client.search_posts(platform, q_used, count=limit)
    except DtkNotReady as exc:
        return {"clips_saved": 0, "message": str(exc), "platform": platform, "query": q_used}
    except DtkError as exc:
        return {"clips_saved": 0, "message": str(exc), "platform": platform, "query": q_used}

    saved = 0
    for post in posts[:limit]:
        content_id = _content_id(post)
        url = _web_url(post)
        if not url and content_id:
            try:
                detail = client.get_video(platform, aweme_id=content_id)
                url = detail.get("web_url") or ""
            except (DtkError, DtkNotReady):
                continue
        if not url and not content_id:
            continue
        # DTK sidecar returns 202 with task_id for POST /api/v1/downloads when
        # the download is queued; _request_with_wait polls and returns the
        # *inner* task data (files/bytes_total), not the download record.
        # So we poll the task ourselves here to wait until the download is
        # actually done, then fetch the file bytes.
        expected_task_id = None
        try:
            dl = client.download_media(platform, url=url or None, content_id=content_id or None)
            dl_id = dl.get("download_id")
            expected_task_id = dl.get("task_id")
            if not dl_id:
                # Poll returned inner task data without download_id (e.g. task
                # already finished before first poll). Try to recover it via
                # the task payload if it carries one.
                continue
            if expected_task_id and dl.get("state") in (None, "queued", "running"):
                client._poll_task(expected_task_id)
            # Verify download state via GET /downloads/{id}; task 'done' alone
            # is not enough (Bilibili stream fetch can fail with file_count=0).
            info = client._request_with_wait(f"/api/v1/downloads/{dl_id}")
            if info.get("state") != "done" or not (info.get("file_count") or 0):
                logger.info("event=bgvideo.dtk_download_no_file content_id=%s dl_id=%s", content_id, dl_id)
                continue
            resp = client.download_file(platform, dl_id, "video.mp4")
        except (DtkError, DtkNotReady) as exc:
            logger.info("event=bgvideo.dtk_download_failed content_id=%s error=%s", content_id, exc)
            continue

        def _dl(dest, resp=resp):
            dest.write_bytes(resp.content)
            return dest.stat().st_size if dest.exists() else 0

        raw_dur = post.get("duration_seconds")
        if raw_dur is None:
            raw_dur = float(post.get("duration_ms") or 0) / 1000.0
        try:
            clip = ClipCandidate(
                keyword=keyword,
                platform=platform,
                source_url=url or content_id,
                author=str((post.get("author") or {}).get("nickname") or ""),
                title=str(post.get("title") or post.get("description") or ""),
                tags=[q_used],
                license_note=f"DTK {platform}: {content_id or ''}".strip(),
                duration_seconds=float(raw_dur or 0),
                width=0, height=0,
                download_to=_dl,
            )
            save_clip(conn, clip)
            saved += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("event=bgvideo.dtk_save_failed content_id=%s error=%s", content_id, exc)
            continue
    return {"clips_saved": saved, "message": f"{platform}:{q_used}", "platform": platform, "query": q_used}
