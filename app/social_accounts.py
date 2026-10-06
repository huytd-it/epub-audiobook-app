"""Tài khoản mạng xã hội dùng chung cho Socials hub (YouTube / Facebook Page / TikTok).

Mỗi dòng social_account là một tài khoản đã kết nối của một mạng; mỗi mạng có đúng
một tài khoản mặc định, là tài khoản được dùng khi nơi gọi không chỉ định gì — nhờ
vậy mọi luồng tự động có từ trước (auto-upload, playlist theo sách...) chạy tiếp mà
không cần biết tới multi-account.

Ba bảng cũ (youtube_credentials / facebook_credentials / tiktok_credentials) chỉ còn
được đọc làm phương án dự phòng khi mạng đó chưa có dòng nào ở đây: một bản sao lưu
cũ nạp lại qua /database-io chỉ mang theo bảng cũ vẫn phải dùng được ngay.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

PLATFORMS = ("youtube", "facebook", "tiktok")

# Bảng cũ của từng mạng và cách đọc nó thành các cột của social_account.
_LEGACY = {
    "youtube": ("youtube_credentials", "channel_id", "channel_name", "access_token"),
    "facebook": ("facebook_credentials", "page_id", "page_name", "page_access_token"),
    "tiktok": ("tiktok_credentials", "open_id", "display_name", "access_token"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _require_platform(platform: str) -> str:
    if platform not in PLATFORMS:
        raise ValueError(f"platform phải là một trong {', '.join(PLATFORMS)}")
    return platform


def _legacy_account(conn: sqlite3.Connection, platform: str) -> dict | None:
    table, id_col, name_col, token_col = _LEGACY[platform]
    row = conn.execute(f"SELECT * FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    row = dict(row)
    return {
        "id": None,
        "platform": platform,
        "label": row.get(name_col) or "",
        "external_id": row.get(id_col) or "",
        "display_name": row.get(name_col),
        "access_token": row.get(token_col) or "",
        "refresh_token": row.get("refresh_token") or "",
        "token_expiry": row.get("token_expiry") or "",
        "extra_json": "{}",
        "egress_endpoint_id": None,
        "is_default": 1,
        "status": "connected",
        "last_error": None,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def list_accounts(conn: sqlite3.Connection, platform: str | None = None) -> list[dict]:
    if platform is None:
        rows = conn.execute(
            "SELECT * FROM social_account ORDER BY platform, is_default DESC, id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM social_account WHERE platform=? ORDER BY is_default DESC, id",
            (_require_platform(platform),),
        ).fetchall()
    return [dict(row) for row in rows]


def get_account(conn: sqlite3.Connection, account_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM social_account WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row is not None else None


def get_default(conn: sqlite3.Connection, platform: str) -> dict | None:
    """Tài khoản mặc định của một mạng; bảng cũ chỉ được hỏi tới khi chưa có dòng nào."""
    row = conn.execute(
        "SELECT * FROM social_account WHERE platform=? ORDER BY is_default DESC, id LIMIT 1",
        (_require_platform(platform),),
    ).fetchone()
    if row is not None:
        return dict(row)
    return _legacy_account(conn, platform)


def resolve(conn: sqlite3.Connection, platform: str, account_id: int | None = None) -> dict | None:
    """Tài khoản được chỉ định, hoặc mặc định của mạng khi account_id là None.

    Một account_id của mạng khác (hoặc đã bị xoá) trả về None chứ không rơi về mặc
    định: đăng nhầm sang kênh khác là lỗi tệ hơn nhiều so với báo "chưa kết nối".
    """
    if account_id is None:
        return get_default(conn, platform)
    account = get_account(conn, int(account_id))
    if account is None or account["platform"] != platform:
        return None
    return account


def upsert(
    conn: sqlite3.Connection,
    platform: str,
    *,
    external_id: str | None,
    display_name: str | None = None,
    access_token: str,
    refresh_token: str = "",
    token_expiry: str = "",
    label: str | None = None,
    extra: dict | None = None,
) -> int:
    """Thêm tài khoản, hoặc cập nhật token khi kết nối lại đúng tài khoản đó.

    Khoá theo (platform, external_id). Tài khoản đầu tiên của một mạng tự thành mặc
    định; refresh_token rỗng không ghi đè cái đang lưu (Google chỉ trả refresh_token
    ở lần cấp quyền đầu).
    """
    _require_platform(platform)
    external_id = (external_id or "").strip()
    now = _now()
    existing = conn.execute(
        "SELECT id, refresh_token, label FROM social_account WHERE platform=? AND external_id=?",
        (platform, external_id),
    ).fetchone()
    if existing is not None:
        conn.execute(
            """UPDATE social_account
               SET display_name=?, access_token=?, refresh_token=?, token_expiry=?,
                   label=?, status='connected', last_error=NULL, updated_at=?
               WHERE id=?""",
            (display_name, access_token, refresh_token or existing["refresh_token"],
             token_expiry or "", label if label is not None else existing["label"],
             now, existing["id"]),
        )
        if extra is not None:
            conn.execute("UPDATE social_account SET extra_json=? WHERE id=?",
                         (json.dumps(extra, ensure_ascii=False), existing["id"]))
        conn.commit()
        return int(existing["id"])
    has_any = conn.execute(
        "SELECT 1 FROM social_account WHERE platform=? LIMIT 1", (platform,)
    ).fetchone() is not None
    cur = conn.execute(
        """INSERT INTO social_account
           (platform, label, external_id, display_name, access_token, refresh_token,
            token_expiry, extra_json, is_default, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (platform, label if label is not None else (display_name or ""), external_id,
         display_name, access_token, refresh_token or "", token_expiry or "",
         json.dumps(extra or {}, ensure_ascii=False), 0 if has_any else 1, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_tokens(conn: sqlite3.Connection, account_id: int, *, access_token: str,
                  refresh_token: str = "", token_expiry: str = "") -> None:
    row = get_account(conn, account_id)
    if row is None:
        return
    conn.execute(
        """UPDATE social_account SET access_token=?, refresh_token=?, token_expiry=?,
           updated_at=? WHERE id=?""",
        (access_token, refresh_token or row["refresh_token"],
         token_expiry or row["token_expiry"], _now(), account_id),
    )
    conn.commit()


def set_default(conn: sqlite3.Connection, account_id: int) -> bool:
    row = get_account(conn, account_id)
    if row is None:
        return False
    conn.execute("UPDATE social_account SET is_default=0 WHERE platform=?", (row["platform"],))
    conn.execute("UPDATE social_account SET is_default=1, updated_at=? WHERE id=?",
                 (_now(), account_id))
    conn.commit()
    return True


_UNSET = object()


def update(conn: sqlite3.Connection, account_id: int, *, label: str | None = None,
           egress_endpoint_id=_UNSET) -> dict | None:
    """Đổi nhãn và/hoặc proxy ghim. egress_endpoint_id=None nghĩa là bỏ ghim."""
    if get_account(conn, account_id) is None:
        return None
    if label is not None:
        conn.execute("UPDATE social_account SET label=?, updated_at=? WHERE id=?",
                     (label.strip(), _now(), account_id))
    if egress_endpoint_id is not _UNSET:
        conn.execute("UPDATE social_account SET egress_endpoint_id=?, updated_at=? WHERE id=?",
                     (egress_endpoint_id, _now(), account_id))
    conn.commit()
    return get_account(conn, account_id)


def mark_error(conn: sqlite3.Connection, account_id: int | None, message: str) -> None:
    if account_id is None:
        return
    conn.execute(
        "UPDATE social_account SET status='error', last_error=?, updated_at=? WHERE id=?",
        ((message or "")[:500], _now(), account_id),
    )
    conn.commit()


def delete(conn: sqlite3.Connection, account_id: int) -> bool:
    """Ngắt một tài khoản; nếu nó đang là mặc định thì tài khoản cũ nhất còn lại lên thay."""
    row = get_account(conn, account_id)
    if row is None:
        return False
    platform = row["platform"]
    conn.execute("DELETE FROM social_account WHERE id=?", (account_id,))
    remaining = conn.execute(
        "SELECT id, is_default FROM social_account WHERE platform=? ORDER BY is_default DESC, id",
        (platform,),
    ).fetchall()
    if not remaining:
        # Không dọn bảng cũ thì get_default() sẽ nhặt lại đúng tài khoản vừa ngắt.
        conn.execute(f"DELETE FROM {_LEGACY[platform][0]}")
    elif not remaining[0]["is_default"]:
        conn.execute("UPDATE social_account SET is_default=1 WHERE id=?", (remaining[0]["id"],))
    conn.commit()
    return True


def delete_all(conn: sqlite3.Connection, platform: str) -> None:
    _require_platform(platform)
    conn.execute("DELETE FROM social_account WHERE platform=?", (platform,))
    conn.execute(f"DELETE FROM {_LEGACY[platform][0]}")
    conn.commit()


def public(account: dict) -> dict:
    """Bản an toàn để trả ra API: không bao giờ kèm token."""
    return {
        "id": account["id"],
        "platform": account["platform"],
        "label": account["label"] or account["display_name"] or account["external_id"] or "",
        "external_id": account["external_id"],
        "display_name": account["display_name"],
        "egress_endpoint_id": account["egress_endpoint_id"],
        "is_default": bool(account["is_default"]),
        "status": account["status"],
        "last_error": account["last_error"],
        "has_refresh_token": bool(account["refresh_token"]),
        "token_expiry": account["token_expiry"],
        "created_at": account["created_at"],
        "updated_at": account["updated_at"],
    }
