"""Lớp egress: một cửa cho request ra ngoài cần đi qua relay hoặc outbound proxy.

Hai loại điểm thoát (bảng egress_endpoint):

* ``proxy`` — outbound proxy http/https/socks5. Dùng được cho mọi thứ, kể cả upload
  video nhiều GB.
* ``relay`` — Vercel Edge Function trong thư mục relay/. Chỉ hợp với request JSON
  nhỏ: Vercel giới hạn body ~4,5 MB nên upload video không bao giờ đi đường này.

Mỗi luồng request thuộc một *scope* (``ai``, ``youtube``, ``facebook``, ``tiktok``)
và policy của scope (app_state ``egress.policy``) quyết định đi thẳng, qua proxy hay
qua relay. Mặc định mọi scope là ``direct`` — chưa cấu hình gì thì app chạy y như
trước khi có lớp này.

Hai nguyên tắc cố ý:

* **Fail closed.** Scope đã đặt proxy/relay mà không còn điểm thoát nào dùng được thì
  báo lỗi, không lặng lẽ đi thẳng — đi thẳng chính là thứ người dùng muốn tránh.
* **Sticky cho mạng xã hội.** Một tài khoản luôn ra cùng một IP (endpoint ghim trên
  tài khoản, hoặc chọn cố định theo id); xoay vòng theo từng request chỉ dành cho AI.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urlsplit, urlunsplit

import requests

logger = logging.getLogger(__name__)

SCOPES = ("ai", "youtube", "facebook", "tiktok")
# Scope được phép dùng relay. Mạng xã hội thì không: upload vượt giới hạn body của Vercel.
RELAY_SCOPES = frozenset({"ai"})
MODES = ("direct", "proxy", "relay")
STRATEGIES = ("round_robin", "sticky")
KINDS = ("proxy", "relay")
_PROXY_SCHEMES = ("http", "https", "socks4", "socks5", "socks5h")

POLICY_KEY = "egress.policy"
MAX_ATTEMPTS = 3
_BASE_COOLDOWN_SECONDS = 30
_MAX_COOLDOWN_SECONDS = 1800
# Gặp 429 thì chỉ nghỉ ngắn: đó là giới hạn tốc độ, không phải điểm thoát hỏng.
_RATE_LIMIT_COOLDOWN_SECONDS = 20
_IP_ECHO_URL = "https://api.ipify.org?format=json"

# Dấu hiệu provider từ chối vì vùng/IP chứ không phải vì key hay nội dung request.
_GEO_BLOCK_MARKERS = (
    "user location is not supported",
    "unsupported_country_region_territory",
    "country, region, or territory not supported",
    "not available in your country",
    "not available in your region",
    "region is not supported",
)

# main.py đặt đường dẫn DB lúc khởi động. Chưa đặt (test, script) thì mọi request
# không kèm conn đều đi thẳng, không đụng tới file DB nào.
_db_path: str | None = None


class EgressError(RuntimeError):
    """Scope cần đi qua proxy/relay nhưng không có điểm thoát nào dùng được."""


@dataclass(frozen=True)
class Endpoint:
    id: int
    kind: str
    label: str
    url: str
    secret: str
    region: str
    enabled: bool
    fail_count: int
    cooldown_until: str | None
    last_used_at: str | None
    last_error: str | None


def configure(db_path: str | None) -> None:
    global _db_path
    _db_path = db_path if db_path and db_path != ":memory:" else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def _conn(conn: sqlite3.Connection | None):
    """conn của nơi gọi, hoặc một kết nối ngắn hạn tới DB của app, hoặc None."""
    if conn is not None:
        yield conn
        return
    if _db_path is None:
        yield None
        return
    from app import db
    own = db.connect(_db_path)
    try:
        yield own
    finally:
        own.close()


# ------------------------------------------------------------------ endpoint CRUD

def _endpoint(row: sqlite3.Row) -> Endpoint:
    return Endpoint(
        id=row["id"], kind=row["kind"], label=row["label"], url=row["url"],
        secret=row["secret"], region=row["region"], enabled=bool(row["enabled"]),
        fail_count=row["fail_count"], cooldown_until=row["cooldown_until"],
        last_used_at=row["last_used_at"], last_error=row["last_error"],
    )


def _validate_endpoint(kind: str, url: str) -> str:
    if kind not in KINDS:
        raise ValueError("kind phải là proxy hoặc relay")
    url = (url or "").strip()
    parts = urlsplit(url)
    if not parts.hostname:
        raise ValueError("URL không hợp lệ (thiếu host)")
    if kind == "proxy":
        if parts.scheme not in _PROXY_SCHEMES:
            raise ValueError(f"proxy phải dùng một trong: {', '.join(_PROXY_SCHEMES)}")
        if not parts.port:
            raise ValueError("proxy cần ghi rõ cổng, ví dụ http://user:pass@host:8080")
    elif parts.scheme != "https":
        raise ValueError("relay phải là URL https")
    return url.rstrip("/") if kind == "relay" else url


def list_endpoints(conn: sqlite3.Connection, kind: str | None = None) -> list[Endpoint]:
    if kind is None:
        rows = conn.execute("SELECT * FROM egress_endpoint ORDER BY kind, id").fetchall()
    else:
        rows = conn.execute("SELECT * FROM egress_endpoint WHERE kind=? ORDER BY id",
                            (kind,)).fetchall()
    return [_endpoint(row) for row in rows]


def get_endpoint(conn: sqlite3.Connection, endpoint_id: int) -> Endpoint | None:
    row = conn.execute("SELECT * FROM egress_endpoint WHERE id=?", (endpoint_id,)).fetchone()
    return _endpoint(row) if row is not None else None


def create_endpoint(conn: sqlite3.Connection, *, kind: str, url: str, label: str = "",
                    secret: str = "", region: str = "", enabled: bool = True) -> int:
    url = _validate_endpoint(kind, url)
    if kind == "relay" and not secret.strip():
        raise ValueError("relay cần secret (trùng RELAY_SECRET của bản deploy)")
    now = _now().isoformat()
    cur = conn.execute(
        """INSERT INTO egress_endpoint (kind, label, url, secret, region, enabled,
                                        created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (kind, label.strip(), url, secret.strip(), region.strip(), int(enabled), now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_endpoint(conn: sqlite3.Connection, endpoint_id: int, *, label: str | None = None,
                    url: str | None = None, secret: str | None = None,
                    region: str | None = None, enabled: bool | None = None) -> Endpoint | None:
    """Sửa một endpoint. url/secret để None (hoặc rỗng) nghĩa là giữ giá trị đang lưu —
    UI chỉ nhận được bản đã che nên không thể gửi lại nguyên văn."""
    current = get_endpoint(conn, endpoint_id)
    if current is None:
        return None
    fields: dict[str, object] = {}
    if label is not None:
        fields["label"] = label.strip()
    if url:
        fields["url"] = _validate_endpoint(current.kind, url)
    if secret:
        fields["secret"] = secret.strip()
    if region is not None:
        fields["region"] = region.strip()
    if enabled is not None:
        fields["enabled"] = int(enabled)
        if enabled:
            fields.update(fail_count=0, cooldown_until=None, last_error=None)
    if fields:
        fields["updated_at"] = _now().isoformat()
        assignments = ", ".join(f"{name}=?" for name in fields)
        conn.execute(f"UPDATE egress_endpoint SET {assignments} WHERE id=?",
                     (*fields.values(), endpoint_id))
        conn.commit()
    return get_endpoint(conn, endpoint_id)


def delete_endpoint(conn: sqlite3.Connection, endpoint_id: int) -> bool:
    """Xoá endpoint và gỡ nó khỏi policy lẫn các tài khoản đang ghim."""
    if get_endpoint(conn, endpoint_id) is None:
        return False
    conn.execute("DELETE FROM egress_endpoint WHERE id=?", (endpoint_id,))
    conn.execute("UPDATE social_account SET egress_endpoint_id=NULL WHERE egress_endpoint_id=?",
                 (endpoint_id,))
    policy = get_policy(conn)
    for rule in policy.values():
        rule["endpoint_ids"] = [i for i in rule["endpoint_ids"] if i != endpoint_id]
    _save_policy(conn, policy)
    conn.commit()
    return True


def mask_url(url: str) -> str:
    """Che mật khẩu trong URL proxy: http://user:***@host:port."""
    parts = urlsplit(url)
    if not parts.password:
        return url
    host = parts.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{parts.username}:***@{host}"
    if parts.port:
        netloc += f":{parts.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def public(endpoint: Endpoint) -> dict:
    """Bản an toàn để trả ra API: không kèm mật khẩu proxy hay secret của relay."""
    cooling = bool(endpoint.cooldown_until and endpoint.cooldown_until > _now().isoformat())
    return {
        "id": endpoint.id,
        "kind": endpoint.kind,
        "label": endpoint.label,
        "url": mask_url(endpoint.url),
        "has_secret": bool(endpoint.secret),
        "region": endpoint.region,
        "enabled": endpoint.enabled,
        "fail_count": endpoint.fail_count,
        "cooldown_until": endpoint.cooldown_until if cooling else None,
        "last_used_at": endpoint.last_used_at,
        "last_error": endpoint.last_error,
    }


# ------------------------------------------------------------------ policy

def _default_rule(scope: str) -> dict:
    return {"mode": "direct",
            "strategy": "round_robin" if scope == "ai" else "sticky",
            "endpoint_ids": []}


def get_policy(conn: sqlite3.Connection) -> dict[str, dict]:
    row = conn.execute("SELECT value FROM app_state WHERE key=?", (POLICY_KEY,)).fetchone()
    try:
        stored = json.loads(row["value"]) if row and row["value"] else {}
    except (ValueError, TypeError):
        stored = {}
    policy: dict[str, dict] = {}
    for scope in SCOPES:
        rule = _default_rule(scope)
        raw = stored.get(scope) if isinstance(stored, dict) else None
        if isinstance(raw, dict):
            if raw.get("mode") in MODES:
                rule["mode"] = raw["mode"]
            if raw.get("strategy") in STRATEGIES:
                rule["strategy"] = raw["strategy"]
            if isinstance(raw.get("endpoint_ids"), list):
                rule["endpoint_ids"] = [int(i) for i in raw["endpoint_ids"]
                                        if isinstance(i, int) or str(i).isdigit()]
        policy[scope] = rule
    return policy


def _save_policy(conn: sqlite3.Connection, policy: dict[str, dict]) -> None:
    conn.execute(
        """INSERT INTO app_state (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (POLICY_KEY, json.dumps(policy)),
    )


def set_policy(conn: sqlite3.Connection, updates: dict[str, dict]) -> dict[str, dict]:
    """Ghi đè policy của các scope có trong ``updates``; scope khác giữ nguyên."""
    policy = get_policy(conn)
    kinds = {endpoint.id: endpoint.kind for endpoint in list_endpoints(conn)}
    for scope, raw in (updates or {}).items():
        if scope not in SCOPES:
            raise ValueError(f"scope không hợp lệ: {scope}")
        rule = dict(policy[scope])
        mode = raw.get("mode", rule["mode"])
        if mode not in MODES:
            raise ValueError(f"mode không hợp lệ: {mode}")
        if mode == "relay" and scope not in RELAY_SCOPES:
            raise ValueError(
                f"scope {scope} không dùng được relay: Vercel giới hạn body ~4,5 MB nên "
                "upload video phải đi thẳng hoặc qua outbound proxy")
        strategy = raw.get("strategy", rule["strategy"])
        if strategy not in STRATEGIES:
            raise ValueError(f"strategy không hợp lệ: {strategy}")
        endpoint_ids = [int(i) for i in raw.get("endpoint_ids", rule["endpoint_ids"])]
        for endpoint_id in endpoint_ids:
            if endpoint_id not in kinds:
                raise ValueError(f"endpoint {endpoint_id} không tồn tại")
            if mode != "direct" and kinds[endpoint_id] != mode:
                raise ValueError(f"endpoint {endpoint_id} không phải loại {mode}")
        policy[scope] = {"mode": mode, "strategy": strategy, "endpoint_ids": endpoint_ids}
    _save_policy(conn, policy)
    conn.commit()
    return policy


# ------------------------------------------------------------------ chọn điểm thoát

def _pinned_endpoint_id(conn: sqlite3.Connection, account_id: int | None) -> int | None:
    if account_id is None:
        return None
    row = conn.execute("SELECT egress_endpoint_id FROM social_account WHERE id=?",
                       (account_id,)).fetchone()
    return row["egress_endpoint_id"] if row is not None else None


def pick(conn: sqlite3.Connection | None, scope: str, account_id: int | None = None,
         exclude: tuple[int, ...] = ()) -> Endpoint | None:
    """Chọn điểm thoát cho một request. None nghĩa là đi thẳng.

    Raise EgressError khi scope cần proxy/relay mà không còn điểm thoát nào.
    """
    if scope not in SCOPES:
        raise ValueError(f"scope không hợp lệ: {scope}")
    if conn is None:
        return None
    rule = get_policy(conn)[scope]
    mode = rule["mode"]
    if mode == "direct":
        return None

    pinned_id = _pinned_endpoint_id(conn, account_id)
    if pinned_id is not None:
        # Tài khoản đã ghim thì chỉ ra đúng IP đó, kể cả khi nó đang cooldown: đổi IP
        # giữa chừng với mạng xã hội còn tệ hơn một lần thử lại thất bại.
        pinned = get_endpoint(conn, pinned_id)
        if pinned is None or not pinned.enabled or pinned.kind != mode:
            raise EgressError(
                f"Proxy ghim cho tài khoản {account_id} không dùng được "
                "(đã tắt, đã xoá hoặc sai loại). Kiểm tra trang Mạng & Proxy.")
        if pinned.id in exclude:
            raise EgressError(f"Proxy ghim cho tài khoản {account_id} đang lỗi: {pinned.last_error}")
        return pinned

    candidates = [endpoint for endpoint in list_endpoints(conn, mode)
                  if endpoint.enabled and endpoint.id not in exclude
                  and (not rule["endpoint_ids"] or endpoint.id in rule["endpoint_ids"])]
    if not candidates:
        raise EgressError(
            f"Scope {scope} đang đặt {mode} nhưng không có điểm thoát nào dùng được. "
            "Thêm/bật endpoint ở trang Mạng & Proxy hoặc chuyển scope về direct.")

    if rule["strategy"] == "sticky" and account_id is not None:
        # Cố định theo id tài khoản trên toàn bộ danh sách (không lọc cooldown) để một
        # endpoint tạm lỗi không làm tài khoản nhảy sang IP khác.
        stable = sorted(candidates, key=lambda endpoint: endpoint.id)
        return stable[int(account_id) % len(stable)]

    now = _now().isoformat()
    ready = [endpoint for endpoint in candidates
             if not endpoint.cooldown_until or endpoint.cooldown_until <= now]
    if ready:
        # Xoay vòng theo id, nhớ cái vừa dùng của từng scope trong app_state. Không sắp
        # theo last_used_at: hai lượt chọn liền nhau có thể trùng dấu thời gian (đồng hồ
        # Windows nhảy từng mili-giây) và khi đó luôn rơi về cùng một endpoint.
        pointer_key = f"egress.rr.{scope}"
        row = conn.execute("SELECT value FROM app_state WHERE key=?", (pointer_key,)).fetchone()
        last_id = int(row["value"]) if row and str(row["value"]).isdigit() else 0
        ordered = sorted(ready, key=lambda endpoint: endpoint.id)
        chosen = next((endpoint for endpoint in ordered if endpoint.id > last_id), ordered[0])
        conn.execute(
            """INSERT INTO app_state (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (pointer_key, str(chosen.id)),
        )
    else:
        # Tất cả đang cooldown: lấy cái hết hạn sớm nhất thay vì bỏ cuộc.
        chosen = min(candidates, key=lambda endpoint: (endpoint.cooldown_until or "", endpoint.id))
    conn.execute("UPDATE egress_endpoint SET last_used_at=? WHERE id=?", (now, chosen.id))
    conn.commit()
    return chosen


def mark_ok(conn: sqlite3.Connection | None, endpoint: Endpoint | None) -> None:
    if conn is None or endpoint is None or (not endpoint.fail_count and not endpoint.cooldown_until):
        return
    conn.execute(
        "UPDATE egress_endpoint SET fail_count=0, cooldown_until=NULL, last_error=NULL WHERE id=?",
        (endpoint.id,),
    )
    conn.commit()


def mark_failed(conn: sqlite3.Connection | None, endpoint: Endpoint | None, error: str,
                *, cooldown_seconds: int | None = None) -> None:
    if conn is None or endpoint is None:
        return
    row = conn.execute("SELECT fail_count FROM egress_endpoint WHERE id=?",
                       (endpoint.id,)).fetchone()
    if row is None:
        return
    fail_count = row["fail_count"] + 1
    if cooldown_seconds is None:
        cooldown_seconds = min(_BASE_COOLDOWN_SECONDS * 2 ** (fail_count - 1),
                               _MAX_COOLDOWN_SECONDS)
    until = (_now() + timedelta(seconds=cooldown_seconds)).isoformat()
    conn.execute(
        "UPDATE egress_endpoint SET fail_count=?, cooldown_until=?, last_error=? WHERE id=?",
        (fail_count, until, (error or "")[:500], endpoint.id),
    )
    conn.commit()
    logger.warning("event=egress.endpoint_failed id=%s kind=%s cooldown=%ss error=%s",
                   endpoint.id, endpoint.kind, cooldown_seconds, (error or "")[:200])


# ------------------------------------------------------------------ gửi request

def proxies_for(endpoint: Endpoint | None) -> dict | None:
    if endpoint is None or endpoint.kind != "proxy":
        return None
    return {"http": endpoint.url, "https": endpoint.url}


def session(conn: sqlite3.Connection | None, scope: str,
            account_id: int | None = None) -> requests.Session:
    """Session cho cả một phiên làm việc (vd. một lần upload): chọn điểm thoát một lần
    rồi giữ nguyên, để mọi request của phiên ra cùng một IP."""
    with _conn(conn) as active:
        endpoint = pick(active, scope, account_id)
    if endpoint is not None and endpoint.kind != "proxy":
        raise EgressError(f"Scope {scope} chỉ hỗ trợ đi thẳng hoặc outbound proxy.")
    http = requests.Session()
    if endpoint is not None:
        http.proxies = proxies_for(endpoint)
        # Proxy đã cấu hình thì không để biến môi trường HTTP(S)_PROXY chen vào.
        http.trust_env = False
    return http


def _relay_call(endpoint: Endpoint, method: str, url: str, kwargs: dict) -> requests.Response:
    kwargs = dict(kwargs)
    params = kwargs.pop("params", None)
    if params:
        url = requests.Request("GET", url, params=params).prepare().url
    headers = dict(kwargs.pop("headers", None) or {})
    headers["x-relay-target"] = url
    headers["x-relay-key"] = endpoint.secret
    return requests.request(method, f"{endpoint.url}/api/relay", headers=headers, **kwargs)


def _blocked_reason(response: requests.Response, endpoint: Endpoint) -> tuple[str, int | None] | None:
    """(lý do, cooldown) nếu response cho thấy nên đổi điểm thoát; None nếu là câu trả
    lời thật của provider (kể cả 4xx do key sai hay request sai)."""
    status = response.status_code
    if endpoint.kind == "relay" and response.headers.get("x-relay-error"):
        return f"relay từ chối: {response.headers['x-relay-error']}", None
    if status == 407:
        return "proxy đòi xác thực (407)", None
    if status == 429:
        return "provider giới hạn tốc độ (429)", _RATE_LIMIT_COOLDOWN_SECONDS
    if status in (400, 403, 451):
        body = (response.text or "")[:2000].lower()
        if status == 451 or any(marker in body for marker in _GEO_BLOCK_MARKERS):
            return f"provider chặn theo vùng (HTTP {status})", None
    return None


def request(scope: str, method: str, url: str, *, conn: sqlite3.Connection | None = None,
            account_id: int | None = None, **kwargs) -> requests.Response:
    """``requests.request`` đi qua điểm thoát của scope, có đổi điểm thoát khi hỏng.

    Đi thẳng thì gọi đúng ``requests.<method>(url, **kwargs)`` như mã cũ. Qua
    proxy/relay thì thử tối đa MAX_ATTEMPTS điểm thoát khác nhau; lỗi kết nối, 429 và
    chặn theo vùng làm điểm thoát đó nghỉ một lúc rồi chuyển sang cái kế. Hết lượt mà
    vẫn bị từ chối thì trả response cuối cùng để nơi gọi báo lỗi như bình thường.
    """
    with _conn(conn) as active:
        tried: list[int] = []
        last_response: requests.Response | None = None
        last_error: Exception | None = None
        for _attempt in range(MAX_ATTEMPTS):
            try:
                endpoint = pick(active, scope, account_id, exclude=tuple(tried))
            except EgressError:
                if last_response is not None:
                    return last_response
                if last_error is not None:
                    raise last_error
                raise
            if endpoint is None:
                return getattr(requests, method.lower())(url, **kwargs)
            tried.append(endpoint.id)
            try:
                if endpoint.kind == "relay":
                    response = _relay_call(endpoint, method, url, kwargs)
                else:
                    response = requests.request(method, url, proxies=proxies_for(endpoint), **kwargs)
            except (requests.ConnectionError, requests.Timeout) as exc:
                mark_failed(active, endpoint, f"{type(exc).__name__}: {exc}")
                last_error = exc
                continue
            blocked = _blocked_reason(response, endpoint)
            if blocked is None:
                mark_ok(active, endpoint)
                return response
            mark_failed(active, endpoint, blocked[0], cooldown_seconds=blocked[1])
            last_response = response
        if last_response is not None:
            return last_response
        assert last_error is not None
        raise last_error


def httplib2_http(conn: sqlite3.Connection | None, scope: str, account_id: int | None = None):
    """``httplib2.Http`` đi qua proxy của scope cho googleapiclient; None = đi thẳng."""
    with _conn(conn) as active:
        endpoint = pick(active, scope, account_id)
    if endpoint is None:
        return None
    if endpoint.kind != "proxy":
        raise EgressError(f"Scope {scope} chỉ hỗ trợ đi thẳng hoặc outbound proxy.")
    import httplib2
    try:
        import socks
    except ImportError as exc:
        raise EgressError("Thiếu gói PySocks để googleapiclient đi qua proxy "
                          "(pip install pysocks).") from exc
    parts = urlsplit(endpoint.url)
    proxy_type = {
        "http": socks.PROXY_TYPE_HTTP, "https": socks.PROXY_TYPE_HTTP,
        "socks4": socks.PROXY_TYPE_SOCKS4,
        "socks5": socks.PROXY_TYPE_SOCKS5, "socks5h": socks.PROXY_TYPE_SOCKS5,
    }[parts.scheme]
    info = httplib2.ProxyInfo(
        proxy_type=proxy_type,
        proxy_host=parts.hostname,
        proxy_port=parts.port,
        proxy_rdns=True,
        proxy_user=unquote(parts.username) if parts.username else None,
        proxy_pass=unquote(parts.password) if parts.password else None,
    )
    return httplib2.Http(proxy_info=info)


def test_endpoint(endpoint: Endpoint, timeout: float = 15.0) -> dict:
    """Gọi một dịch vụ trả IP qua endpoint để thấy IP thoát thật. Không raise."""
    try:
        if endpoint.kind == "relay":
            response = _relay_call(endpoint, "GET", _IP_ECHO_URL, {"timeout": timeout})
        else:
            response = requests.get(_IP_ECHO_URL, proxies=proxies_for(endpoint), timeout=timeout)
        if response.status_code >= 400:
            detail = response.headers.get("x-relay-error") or (response.text or "")[:200]
            return {"ok": False, "error": f"HTTP {response.status_code}: {detail}"}
        return {"ok": True, "ip": str(response.json().get("ip") or ""),
                "elapsed_ms": int(response.elapsed.total_seconds() * 1000)}
    except Exception as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}


# pytest gom mọi hàm test_* ở cấp module; đây là mã ứng dụng, không phải test.
test_endpoint.__test__ = False  # type: ignore[attr-defined]
