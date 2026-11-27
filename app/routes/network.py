"""Trang "Mạng & Server": endpoint egress (relay/proxy, vd. server 2 qua LAN +
Tailscale) và định tuyến theo luồng (ai, youtube, facebook, tiktok, kaggle, drive).

Nằm dưới /network/api để không đụng trang SPA /network. /socials/api/network/* vẫn
được giữ làm alias (app/routes/socials.py gọi lại đúng các hàm ở đây) cho client cũ.

Không response nào kèm mật khẩu proxy hay secret của relay — chỉ bản đã che từ
egress.public().
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from app import egress, social_accounts
from app.deps import locked_conn

router = APIRouter(prefix="/network/api", tags=["network"])

# Scope egress mà tài khoản của từng mạng xã hội đi qua (ghim endpoint theo tài khoản).
_PLATFORM_SCOPE = {"youtube": "youtube", "facebook": "facebook", "tiktok": "tiktok"}


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


def pinned_conflicts(conn) -> list[dict]:
    """Tài khoản mạng xã hội đang ghim một endpoint KHÔNG thuộc định tuyến của scope
    (endpoint không có trong endpoint_ids, hoặc scope đang direct nên ghim bị bỏ qua).
    Chỉ cảnh báo: người dùng tự bỏ ghim ở Socials."""
    policy = egress.get_policy(conn)
    endpoints = {endpoint.id: endpoint for endpoint in egress.list_endpoints(conn)}
    conflicts = []
    for account in social_accounts.list_accounts(conn):
        endpoint_id = account.get("egress_endpoint_id")
        scope = _PLATFORM_SCOPE.get(account["platform"])
        if endpoint_id is None or scope is None:
            continue
        rule = policy[scope]
        if rule["mode"] == "direct":
            reason = f"Luồng {scope} đang đi thẳng nên proxy ghim của tài khoản này bị bỏ qua"
        elif rule["endpoint_ids"] and endpoint_id not in rule["endpoint_ids"]:
            reason = f"Endpoint ghim không nằm trong danh sách endpoint của luồng {scope}"
        else:
            continue
        endpoint = endpoints.get(endpoint_id)
        conflicts.append({
            "account_id": account["id"],
            "platform": account["platform"],
            "label": account.get("label") or account.get("display_name") or account.get("external_id") or "",
            "endpoint_id": endpoint_id,
            "endpoint_label": endpoint.label if endpoint else "",
            "scope": scope,
            "reason": reason,
        })
    return conflicts


@router.get("")
def network_state(request: Request):
    with locked_conn(request) as conn:
        return {
            "endpoints": [egress.public(endpoint) for endpoint in egress.list_endpoints(conn)],
            "policy": egress.get_policy(conn),
            "scopes": list(egress.SCOPES),
            "relay_scopes": sorted(egress.RELAY_SCOPES),
            "pinned_conflicts": pinned_conflicts(conn),
        }


@router.post("/endpoints")
def create_endpoint(request: Request, body: EndpointCreate):
    with locked_conn(request) as conn:
        try:
            endpoint_id = egress.create_endpoint(
                conn, kind=body.kind, url=body.url, label=body.label, secret=body.secret,
                region=body.region, enabled=body.enabled)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        return egress.public(egress.get_endpoint(conn, endpoint_id))


@router.patch("/endpoints/{endpoint_id}")
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


@router.delete("/endpoints/{endpoint_id}")
def delete_endpoint(request: Request, endpoint_id: int):
    with locked_conn(request) as conn:
        if not egress.delete_endpoint(conn, endpoint_id):
            raise HTTPException(404, "Không tìm thấy endpoint")
    return {"deleted": endpoint_id}


@router.post("/endpoints/{endpoint_id}/test")
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


@router.put("/policy")
def update_policy(request: Request, body: dict[str, dict]):
    with locked_conn(request) as conn:
        try:
            return {"policy": egress.set_policy(conn, body)}
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from exc


# pytest gom mọi hàm test_* ở cấp module; đây là route, không phải test.
test_endpoint.__test__ = False  # type: ignore[attr-defined]
