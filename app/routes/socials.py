"""Socials hub API: tài khoản dùng chung của mọi mạng + cấu hình egress (relay/proxy).

Nằm dưới /socials/api để không đụng trang SPA /socials. Các route đặc thù của YouTube
(uploads, playlist, video kênh) vẫn ở app/routes/youtube.py; ở đây chỉ có phần chung
cho mọi mạng và phần riêng của Facebook/TikTok.

Không response nào ở đây kèm token, mật khẩu proxy hay secret của relay — chỉ có bản
đã che từ social_accounts.public() / egress.public().
"""
from __future__ import annotations

import logging

import requests
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import egress, facebook, social_accounts, tiktok, youtube
from app.deps import locked_conn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/socials/api", tags=["socials"])

# platform của social_account -> khoá kênh trong short_uploads.
_UPLOAD_CHANNEL = {"youtube": "youtube", "facebook": "fb", "tiktok": "tiktok"}


def _account_or_404(conn, account_id: int, platform: str | None = None) -> dict:
    account = social_accounts.get_account(conn, account_id)
    if account is None or (platform is not None and account["platform"] != platform):
        raise HTTPException(404, "Không tìm thấy tài khoản")
    return account


# ------------------------------------------------------------------ tài khoản

@router.get("/overview")
def overview(request: Request):
    """Một lần gọi cho cả hub: tài khoản của từng mạng + mạng nào đã cấu hình OAuth."""
    with locked_conn(request) as conn:
        accounts = {platform: [social_accounts.public(account)
                               for account in social_accounts.list_accounts(conn, platform)]
                    for platform in social_accounts.PLATFORMS}
    return {
        "youtube": {"configured": youtube.is_configured(), "accounts": accounts["youtube"]},
        "facebook": {"configured": True, "accounts": accounts["facebook"]},
        "tiktok": {"configured": True, "accounts": accounts["tiktok"]},
    }


@router.get("/accounts")
def list_accounts(request: Request, platform: str | None = None):
    with locked_conn(request) as conn:
        try:
            rows = social_accounts.list_accounts(conn, platform)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    return {"items": [social_accounts.public(row) for row in rows]}


class AccountUpdate(BaseModel):
    label: str | None = None
    # null = bỏ ghim (theo policy của mạng); không gửi field = giữ nguyên.
    egress_endpoint_id: int | None = None


@router.patch("/accounts/{account_id}")
def update_account(request: Request, account_id: int, body: AccountUpdate):
    with locked_conn(request) as conn:
        _account_or_404(conn, account_id)
        kwargs: dict = {}
        if "egress_endpoint_id" in body.model_fields_set:
            endpoint_id = body.egress_endpoint_id
            if endpoint_id is not None:
                endpoint = egress.get_endpoint(conn, endpoint_id)
                if endpoint is None or endpoint.kind != "proxy":
                    raise HTTPException(400, "Chỉ ghim được outbound proxy đang tồn tại")
            kwargs["egress_endpoint_id"] = endpoint_id
        account = social_accounts.update(conn, account_id, label=body.label, **kwargs)
    return social_accounts.public(account)


@router.post("/accounts/{account_id}/default")
def make_default(request: Request, account_id: int):
    with locked_conn(request) as conn:
        _account_or_404(conn, account_id)
        social_accounts.set_default(conn, account_id)
        return social_accounts.public(social_accounts.get_account(conn, account_id))


@router.delete("/accounts/{account_id}")
def delete_account(request: Request, account_id: int):
    with locked_conn(request) as conn:
        _account_or_404(conn, account_id)
        social_accounts.delete(conn, account_id)
    return {"deleted": account_id}


@router.get("/{platform}/accounts/{account_id}/uploads")
def account_uploads(request: Request, platform: str, account_id: int, limit: int = 100):
    """Lịch sử đăng short của một tài khoản. Dòng chưa gắn tài khoản (đăng bằng mặc
    định, hoặc có từ trước multi-account) được tính cho tài khoản mặc định."""
    if platform not in _UPLOAD_CHANNEL:
        raise HTTPException(404, "Mạng không được hỗ trợ")
    with locked_conn(request) as conn:
        account = _account_or_404(conn, account_id, platform)
        rows = conn.execute(
            """SELECT u.short_id, u.platform_video_id, u.status, u.error_message,
                      u.created_at, s.caption, s.book_id, b.title AS book_title
               FROM short_uploads u
               JOIN shorts s ON s.id = u.short_id
               LEFT JOIN book b ON b.id = s.book_id
               WHERE u.platform = ? AND (u.account_id = ? OR (u.account_id IS NULL AND ?))
               ORDER BY u.created_at DESC, u.id DESC LIMIT ?""",
            (_UPLOAD_CHANNEL[platform], account_id, 1 if account["is_default"] else 0,
             max(1, min(limit, 500))),
        ).fetchall()
    return {"items": [dict(row) for row in rows]}


# ------------------------------------------------------------------ Facebook

class FacebookAccountBody(BaseModel):
    page_id: str = Field(min_length=1)
    page_access_token: str = Field(min_length=1)
    page_name: str | None = None
    # Gọi Graph để xác nhận token đọc được Page trước khi lưu.
    verify: bool = True


def _graph_error(exc: Exception) -> str:
    """Thông điệp lỗi của Graph API, không bao giờ kèm URL (URL chứa access_token)."""
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            message = (response.json().get("error") or {}).get("message")
        except ValueError:
            message = None
        return message or f"HTTP {response.status_code}"
    return type(exc).__name__


@router.post("/facebook/accounts")
def add_facebook_account(request: Request, body: FacebookAccountBody):
    page_id = body.page_id.strip()
    token = body.page_access_token.strip()
    page_name = (body.page_name or "").strip() or None
    if body.verify:
        try:
            with egress.session(None, "facebook") as http:
                page = facebook.fetch_page(page_id=page_id, token=token, http=http)
        except (requests.RequestException, egress.EgressError) as exc:
            raise HTTPException(400, f"Facebook từ chối token/Page này: {_graph_error(exc)}") from exc
        page_id = str(page.get("id") or page_id)
        page_name = page_name or page.get("name")
    with locked_conn(request) as conn:
        account_id = facebook.save_credentials(conn, page_id=page_id, page_name=page_name,
                                               page_access_token=token)
        return social_accounts.public(social_accounts.get_account(conn, account_id))


@router.get("/facebook/accounts/{account_id}/videos")
def facebook_videos(request: Request, account_id: int, limit: int = 25):
    with locked_conn(request) as conn:
        _account_or_404(conn, account_id, "facebook")
        creds = facebook.get_credentials(conn, account_id)
    try:
        with egress.session(None, "facebook", account_id) as http:
            items = facebook.list_page_videos(page_id=creds["page_id"],
                                              token=creds["page_access_token"],
                                              limit=limit, http=http)
    except (requests.RequestException, egress.EgressError) as exc:
        raise HTTPException(502, f"Không lấy được video của Page: {_graph_error(exc)}") from exc
    return {"items": items}


# ------------------------------------------------------------------ TikTok

class TikTokAccountBody(BaseModel):
    access_token: str = Field(min_length=1)
    open_id: str = ""
    display_name: str | None = None
    refresh_token: str = ""
    token_expiry: str = ""


@router.post("/tiktok/accounts")
def add_tiktok_account(request: Request, body: TikTokAccountBody):
    with locked_conn(request) as conn:
        account_id = tiktok.save_credentials(
            conn, open_id=body.open_id.strip(),
            display_name=(body.display_name or "").strip() or None,
            access_token=body.access_token.strip(),
            refresh_token=body.refresh_token.strip(), token_expiry=body.token_expiry.strip(),
        )
        return social_accounts.public(social_accounts.get_account(conn, account_id))


# ------------------------------------------------------------------ mạng: relay / proxy

class EndpointCreate(BaseModel):
    kind: str
    url: str
    label: str = ""
    secret: str = ""
    region: str = ""
    enabled: bool = True


class EndpointUpdate(BaseModel):
    label: str | None = None
    # Rỗng/None = giữ giá trị đang lưu (UI chỉ thấy bản đã che).
    url: str | None = None
    secret: str | None = None
    region: str | None = None
    enabled: bool | None = None


@router.get("/network")
def network_state(request: Request):
    with locked_conn(request) as conn:
        return {
            "endpoints": [egress.public(endpoint) for endpoint in egress.list_endpoints(conn)],
            "policy": egress.get_policy(conn),
            "scopes": list(egress.SCOPES),
            "relay_scopes": sorted(egress.RELAY_SCOPES),
        }


@router.post("/network/endpoints")
def create_endpoint(request: Request, body: EndpointCreate):
    with locked_conn(request) as conn:
        try:
            endpoint_id = egress.create_endpoint(
                conn, kind=body.kind, url=body.url, label=body.label, secret=body.secret,
                region=body.region, enabled=body.enabled)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return egress.public(egress.get_endpoint(conn, endpoint_id))


@router.patch("/network/endpoints/{endpoint_id}")
def update_endpoint(request: Request, endpoint_id: int, body: EndpointUpdate):
    with locked_conn(request) as conn:
        try:
            endpoint = egress.update_endpoint(
                conn, endpoint_id, label=body.label, url=body.url, secret=body.secret,
                region=body.region, enabled=body.enabled)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if endpoint is None:
            raise HTTPException(404, "Không tìm thấy endpoint")
        return egress.public(endpoint)


@router.delete("/network/endpoints/{endpoint_id}")
def delete_endpoint(request: Request, endpoint_id: int):
    with locked_conn(request) as conn:
        if not egress.delete_endpoint(conn, endpoint_id):
            raise HTTPException(404, "Không tìm thấy endpoint")
    return {"deleted": endpoint_id}


@router.post("/network/endpoints/{endpoint_id}/test")
def test_endpoint(request: Request, endpoint_id: int):
    """Gọi thử qua endpoint và trả IP thoát. Chạy ngoài db_lock vì là request mạng."""
    with locked_conn(request) as conn:
        endpoint = egress.get_endpoint(conn, endpoint_id)
    if endpoint is None:
        raise HTTPException(404, "Không tìm thấy endpoint")
    result = egress.test_endpoint(endpoint)
    with locked_conn(request) as conn:
        if result["ok"]:
            egress.mark_ok(conn, endpoint)
        else:
            egress.mark_failed(conn, endpoint, result["error"])
    return result


@router.put("/network/policy")
def update_policy(request: Request, body: dict[str, dict]):
    with locked_conn(request) as conn:
        try:
            return {"policy": egress.set_policy(conn, body)}
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc
