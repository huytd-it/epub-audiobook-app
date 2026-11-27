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
from app.routes import network

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/socials/api", tags=["socials"])


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
# Đã chuyển sang trang "Mạng & Server" (app/routes/network.py, /network/api). Giữ các
# đường dẫn cũ dưới /socials/api/network làm alias cho client cũ (kể cả bản desktop
# chưa cập nhật): cùng một hàm xử lý, không lặp logic.

for _path, _endpoint, _methods in (
    ("/network", network.network_state, ["GET"]),
    ("/network/endpoints", network.create_endpoint, ["POST"]),
    ("/network/endpoints/{endpoint_id}", network.update_endpoint, ["PATCH"]),
    ("/network/endpoints/{endpoint_id}", network.delete_endpoint, ["DELETE"]),
    ("/network/endpoints/{endpoint_id}/test", network.test_endpoint, ["POST"]),
    ("/network/policy", network.update_policy, ["PUT"]),
):
    router.add_api_route(_path, _endpoint, methods=_methods, include_in_schema=False)
