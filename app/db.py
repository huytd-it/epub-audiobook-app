"""SQLite connection helper and schema initialization."""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS book (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    title           TEXT NOT NULL,
    original_filename TEXT NOT NULL,
    epub_path       TEXT NOT NULL,
    patch_size      INTEGER NOT NULL DEFAULT 10,
    status          TEXT NOT NULL DEFAULT 'parsing',
    final_audio_path TEXT,
    final_video_path TEXT,
    background_image_path TEXT,
    voice_clip_path TEXT,
    voice_transcript TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chapter (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id         INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    chapter_index   INTEGER NOT NULL,
    title           TEXT,
    text            TEXT NOT NULL,
    char_count      INTEGER NOT NULL,
    UNIQUE(book_id, chapter_index)
);

CREATE TABLE IF NOT EXISTS patch (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id         INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    patch_index     INTEGER NOT NULL,
    chapter_start   INTEGER NOT NULL,
    chapter_end     INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    audio_path      TEXT,
    error_message   TEXT,
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(book_id, patch_index)
);

CREATE INDEX IF NOT EXISTS idx_patch_status ON patch(status);
CREATE INDEX IF NOT EXISTS idx_patch_book_order ON patch(book_id, patch_index);
"""


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a book table already existed on disk."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(book)")}
    if "voice_clip_path" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN voice_clip_path TEXT")
    if "voice_transcript" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN voice_transcript TEXT")
