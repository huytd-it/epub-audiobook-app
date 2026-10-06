"""Short Video Studio API: sách -> script AI/sửa tay -> render -> caption -> publish 3 kênh."""
from __future__ import annotations

import json

from fastapi import APIRouter, Form, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app import repository, shorts_repository
from app.deps import locked_conn
from app.jobqueue import store
from app.video_config import SHORT_RESOLUTIONS, validate_short_config

router = APIRouter(prefix="/shorts", tags=["shorts"])


def _short_payload(short) -> dict:
    return {
        "id": short.id, "book_id": short.book_id,
        "script_text": short.script_text, "script_source": short.script_source,
        "duration_target": short.duration_target, "voice_id": short.voice_id,
        "music_id": short.music_id, "resolution": short.resolution,
        "render_config_json": short.render_config_json,
        "video_path": short.video_path, "caption": short.caption,
        "story_link": short.story_link, "status": short.status,
        "renderer": getattr(short, "renderer", "ffmpeg"),
        "created_at": short.created_at, "updated_at": short.updated_at,
    }


@router.get("")
def list_shorts(request: Request, book_id: int | None = None):
    with locked_conn(request) as conn:
        shorts = shorts_repository.list_shorts(conn, book_id=book_id)
        return {"shorts": [_short_payload(s) for s in shorts]}


@router.post("")
def create_short(request: Request, book_id: int = Form(...),
                 resolution: str = Form("1080x1920"),
                 renderer: str = Form("ffmpeg")):
    if resolution not in SHORT_RESOLUTIONS:
        return JSONResponse({"detail": "resolution phải là 1 trong 1080x1920/1080x1080/1920x1080"},
                            status_code=400)
    if renderer not in shorts_repository.SHORT_RENDERERS:
        return JSONResponse({"detail": "renderer phải là ffmpeg|remotion"},
                            status_code=400)
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            return JSONResponse({"detail": "book không tồn tại"}, status_code=404)
        short = shorts_repository.create_short(
            conn, book_id=book_id, resolution=resolution, renderer=renderer,
            voice_id=book.tts_voice_id, music_id=book.music_id)
        shorts_repository.ensure_uploads(conn, short.id)
        return _short_payload(short)


@router.get("/{short_id}")
def get_short_detail(request: Request, short_id: int):
    with locked_conn(request) as conn:
        short = shorts_repository.get_short(conn, short_id)
        if short is None:
            return JSONResponse({"detail": "short không tồn tại"}, status_code=404)
        uploads = shorts_repository.list_uploads(conn, short_id)
        return {**_short_payload(short), "uploads": [
            {"platform": u.platform, "platform_video_id": u.platform_video_id,
             "status": u.status, "error_message": u.error_message} for u in uploads]}


@router.delete("/{short_id}")
def remove_short(request: Request, short_id: int):
    with locked_conn(request) as conn:
        if not shorts_repository.delete_short(conn, short_id):
            return JSONResponse({"detail": "short không tồn tại"}, status_code=404)
        return {"deleted": True}


class ScriptBody(BaseModel):
    script_text: str = ""
    script_source: str = "manual"  # ai | manual
    duration_target: int = 75


@router.post("/{short_id}/script")
def save_script(request: Request, short_id: int, body: ScriptBody):
    if body.script_source not in {"ai", "manual"}:
        return JSONResponse({"detail": "script_source phải là ai|manual"}, status_code=400)
    with locked_conn(request) as conn:
        short = shorts_repository.update_short(
            conn, short_id, script_text=body.script_text,
            script_source=body.script_source,
            duration_target=body.duration_target)
        if short is None:
            return JSONResponse({"detail": "short không tồn tại"}, status_code=404)
        return _short_payload(short)


@router.post("/{short_id}/script/preview")
def preview_script(request: Request, short_id: int):
    """Sinh script AI từ build_book_context (không tự lưu — user bấm lưu)."""
    with locked_conn(request) as conn:
        short = shorts_repository.get_short(conn, short_id)
        if short is None:
            return JSONResponse({"detail": "short không tồn tại"}, status_code=404)
        book = repository.get_book(conn, short.book_id)
        if book is None:
            return JSONResponse({"detail": "book không tồn tại"}, status_code=404)
        from app import ai_content
        try:
            summary = ai_content.run_task(conn, book, "short_script",
                                         params={"save": False})
        except ai_content.ProviderNotConfigured as exc:
            return JSONResponse({"detail": str(exc)}, status_code=400)
        except ai_content.GenerationError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=502)
        try:
            parsed = ai_content.parse_short_script(
                json.dumps(summary.get("result") or {}))
        except ai_content.GenerationError as exc:
            return JSONResponse({"detail": str(exc),
                                 "raw": summary.get("result")}, status_code=502)
        return {"task": "short_script", **parsed}


class RenderBody(BaseModel):
    resolution: str = "1080x1920"
    voice_id: str | None = None
    music_id: int | None = None
    renderer: str = "ffmpeg"  # ffmpeg | remotion (chỉ lớp đồ hoạ dọc)
    render_config: dict = {}


@router.post("/{short_id}/render")
def render_short(request: Request, short_id: int, body: RenderBody):
    if body.renderer not in shorts_repository.SHORT_RENDERERS:
        return JSONResponse({"detail": "renderer phải là ffmpeg|remotion"},
                            status_code=400)
    try:
        validate_short_config({**body.render_config, "resolution": body.resolution})
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    with locked_conn(request) as conn:
        short = shorts_repository.get_short(conn, short_id)
        if short is None:
            return JSONResponse({"detail": "short không tồn tại"}, status_code=404)
        shorts_repository.update_short(
            conn, short_id, resolution=body.resolution,
            voice_id=body.voice_id, music_id=body.music_id,
            renderer=body.renderer,
            render_config_json=json.dumps(body.render_config or {}),
            status="rendering")
        job_id = store.enqueue(
            conn, "short_render", payload={"short_id": short_id},
            book_id=short.book_id,
            dedupe_key=f"short_render:short={short_id}",
            max_attempts=3,
        )
        if job_id is None:
            live = store.find_live_by_dedupe(conn, f"short_render:short={short_id}")
            return {"status": "queued", "job_id": live.id if live else None,
                    "detail": "đã có job render đang chạy"}
        return {"status": "queued", "job_id": job_id}


class CaptionBody(BaseModel):
    caption: str = ""
    story_link: str = ""  # URL tĩnh dán tay, chưa có landing tự sinh


@router.post("/{short_id}/caption")
def save_caption(request: Request, short_id: int, body: CaptionBody):
    with locked_conn(request) as conn:
        short = shorts_repository.update_short(
            conn, short_id, caption=body.caption.strip()[:2000],
            story_link=body.story_link.strip()[:1000])
        if short is None:
            return JSONResponse({"detail": "short không tồn tại"}, status_code=404)
        return _short_payload(short)


# Khoá kênh của short_uploads -> platform của social_account.
_ACCOUNT_PLATFORM = {"fb": "facebook", "tiktok": "tiktok", "youtube": "youtube"}


class PublishBody(BaseModel):
    # {"fb": 3, "youtube": 7}: tài khoản đăng cho từng kênh. Kênh không nêu (hoặc null)
    # giữ lựa chọn đã lưu, mặc định là tài khoản mặc định của mạng đó.
    accounts: dict[str, int | None] = {}


@router.post("/{short_id}/publish")
def publish_short(request: Request, short_id: int, body: PublishBody | None = None):
    """Enqueue upload cả 3 kênh fb|tiktok|youtube (đăng ngay/private-draft)."""
    with locked_conn(request) as conn:
        short = shorts_repository.get_short(conn, short_id)
        if short is None:
            return JSONResponse({"detail": "short không tồn tại"}, status_code=404)
        if not short.video_path:
            return JSONResponse({"detail": "short chưa render xong"}, status_code=400)
        shorts_repository.ensure_uploads(conn, short_id)
        from app import social_accounts
        for channel, account_id in (body.accounts if body else {}).items():
            if channel not in _ACCOUNT_PLATFORM:
                return JSONResponse({"detail": "kênh phải là fb|tiktok|youtube"}, status_code=400)
            if account_id is not None and social_accounts.resolve(
                    conn, _ACCOUNT_PLATFORM[channel], account_id) is None:
                return JSONResponse(
                    {"detail": f"tài khoản {account_id} không thuộc kênh {channel}"},
                    status_code=400)
            shorts_repository.set_upload_account(conn, short_id, channel, account_id)
        shorts_repository.update_short(conn, short_id, status="publishing")
        queued: list[dict] = []
        for platform in ("fb", "tiktok", "youtube"):
            job_id = store.enqueue(
                conn, "short_upload",
                payload={"short_id": short_id, "platform": platform},
                book_id=short.book_id,
                dedupe_key=f"short_upload:short={short_id}:{platform}",
                max_attempts=5,
            )
            if job_id is None:
                live = store.find_live_by_dedupe(
                    conn, f"short_upload:short={short_id}:{platform}")
                queued.append({"platform": platform,
                               "job_id": live.id if live else None,
                               "reused": True})
            else:
                queued.append({"platform": platform, "job_id": job_id,
                               "reused": False})
        return {"status": "publishing", "jobs": queued}


@router.post("/{short_id}/retry/{platform}")
def retry_upload(request: Request, short_id: int, platform: str):
    if platform not in {"fb", "tiktok", "youtube"}:
        return JSONResponse({"detail": "platform phải là fb|tiktok|youtube"},
                            status_code=400)
    with locked_conn(request) as conn:
        short = shorts_repository.get_short(conn, short_id)
        if short is None:
            return JSONResponse({"detail": "short không tồn tại"}, status_code=404)
        shorts_repository.reset_upload(conn, short_id, platform)
        job_id = store.enqueue(
            conn, "short_upload",
            payload={"short_id": short_id, "platform": platform},
            book_id=short.book_id,
            dedupe_key=f"short_upload:short={short_id}:{platform}",
            max_attempts=5,
        )
        return {"status": "queued", "job_id": job_id, "platform": platform}


# --- Credentials FB/TikTok (token dán tay cho MVP; OAuth đầy đủ cần duyệt app) ---

@router.get("/integrations/status")
def integrations_status(request: Request):
    with locked_conn(request) as conn:
        from app import facebook, social_accounts, tiktok, youtube
        accounts = {platform: [social_accounts.public(account)
                               for account in social_accounts.list_accounts(conn, platform)]
                    for platform in social_accounts.PLATFORMS}
        return {
            "facebook": {"connected": facebook.get_credentials(conn) is not None,
                         "accounts": accounts["facebook"]},
            "tiktok": {"connected": tiktok.get_credentials(conn) is not None,
                       "accounts": accounts["tiktok"]},
            "youtube": {"connected": youtube.get_creds_from_db(conn) is not None,
                        "accounts": accounts["youtube"]},
        }


class FBCreds(BaseModel):
    page_id: str
    page_name: str | None = None
    page_access_token: str


@router.post("/integrations/facebook")
def connect_facebook(request: Request, body: FBCreds):
    if not body.page_access_token.strip():
        return JSONResponse({"detail": "thiếu Page Access Token"}, status_code=400)
    with locked_conn(request) as conn:
        from app import facebook
        facebook.save_credentials(conn, page_id=body.page_id.strip(),
                                  page_name=body.page_name,
                                  page_access_token=body.page_access_token.strip())
        return {"connected": True}


class TikTokCreds(BaseModel):
    access_token: str
    open_id: str = ""
    display_name: str | None = None


@router.post("/integrations/tiktok")
def connect_tiktok(request: Request, body: TikTokCreds):
    if not body.access_token.strip():
        return JSONResponse({"detail": "thiếu access token"}, status_code=400)
    with locked_conn(request) as conn:
        from app import tiktok
        tiktok.save_credentials(conn, open_id=body.open_id,
                                display_name=body.display_name,
                                access_token=body.access_token.strip())
        return {"connected": True}
