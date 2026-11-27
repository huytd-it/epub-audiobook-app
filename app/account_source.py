"""Kho tài khoản trên server 2: Kaggle (kaggle.json) và YouTube (refresh token).

Server 2 phục vụ một thư mục bằng HTTP file server của gost (Basic Auth, chỉ LAN +
Tailscale)::

    accounts/
      kaggle/<tên>.json    {"username": "...", "key": "..."}          (kaggle.json gốc)
      youtube/<tên>.json   {"refresh_token": "...", "client_id"?: "...",
                            "client_secret"?: "...", "channel_id"?: "...",
                            "channel_title"?: "...", "label"?: "..."}

"Nạp từ server 2" chỉ ghi **metadata** vào DB (username, nhãn, kênh) với
source='server2' + remote_ref, để mọi ô chọn tài khoản thấy chúng. Key/token
**không** lưu vào DB: mỗi lần job cần thì đọc lại từ server 2 và chỉ giữ trong bộ nhớ
(cache ngắn). Server 2 không vào được → egress.EgressUnavailable → job được hoãn.

Kết nối tới kho là kết nối LAN/Tailscale trực tiếp (không qua proxy egress).
"""
from __future__ import annotations

import json
import logging
import re
import socket
import sqlite3
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, unquote, urljoin, urlsplit

import requests

from app import egress

logger = logging.getLogger(__name__)

CONFIG_KEY = "account_source.server2"
SOURCE = "server2"
_TIMEOUT = 10.0
_CACHE_TTL = 300.0
_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()
_last_good: str | None = None


class AccountSourceError(RuntimeError):
    """Kho trả lời nhưng không dùng được (sai user/pass, file mất, JSON hỏng)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ------------------------------------------------------------------ cấu hình

def get_config(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT value FROM app_state WHERE key=?", (CONFIG_KEY,)).fetchone()
    try:
        raw = json.loads(row["value"]) if row and row["value"] else {}
    except (TypeError, ValueError):
        raw = {}
    urls = [str(u).strip().rstrip("/") for u in raw.get("urls") or [] if str(u).strip()]
    return {"urls": urls, "username": str(raw.get("username") or ""),
            "password": str(raw.get("password") or "")}


def public_config(conn: sqlite3.Connection) -> dict:
    cfg = get_config(conn)
    return {"urls": cfg["urls"], "username": cfg["username"], "has_password": bool(cfg["password"]),
            "configured": bool(cfg["urls"])}


def set_config(conn: sqlite3.Connection, *, urls: list[str], username: str,
               password: str | None) -> dict:
    """password None/rỗng = giữ mật khẩu đang lưu (UI không nhận lại được nó)."""
    clean = []
    for url in urls or []:
        url = str(url).strip().rstrip("/")
        if not url:
            continue
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or not parts.port:
            raise ValueError(f"URL kho không hợp lệ: {url} (vd. http://100.102.234.19:1081)")
        clean.append(url)
    current = get_config(conn)
    value = {"urls": clean, "username": (username or "").strip(),
             "password": password if password else current["password"]}
    conn.execute(
        """INSERT INTO app_state (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (CONFIG_KEY, json.dumps(value)),
    )
    conn.commit()
    clear_cache()
    return public_config(conn)


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()


# ------------------------------------------------------------------ HTTP

def _reachable(url: str, timeout: float = 2.0) -> bool:
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=timeout):
            return True
    except OSError:
        return False


def _get(cfg: dict, path: str) -> requests.Response:
    """GET path trên kho, thử lần lượt từng URL (LAN ↔ Tailscale) có kiểm tra TCP."""
    global _last_good
    if not cfg["urls"]:
        raise AccountSourceError("Chưa cấu hình kho tài khoản server 2 (trang Mạng & Server).")
    errors = []
    # URL vừa chạy được thử trước: LAN rớt thì không phải chờ ~2s mỗi lần đọc.
    ordered = sorted(cfg["urls"], key=lambda url: url != _last_good)
    for base in ordered:
        if not _reachable(base):
            errors.append(f"{base}: không mở được TCP")
            continue
        session = requests.Session()
        session.trust_env = False  # kết nối LAN/Tailscale trực tiếp, không qua HTTP(S)_PROXY
        try:
            response = session.get(urljoin(base + "/", path), timeout=_TIMEOUT,
                                   auth=(cfg["username"], cfg["password"]) if cfg["username"] else None)
        except requests.RequestException as exc:
            errors.append(f"{base}: {type(exc).__name__}: {exc}")
            continue
        if response.status_code in (401, 403):
            raise AccountSourceError("Kho server 2 từ chối: sai user/pass.")
        if response.status_code == 404:
            raise AccountSourceError(f"Không có '{path}' trên kho server 2.")
        if response.status_code >= 400:
            errors.append(f"{base}: HTTP {response.status_code}")
            continue
        _last_good = base
        return response
    raise egress.EgressUnavailable("Kho tài khoản server 2 không vào được: " + "; ".join(errors))


_HREF = re.compile(r'href="([^"?#]+)"', re.IGNORECASE)


def list_files(cfg: dict, folder: str) -> list[str]:
    """Tên các file .json trong accounts/<folder>/ (đọc trang liệt kê của file server)."""
    response = _get(cfg, f"{folder}/")
    try:
        data = response.json()
        if isinstance(data, list):
            names = [str(x) for x in data]
        else:
            names = []
    except ValueError:
        names = [unquote(href) for href in _HREF.findall(response.text)]
    return sorted({n.rsplit("/", 1)[-1] for n in names if n.lower().endswith(".json")})


def read_json(cfg: dict, ref: str, *, use_cache: bool = True) -> dict:
    with _cache_lock:
        hit = _cache.get(ref)
        if use_cache and hit and hit[0] > time.monotonic():
            return dict(hit[1])  # type: ignore[arg-type]
    response = _get(cfg, quote(ref))
    try:
        data = response.json()
    except ValueError as exc:
        raise AccountSourceError(f"'{ref}' trên server 2 không phải JSON hợp lệ.") from exc
    if not isinstance(data, dict):
        raise AccountSourceError(f"'{ref}' trên server 2 phải là một object JSON.")
    with _cache_lock:
        _cache[ref] = (time.monotonic() + _CACHE_TTL, dict(data))
    return data


# ------------------------------------------------------------------ dùng lúc chạy

def kaggle_key(conn: sqlite3.Connection, account: dict) -> str:
    """API key của một tài khoản Kaggle: lưu local thì lấy từ DB, nguồn server 2 thì
    đọc file trên server 2 (không ghi vào DB)."""
    if account.get("source") != SOURCE:
        return account["api_key"]
    data = read_json(get_config(conn), account["remote_ref"])
    key = str(data.get("key") or "").strip()
    if not key:
        raise AccountSourceError(f"'{account['remote_ref']}' thiếu trường key.")
    if str(data.get("username") or "").strip() not in ("", account["username"]):
        raise AccountSourceError(
            f"'{account['remote_ref']}' giờ là của user {data.get('username')}, không phải "
            f"{account['username']}. Bấm 'Nạp từ server 2' để cập nhật.")
    return key


def youtube_tokens(conn: sqlite3.Connection, account: dict) -> dict:
    """{refresh_token, client_id?, client_secret?} của một kênh nguồn server 2."""
    data = read_json(get_config(conn), account["remote_ref"])
    if not str(data.get("refresh_token") or "").strip():
        raise AccountSourceError(f"'{account['remote_ref']}' thiếu refresh_token.")
    return {"refresh_token": data["refresh_token"].strip(),
            "client_id": str(data.get("client_id") or "").strip() or None,
            "client_secret": str(data.get("client_secret") or "").strip() or None}


# ------------------------------------------------------------------ đồng bộ metadata

def _label_from(name: str) -> str:
    return name[:-5] if name.lower().endswith(".json") else name


def sync(conn: sqlite3.Connection, *, discover_channel=None) -> dict:
    """Đọc danh sách trên server 2 và cập nhật metadata trong DB. Không ghi key/token.

    discover_channel(refresh_token, client_id, client_secret) -> (channel_id, title):
    dùng khi file YouTube không ghi sẵn channel_id (gọi YouTube API qua egress)."""
    cfg = get_config(conn)
    clear_cache()
    summary = {"kaggle": {"added": 0, "updated": 0, "removed": 0, "skipped": []},
               "youtube": {"added": 0, "updated": 0, "removed": 0, "skipped": []}}

    # --- Kaggle: file ở GỐC thư mục được phục vụ (cài "như bình thường": kaggle.json, vd.
    # khi gost phục vụ thẳng ~/.kaggle) và/hoặc trong thư mục con kaggle/.
    refs: list[str] = []
    kaggle_listed = False
    for folder in ("", "kaggle"):
        try:
            names = list_files(cfg, folder)
        except AccountSourceError:
            continue  # thư mục con kaggle/ không có là bình thường
        kaggle_listed = True
        refs += [f"{folder}/{n}" if folder else n for n in names]
    summary["kaggle"]["files"] = len(refs)
    seen = set()
    for ref in refs:
        name = ref.rsplit("/", 1)[-1]
        at_root = "/" not in ref
        try:
            data = read_json(cfg, ref, use_cache=False)
        except AccountSourceError as exc:
            summary["kaggle"]["skipped"].append({"file": ref, "reason": str(exc)})
            continue
        username = str(data.get("username") or "").strip()
        if not username or not str(data.get("key") or "").strip():
            if not at_root:  # ở gốc có thể là file khác: bỏ qua im lặng
                summary["kaggle"]["skipped"].append({"file": ref, "reason": "thiếu username hoặc key"})
            continue
        # kaggle.json mặc định thì tên file không nói gì: lấy username làm nhãn.
        default_name = name.lower() == "kaggle.json"
        label = str(data.get("label") or "").strip() or (username if default_name else _label_from(name))
        seen.add(ref)
        row = conn.execute("SELECT id, source FROM kaggle_account WHERE remote_ref=? AND source=?",
                           (ref, SOURCE)).fetchone()
        local_same = conn.execute("SELECT id FROM kaggle_account WHERE username=? AND source!=?",
                                  (username, SOURCE)).fetchone()
        if row is None and local_same is not None:
            summary["kaggle"]["skipped"].append(
                {"file": ref, "reason": f"đã có tài khoản local cùng username {username}; giữ bản local"})
            continue
        try:
            if row is None:
                conn.execute(
                    """INSERT INTO kaggle_account (label, username, api_key, source, remote_ref,
                                                   created_at, updated_at)
                       VALUES (?, ?, '', ?, ?, ?, ?)""",
                    (label, username, SOURCE, ref, _now(), _now()))
                summary["kaggle"]["added"] += 1
            else:
                conn.execute("UPDATE kaggle_account SET label=?, username=?, api_key='', updated_at=? WHERE id=?",
                             (label, username, _now(), row["id"]))
                summary["kaggle"]["updated"] += 1
        except sqlite3.IntegrityError:
            # username là UNIQUE: hai file trên server 2 cùng một tài khoản Kaggle.
            if row is None:
                seen.discard(ref)
            summary["kaggle"]["skipped"].append(
                {"file": ref, "reason": f"trùng username {username} với một file khác trên server 2"})
    for row in conn.execute("SELECT id, remote_ref, in_use_by_job_id FROM kaggle_account WHERE source=?",
                            (SOURCE,)).fetchall() if kaggle_listed else []:
        if row["remote_ref"] in seen:
            continue
        if row["in_use_by_job_id"] is not None:
            summary["kaggle"]["skipped"].append(
                {"file": row["remote_ref"], "reason": "không còn trên server 2 nhưng đang chạy job; giữ tới khi xong"})
            continue
        conn.execute("DELETE FROM kaggle_account WHERE id=?", (row["id"],))
        summary["kaggle"]["removed"] += 1
    conn.commit()

    # --- YouTube
    try:
        youtube_files, youtube_listed = list_files(cfg, "youtube"), True
    except AccountSourceError:
        youtube_files, youtube_listed = [], False  # chưa có thư mục youtube/ là bình thường
    summary["youtube"]["files"] = len(youtube_files)
    seen = set()
    for name in youtube_files:
        ref = f"youtube/{name}"
        try:
            data = read_json(cfg, ref, use_cache=False)
        except AccountSourceError as exc:
            summary["youtube"]["skipped"].append({"file": ref, "reason": str(exc)})
            continue
        refresh = str(data.get("refresh_token") or "").strip()
        if not refresh:
            summary["youtube"]["skipped"].append({"file": ref, "reason": "thiếu refresh_token"})
            continue
        channel_id = str(data.get("channel_id") or "").strip()
        title = str(data.get("channel_title") or "").strip() or None
        if not channel_id:
            if discover_channel is None:
                summary["youtube"]["skipped"].append({"file": ref, "reason": "thiếu channel_id"})
                continue
            try:
                channel_id, discovered = discover_channel(
                    refresh, str(data.get("client_id") or "").strip() or None,
                    str(data.get("client_secret") or "").strip() or None)
                title = title or discovered
            except Exception as exc:
                if egress.unavailable_in(exc) is not None:
                    raise
                summary["youtube"]["skipped"].append(
                    {"file": ref, "reason": f"không đọc được kênh: {type(exc).__name__}: {exc}"[:300]})
                continue
        seen.add(ref)
        label = str(data.get("label") or "").strip() or title or _label_from(name)
        row = conn.execute("SELECT id, source FROM social_account WHERE platform='youtube' AND external_id=?",
                           (channel_id,)).fetchone()
        if row is not None and row["source"] != SOURCE:
            summary["youtube"]["skipped"].append(
                {"file": ref, "reason": f"kênh {channel_id} đã kết nối local; giữ bản local"})
            continue
        if row is None:
            has_any = conn.execute("SELECT 1 FROM social_account WHERE platform='youtube' LIMIT 1").fetchone()
            conn.execute(
                """INSERT INTO social_account (platform, label, external_id, display_name, access_token,
                       refresh_token, token_expiry, extra_json, is_default, source, remote_ref,
                       created_at, updated_at)
                   VALUES ('youtube', ?, ?, ?, '', '', '', '{}', ?, ?, ?, ?, ?)""",
                (label, channel_id, title, 0 if has_any else 1, SOURCE, ref, _now(), _now()))
            summary["youtube"]["added"] += 1
        else:
            conn.execute(
                """UPDATE social_account SET label=?, display_name=?, remote_ref=?, access_token='',
                       refresh_token='', status='connected', last_error=NULL, updated_at=? WHERE id=?""",
                (label, title, ref, _now(), row["id"]))
            summary["youtube"]["updated"] += 1
    if youtube_listed:
        for row in conn.execute("SELECT id, remote_ref FROM social_account WHERE platform='youtube' AND source=?",
                                (SOURCE,)).fetchall():
            if row["remote_ref"] not in seen:
                from app import social_accounts
                social_accounts.delete(conn, row["id"])
                summary["youtube"]["removed"] += 1
    conn.commit()
    return summary
