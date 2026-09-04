"""Schema của bảng job: cột, index, và partial unique index trên dedupe_key."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app import db


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def _insert(conn, **over):
    now = datetime.now(timezone.utc).isoformat()
    cols = {
        "job_type": "video", "status": "pending", "payload_json": "{}",
        "dedupe_key": None, "created_at": now, "updated_at": now,
    }
    cols.update(over)
    names = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    cur = conn.execute(f"INSERT INTO job ({names}) VALUES ({marks})", list(cols.values()))
    conn.commit()
    return cur.lastrowid


def test_job_table_has_expected_columns():
    conn = _conn()
    names = {r["name"] for r in conn.execute("PRAGMA table_info(job)")}
    assert names == {
        "id", "job_type", "status", "priority", "book_id", "payload_json", "dedupe_key",
        "phase", "progress_current", "progress_total", "result_json", "error_message",
        "attempt_count", "max_attempts", "next_retry_at", "worker_id", "heartbeat_at",
        "created_at", "started_at", "finished_at", "updated_at",
        "patch_id",
    }


def test_defaults_are_applied():
    conn = _conn()
    job_id = _insert(conn)
    row = conn.execute("SELECT * FROM job WHERE id=?", (job_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["priority"] == 100
    assert row["progress_current"] == 0
    assert row["progress_total"] == 0
    assert row["attempt_count"] == 0
    assert row["max_attempts"] == 3


def test_dedupe_key_blocks_a_second_live_job():
    conn = _conn()
    _insert(conn, dedupe_key="video:book_job=1")
    with pytest.raises(sqlite3.IntegrityError):
        _insert(conn, dedupe_key="video:book_job=1")


def test_dedupe_key_is_free_again_once_the_first_job_is_terminal():
    """Partial index chỉ phủ pending/running — job xong rồi thì khóa được tái sử dụng."""
    conn = _conn()
    first = _insert(conn, dedupe_key="video:book_job=1")
    conn.execute("UPDATE job SET status='done' WHERE id=?", (first,))
    conn.commit()
    second = _insert(conn, dedupe_key="video:book_job=1")
    assert second != first


def test_null_dedupe_keys_do_not_collide():
    conn = _conn()
    assert _insert(conn, dedupe_key=None) != _insert(conn, dedupe_key=None)


def test_claim_index_exists():
    conn = _conn()
    names = {r["name"] for r in conn.execute("PRAGMA index_list(job)")}
    assert {"idx_job_claim", "idx_job_book", "idx_job_dedupe"} <= names
