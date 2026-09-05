"""Kaggle account pool: CRUD, atomic claim/release, and a local best-effort GPU-time
ledger used to estimate each account's remaining weekly quota.

Owns `kaggle_account`/`kaggle_usage` directly (own SQL), the same way
`app/google_drive.py` owns `drive_account`/`drive_oauth_client` instead of going
through `app/repository.py`."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------


def create_account(conn: sqlite3.Connection, label: str, username: str, api_key: str) -> int:
    now = _now_iso()
    cur = conn.execute(
        """INSERT INTO kaggle_account (label, username, api_key, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
        (label, username, api_key, now, now),
    )
    conn.commit()
    return cur.lastrowid


def list_accounts(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM kaggle_account ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_account(conn: sqlite3.Connection, account_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM kaggle_account WHERE id=?", (account_id,)).fetchone()
    return dict(row) if row else None


def update_account(
    conn: sqlite3.Connection, account_id: int, *, label: str, username: str, api_key: str = "",
) -> None:
    """Blank api_key keeps the existing one -- same convention as
    google_drive.update_client's client_secret."""
    if api_key:
        conn.execute(
            "UPDATE kaggle_account SET label=?, username=?, api_key=?, updated_at=? WHERE id=?",
            (label, username, api_key, _now_iso(), account_id),
        )
    else:
        conn.execute(
            "UPDATE kaggle_account SET label=?, username=?, updated_at=? WHERE id=?",
            (label, username, _now_iso(), account_id),
        )
    conn.commit()


def set_disabled(conn: sqlite3.Connection, account_id: int, disabled: bool) -> None:
    """Manual state set from the settings page only -- account selection always skips
    'disabled'. Re-enabling clears any stale cooldown so the account is immediately
    claimable again rather than waiting out a cooldown set before it was disabled."""
    if disabled:
        conn.execute(
            "UPDATE kaggle_account SET status='disabled', updated_at=? WHERE id=?",
            (_now_iso(), account_id),
        )
    else:
        conn.execute(
            "UPDATE kaggle_account SET status='idle', cooldown_until=NULL, updated_at=? WHERE id=?",
            (_now_iso(), account_id),
        )
    conn.commit()


def delete_account(conn: sqlite3.Connection, account_id: int) -> bool:
    """False (no exception) while the account is in_use_by_job_id -- callers map this to
    an HTTP 400, matching the existing refusal pattern for a Drive OAuth client still
    referenced by an account."""
    row = conn.execute(
        "SELECT in_use_by_job_id FROM kaggle_account WHERE id=?", (account_id,)
    ).fetchone()
    if row is None:
        return False
    if row["in_use_by_job_id"] is not None:
        return False
    conn.execute("DELETE FROM kaggle_account WHERE id=?", (account_id,))
    conn.commit()
    return True


# ---------------------------------------------------------------------------
# Claim / release -- one atomic UPDATE, same pattern as jobqueue/store.py::claim
# ---------------------------------------------------------------------------


def claim_idle_account(conn: sqlite3.Connection, job_id: int) -> dict | None:
    """Claim one account that is 'idle', or 'cooldown' whose cooldown_until has already
    passed (self-heals a stale cooldown), preferring whichever was touched longest ago.
    Never returns a 'disabled' account."""
    now = _now_iso()
    row = conn.execute(
        """UPDATE kaggle_account
              SET status='busy', in_use_by_job_id=?, updated_at=?
            WHERE id = (
                SELECT id FROM kaggle_account
                 WHERE status='idle' OR (status='cooldown' AND cooldown_until<=?)
                 ORDER BY updated_at ASC LIMIT 1)
              AND (status='idle' OR (status='cooldown' AND cooldown_until<=?))
        RETURNING *""",
        (job_id, now, now, now),
    ).fetchone()
    conn.commit()
    return dict(row) if row else None


def release_account(
    conn: sqlite3.Connection, account_id: int, *, cooldown_until: str | None = None,
) -> None:
    status = "cooldown" if cooldown_until else "idle"
    conn.execute(
        """UPDATE kaggle_account
              SET status=?, cooldown_until=?, in_use_by_job_id=NULL, updated_at=?
            WHERE id=?""",
        (status, cooldown_until, _now_iso(), account_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Usage ledger / quota estimate
# ---------------------------------------------------------------------------


def record_usage_start(conn: sqlite3.Connection, account_id: int, kernel_ref: str) -> int:
    now = _now_iso()
    cur = conn.execute(
        """INSERT INTO kaggle_usage (account_id, kernel_ref, started_at, created_at)
           VALUES (?, ?, ?, ?)""",
        (account_id, kernel_ref, now, now),
    )
    conn.commit()
    return cur.lastrowid


def record_usage_finish(conn: sqlite3.Connection, usage_id: int, gpu_seconds: int) -> None:
    conn.execute(
        "UPDATE kaggle_usage SET finished_at=?, gpu_seconds=? WHERE id=?",
        (_now_iso(), gpu_seconds, usage_id),
    )
    conn.commit()


def remaining_quota_seconds(conn: sqlite3.Connection, account_id: int) -> int:
    """weekly_quota_seconds - SUM(gpu_seconds) over the last 7 days, clamped to 0.
    Reads settings.kaggle_weekly_gpu_quota_hours at call time (not import time) so
    changing it takes effect immediately."""
    from app.config import settings

    weekly_quota_seconds = settings.kaggle_weekly_gpu_quota_hours * 3600
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    row = conn.execute(
        """SELECT COALESCE(SUM(gpu_seconds), 0) AS used FROM kaggle_usage
            WHERE account_id=? AND started_at >= ?""",
        (account_id, cutoff),
    ).fetchone()
    return max(0, weekly_quota_seconds - row["used"])


def earliest_quota_reset(conn: sqlite3.Connection) -> str | None:
    """The earliest moment any account's quota window frees up: 7 days after the oldest
    usage row still counted against quota, across every account. None if no usage in
    the last 7 days counts against anyone."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    row = conn.execute(
        "SELECT MIN(started_at) AS oldest FROM kaggle_usage WHERE started_at >= ?",
        (cutoff,),
    ).fetchone()
    if row is None or row["oldest"] is None:
        return None
    oldest = datetime.fromisoformat(row["oldest"])
    return (oldest + timedelta(days=7)).isoformat()
