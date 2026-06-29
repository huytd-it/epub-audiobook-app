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
    name            TEXT,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS idx_patch_status_updated ON patch(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS book_job (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id         INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    job_type        TEXT NOT NULL DEFAULT 'video',
    status          TEXT NOT NULL DEFAULT 'pending',
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    error_message   TEXT,
    output_path     TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    UNIQUE(book_id, job_type)
);

CREATE INDEX IF NOT EXISTS idx_book_job_status ON book_job(status, book_id, id);
CREATE INDEX IF NOT EXISTS idx_book_job_book_type ON book_job(book_id, job_type);

CREATE TABLE IF NOT EXISTS app_state (
    key             TEXT PRIMARY KEY,
    value           TEXT
);

CREATE TABLE IF NOT EXISTS text_replace_rule (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id         INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    find            TEXT NOT NULL,
    replace         TEXT NOT NULL DEFAULT '',
    is_regex        INTEGER NOT NULL DEFAULT 0,
    position        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS youtube_credentials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    access_token    TEXT NOT NULL,
    refresh_token   TEXT NOT NULL,
    token_expiry    TEXT NOT NULL,
    channel_id      TEXT,
    channel_name    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youtube_uploads (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    video_path      TEXT NOT NULL,
    youtube_video_id TEXT,
    title           TEXT,
    description     TEXT,
    tags            TEXT,
    privacy_status  TEXT NOT NULL DEFAULT 'private',
    status          TEXT NOT NULL DEFAULT 'pending',
    error_message   TEXT,
    uploaded_at     TEXT,
    created_at      TEXT NOT NULL
);
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
    # book_job and app_state are CREATE TABLE IF NOT EXISTS, so they're picked up by
    # init_schema on a fresh DB and are a no-op on an existing DB; no per-column migration
    # is needed for them.
    chapter_existing = {row["name"] for row in conn.execute("PRAGMA table_info(chapter)")}
    if "is_excluded" not in chapter_existing:
        conn.execute("ALTER TABLE chapter ADD COLUMN is_excluded INTEGER NOT NULL DEFAULT 0")
    patch_existing = {row["name"] for row in conn.execute("PRAGMA table_info(patch)")}
    if "image_path" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN image_path TEXT")
    if "image_type" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN image_type TEXT NOT NULL DEFAULT 'static'")
    if "name" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN name TEXT")
    if "chunk_count" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0")
    if "video_resolution" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN video_resolution TEXT NOT NULL DEFAULT '1920x1080'")
    if "video_fps" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN video_fps INTEGER NOT NULL DEFAULT 30")
    if "default_image_animation" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN default_image_animation TEXT NOT NULL DEFAULT 'none'")
