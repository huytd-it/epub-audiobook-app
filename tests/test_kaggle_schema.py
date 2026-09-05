"""Schema kaggle_account/kaggle_usage + 2 cột mới trên patch_export."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from app import db


def _conn():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    return conn


def test_kaggle_account_table_has_expected_columns():
    conn = _conn()
    names = {r["name"] for r in conn.execute("PRAGMA table_info(kaggle_account)")}
    assert names == {
        "id", "label", "username", "api_key", "status", "cooldown_until",
        "in_use_by_job_id", "created_at", "updated_at",
    }


def test_kaggle_account_defaults_to_idle():
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO kaggle_account (label, username, api_key, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        ("acc1", "user1", "key1", now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM kaggle_account WHERE id=?", (cur.lastrowid,)).fetchone()
    assert row["status"] == "idle"
    assert row["in_use_by_job_id"] is None


def test_kaggle_account_username_is_unique():
    conn = _conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO kaggle_account (label, username, api_key, created_at, updated_at) "
        "VALUES ('a', 'dup', 'k1', ?, ?)", (now, now),
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO kaggle_account (label, username, api_key, created_at, updated_at) "
            "VALUES ('b', 'dup', 'k2', ?, ?)", (now, now),
        )


def test_kaggle_usage_table_has_expected_columns():
    conn = _conn()
    names = {r["name"] for r in conn.execute("PRAGMA table_info(kaggle_usage)")}
    assert names == {
        "id", "account_id", "kernel_ref", "started_at", "finished_at",
        "gpu_seconds", "created_at",
    }


def test_patch_export_has_kaggle_columns():
    conn = _conn()
    names = {r["name"] for r in conn.execute("PRAGMA table_info(patch_export)")}
    assert {"kaggle_account_id", "kaggle_kernel_ref"} <= names
