"""Unit tests for auto_build_patches in repository."""
import sqlite3

import pytest

from app import db as app_db
from app import repository


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(repository.settings, "data_root", tmp_path)
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    app_db.init_schema(c)
    yield c
    c.close()


def _seed(conn, chapter_count: int = 20, patch_size: int = 10):
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, created_at, updated_at) "
        "VALUES ('test', 't.epub', 't.epub', ?, 'ready', ?, ?)",
        (patch_size, now, now),
    )
    book_id = cur.lastrowid
    for i in range(chapter_count):
        conn.execute(
            "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (book_id, i, f"Ch{i}", f"text {i} " * 5, 50),
        )
    conn.commit()
    return book_id


def test_basic_chunking(conn):
    book_id = _seed(conn, 20, 10)
    patches = repository.auto_build_patches(conn, book_id, start_chapter=0)
    assert len(patches) == 2
    assert patches[0].chapter_start == 0
    assert patches[0].chapter_end == 9
    assert patches[1].chapter_start == 10
    assert patches[1].chapter_end == 19


def test_auto_build_cleans_existing_chunk_directories(conn):
    book_id = _seed(conn, 4, 2)
    patches = repository.auto_build_patches(conn, book_id, start_chapter=0)
    audio_path = repository.get_patch_audio_path(book_id, patches[0].patch_index)
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(b"existing merged audio")
    for patch in patches:
        chunk_dir = repository.get_patch_chunk_dir(book_id, patch.patch_index)
        chunk_dir.mkdir(parents=True)
        (chunk_dir / "chunk_000.wav").write_bytes(b"existing chunk audio")

    rebuilt = repository.auto_build_patches(conn, book_id, start_chapter=0)

    assert [(p.chapter_start, p.chapter_end) for p in rebuilt] == [(0, 1), (2, 3)]
    assert list(audio_path.parent.iterdir()) == []
    backups = list((audio_path.parent.parent / "backup_audio").glob("*.wav"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"existing merged audio"


def test_default_end_uses_max(conn):
    book_id = _seed(conn, 7, 5)
    patches = repository.auto_build_patches(conn, book_id, start_chapter=3)
    assert len(patches) == 1
    assert patches[0].chapter_start == 3
    assert patches[0].chapter_end == 6


def test_default_patch_size_from_book(conn):
    book_id = _seed(conn, 10, 3)
    patches = repository.auto_build_patches(conn, book_id, start_chapter=0)
    assert len(patches) == 4  # 10 chapters / 3 = 4 patches (3, 3, 3, 1)


def test_excluded_skipped(conn):
    book_id = _seed(conn, 10, 3)
    repository.set_chapter_excluded(conn, book_id, 3, True)
    repository.set_chapter_excluded(conn, book_id, 7, True)
    patches = repository.auto_build_patches(conn, book_id, start_chapter=0)
    assert len(patches) == 3  # 8 included chapters / 3 = 3 patches
    assert patches[0].chapter_start == 0
    assert patches[0].chapter_end == 2
    assert patches[1].chapter_start == 4
    assert patches[1].chapter_end == 6
    assert patches[2].chapter_start == 8
    assert patches[2].chapter_end == 9


def test_end_before_start_rejected(conn):
    book_id = _seed(conn, 10, 5)
    with pytest.raises(ValueError, match="end_chapter must be >= start_chapter"):
        repository.auto_build_patches(conn, book_id, start_chapter=5, end_chapter=2)


def test_start_out_of_bounds_rejected(conn):
    book_id = _seed(conn, 5, 5)
    with pytest.raises(ValueError, match="out of bounds"):
        repository.auto_build_patches(conn, book_id, start_chapter=99)


def test_all_excluded_rejected(conn):
    book_id = _seed(conn, 3, 5)
    for i in range(3):
        repository.set_chapter_excluded(conn, book_id, i, True)
    with pytest.raises(ValueError, match="no included chapters"):
        repository.auto_build_patches(conn, book_id, start_chapter=0)


def test_patch_size_less_than_1_rejected(conn):
    book_id = _seed(conn, 10, 5)
    with pytest.raises(ValueError, match="patch_size must be >= 1"):
        repository.auto_build_patches(conn, book_id, start_chapter=0, patch_size=0)


def test_last_chunk_smaller(conn):
    book_id = _seed(conn, 7, 5)
    patches = repository.auto_build_patches(conn, book_id, start_chapter=0)
    assert len(patches) == 2
    assert patches[0].chapter_end == 4
    assert patches[1].chapter_start == 5
    assert patches[1].chapter_end == 6


def test_explicit_end_and_patch_size(conn):
    book_id = _seed(conn, 20, 10)
    patches = repository.auto_build_patches(conn, book_id, start_chapter=2, end_chapter=11, patch_size=4)
    assert len(patches) == 3
    assert patches[0].chapter_start == 2
    assert patches[0].chapter_end == 5
    assert patches[1].chapter_start == 6
    assert patches[1].chapter_end == 9
    assert patches[2].chapter_start == 10
    assert patches[2].chapter_end == 11
