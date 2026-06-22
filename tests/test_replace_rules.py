"""Unit tests for replace rules: CRUD, validation, and apply."""
import sqlite3

import pytest

from app import db as app_db
from app import repository
from app.models import TextReplaceRule


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    app_db.init_schema(c)
    yield c
    c.close()


def _seed(conn) -> int:
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES ('test', 't.epub', 't.epub', 10, 'ready', ?, ?)",
        (now, now),
    )
    conn.commit()
    return cur.lastrowid


def test_create_literal_rule(conn):
    book_id = _seed(conn)
    rule = repository.create_replace_rule(conn, book_id, "AI", "A.I.", False, 0)
    assert rule.find == "AI"
    assert rule.replace == "A.I."
    assert rule.is_regex is False


def test_create_regex_rule(conn):
    book_id = _seed(conn)
    rule = repository.create_replace_rule(conn, book_id, r"\bAI\b", "A.I.", True, 1)
    assert rule.is_regex is True


def test_create_invalid_regex_raises(conn):
    book_id = _seed(conn)
    with pytest.raises(ValueError, match="invalid regex"):
        repository.create_replace_rule(conn, book_id, "[invalid", "", True, 0)


def test_create_empty_find_raises(conn):
    book_id = _seed(conn)
    with pytest.raises(ValueError, match="must not be empty"):
        repository.create_replace_rule(conn, book_id, "", "x", False, 0)


def test_update_rule(conn):
    book_id = _seed(conn)
    rule = repository.create_replace_rule(conn, book_id, "old", "new", False, 0)
    updated = repository.update_replace_rule(conn, rule.id, find="updated", replace="value")
    assert updated.find == "updated"
    assert updated.replace == "value"


def test_delete_rule(conn):
    book_id = _seed(conn)
    rule = repository.create_replace_rule(conn, book_id, "x", "y", False, 0)
    ok = repository.delete_replace_rule(conn, rule.id)
    assert ok is True
    assert repository.delete_replace_rule(conn, rule.id) is False


def test_list_ordered_by_position_then_id(conn):
    book_id = _seed(conn)
    r1 = repository.create_replace_rule(conn, book_id, "a", "A", False, 10)
    r2 = repository.create_replace_rule(conn, book_id, "b", "B", False, 0)
    r3 = repository.create_replace_rule(conn, book_id, "c", "C", False, 0)
    rules = repository.list_replace_rules(conn, book_id)
    ids = [r.id for r in rules]
    assert ids == [r2.id, r3.id, r1.id]  # position 0,0 then 10; ties by id


def test_apply_literal_rules_in_order(conn):
    book_id = _seed(conn)
    repository.create_replace_rule(conn, book_id, "AI", "A.I.", False, 0)
    repository.create_replace_rule(conn, book_id, "A.I.", "Artificial Intelligence", False, 1)
    rules = repository.list_replace_rules(conn, book_id)
    result = repository.apply_replace_rules("The AI uses AI.", rules)
    assert result == "The Artificial Intelligence uses Artificial Intelligence."


def test_apply_regex_rule(conn):
    book_id = _seed(conn)
    repository.create_replace_rule(conn, book_id, r"\bAI\b", "A.I.", True, 0)
    rules = repository.list_replace_rules(conn, book_id)
    result = repository.apply_replace_rules("The AI said AI is great but AID is not.", rules)
    assert result == "The A.I. said A.I. is great but AID is not."


def test_reset_done_patches(conn):
    book_id = _seed(conn)
    conn.execute(
        "INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status, audio_path, created_at, updated_at) "
        "VALUES (?, 0, 0, 0, 'done', 'x.wav', '2026-01-01', '2026-01-01')",
        (book_id,),
    )
    conn.commit()
    count = repository.reset_done_patches_for_book(conn, book_id)
    assert count == 1
    row = conn.execute("SELECT status, audio_path FROM patch WHERE book_id = ?", (book_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["audio_path"] is None
