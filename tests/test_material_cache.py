"""Unit tests for the content-addressed background material cache."""
from __future__ import annotations

import json

import pytest

from app import db, material_cache
from app.config import settings


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


def test_make_key_is_stable_and_distinguishes_inputs():
    key = material_cache.make_key("pollinations", "a red fox", 1024, 1024, model="flux")
    assert key == material_cache.make_key("pollinations", "a red fox", 1024, 1024, model="flux")
    assert key != material_cache.make_key("pollinations", "a blue fox", 1024, 1024, model="flux")
    assert key != material_cache.make_key("pollinations", "a red fox", 512, 512, model="flux")
    assert key != material_cache.make_key("pexels", "a red fox", 1024, 1024, model="flux")


def test_get_misses_on_an_unknown_key(conn):
    assert material_cache.get(conn, "does-not-exist") is None


def test_put_then_get_round_trips_the_file(conn):
    key = material_cache.make_key("pollinations", "a red fox", 512, 512)
    path = material_cache.put(conn, key, b"fake-png-bytes", source="pollinations", prompt="a red fox", width=512, height=512)
    assert path.is_file()
    assert path.read_bytes() == b"fake-png-bytes"

    hit = material_cache.get(conn, key)
    assert hit == path
    row = conn.execute("SELECT use_count FROM material_cache WHERE cache_key = ?", (key,)).fetchone()
    assert row["use_count"] == 1


def test_get_drops_the_row_when_the_file_was_deleted_out_from_under_it(conn):
    key = material_cache.make_key("pollinations", "a red fox", 512, 512)
    path = material_cache.put(conn, key, b"bytes", source="pollinations", prompt="a red fox", width=512, height=512)
    path.unlink()

    assert material_cache.get(conn, key) is None
    assert conn.execute("SELECT 1 FROM material_cache WHERE cache_key = ?", (key,)).fetchone() is None


def test_put_is_idempotent_for_the_same_key(conn):
    key = material_cache.make_key("pollinations", "a red fox", 512, 512)
    first = material_cache.put(conn, key, b"v1", source="pollinations", prompt="a red fox", width=512, height=512)
    second = material_cache.put(conn, key, b"v2", source="pollinations", prompt="a red fox", width=512, height=512)
    assert first == second
    assert second.read_bytes() == b"v2"
    count = conn.execute("SELECT COUNT(*) AS c FROM material_cache").fetchone()["c"]
    assert count == 1


def test_gc_removes_unreferenced_entries_past_max_age(conn):
    key = material_cache.make_key("pollinations", "old fox", 512, 512)
    path = material_cache.put(conn, key, b"bytes", source="pollinations", prompt="old fox", width=512, height=512)
    conn.execute(
        "UPDATE material_cache SET last_used_at = '2000-01-01T00:00:00+00:00' WHERE cache_key = ?", (key,)
    )
    conn.commit()

    removed = material_cache.gc(conn, max_age_days=30)
    assert removed == 1
    assert not path.exists()
    assert conn.execute("SELECT 1 FROM material_cache WHERE cache_key = ?", (key,)).fetchone() is None


def test_gc_keeps_entries_still_referenced_by_a_book(conn):
    key = material_cache.make_key("pollinations", "referenced fox", 512, 512)
    path = material_cache.put(conn, key, b"bytes", source="pollinations", prompt="referenced fox", width=512, height=512)
    conn.execute(
        "UPDATE material_cache SET last_used_at = '2000-01-01T00:00:00+00:00' WHERE cache_key = ?", (key,)
    )
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        """INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,automation_config,created_at,updated_at)
           VALUES (1,'Book','book.epub','book.epub',10,'ready',?,?,?)""",
        (json.dumps({"video": {"backgrounds": [str(path)]}}), now, now),
    )
    conn.commit()

    removed = material_cache.gc(conn, max_age_days=30)
    assert removed == 0
    assert path.exists()
    assert conn.execute("SELECT 1 FROM material_cache WHERE cache_key = ?", (key,)).fetchone() is not None


def test_gc_removes_oldest_unreferenced_entries_first_over_budget(conn):
    keys = []
    for i in range(3):
        key = material_cache.make_key("pollinations", f"fox {i}", 512, 512)
        material_cache.put(conn, key, b"x" * 100, source="pollinations", prompt=f"fox {i}", width=512, height=512)
        conn.execute(
            "UPDATE material_cache SET last_used_at = ? WHERE cache_key = ?",
            (f"2020-01-0{i + 1}T00:00:00+00:00", key),
        )
        keys.append(key)
    conn.commit()

    removed = material_cache.gc(conn, max_bytes=150)
    assert removed == 2
    remaining = {row["cache_key"] for row in conn.execute("SELECT cache_key FROM material_cache").fetchall()}
    assert remaining == {keys[2]}


def test_gc_is_a_no_op_when_nothing_is_stale_or_over_budget(conn):
    key = material_cache.make_key("pollinations", "fresh fox", 512, 512)
    path = material_cache.put(conn, key, b"bytes", source="pollinations", prompt="fresh fox", width=512, height=512)

    removed = material_cache.gc(conn, max_age_days=30, max_bytes=10_000_000)
    assert removed == 0
    assert path.exists()
