"""SQLite connection helper and schema initialization."""
from __future__ import annotations

import json
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
    normalize_numbers_enabled INTEGER NOT NULL DEFAULT 1,
    normalize_junk_enabled INTEGER NOT NULL DEFAULT 1,
    normalize_spellcheck_enabled INTEGER NOT NULL DEFAULT 1,
    normalize_dictionary_enabled INTEGER NOT NULL DEFAULT 0,
    normalize_transliteration_enabled INTEGER NOT NULL DEFAULT 0,
    normalize_abbreviations_enabled INTEGER NOT NULL DEFAULT 1,
    normalize_breaks_enabled INTEGER NOT NULL DEFAULT 1,
    auto_create_video INTEGER NOT NULL DEFAULT 1,
    auto_upload_youtube INTEGER NOT NULL DEFAULT 1,
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
    chapter_no      INTEGER,
    text_hash       TEXT,
    UNIQUE(book_id, chapter_index)
);

CREATE TABLE IF NOT EXISTS patch (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id         INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    patch_index     INTEGER NOT NULL,
    chapter_start   INTEGER NOT NULL,
    chapter_end     INTEGER NOT NULL,
    -- Số chương đọc từ tiêu đề (không phải chỉ số vị trí). Đây mới là danh tính ổn
    -- định của patch: khi re-import EPUB làm chỉ số chương xê dịch, cặp số này cho
    -- phép căn lại chapter_start/chapter_end về đúng khoảng chương ban đầu.
    chapter_no_start INTEGER,
    chapter_no_end   INTEGER,
    name            TEXT,
    chunk_count     INTEGER NOT NULL DEFAULT 0,
    chunk_count_exact INTEGER NOT NULL DEFAULT 0,
    next_chunk_index INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    audio_path      TEXT,
    error_message   TEXT,
    youtube_override TEXT,
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

CREATE TABLE IF NOT EXISTS job (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 100,
    book_id INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    dedupe_key TEXT,
    phase TEXT,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    next_retry_at TEXT,
    worker_id TEXT,
    heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_claim ON job(status, job_type, priority, id);
CREATE INDEX IF NOT EXISTS idx_job_book ON job(book_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_dedupe ON job(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('pending','running');

CREATE TABLE IF NOT EXISTS flow_definition (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS flow_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flow_definition_id INTEGER NOT NULL REFERENCES flow_definition(id) ON DELETE RESTRICT,
    book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'running',
    definition_snapshot TEXT NOT NULL,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS job_dependency (
    job_id INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    depends_on_job_id INTEGER NOT NULL REFERENCES job(id) ON DELETE CASCADE,
    PRIMARY KEY (job_id, depends_on_job_id)
);
CREATE INDEX IF NOT EXISTS idx_job_dependency_upstream ON job_dependency(depends_on_job_id);

CREATE TABLE IF NOT EXISTS drive_oauth_client (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    client_id       TEXT NOT NULL,
    client_secret   TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drive_sync_target (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    account_email   TEXT NOT NULL,
    folder_path     TEXT NOT NULL,
    rclone_remote   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

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

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    filename TEXT NOT NULL,
    original_name TEXT,
    title TEXT DEFAULT '',
    description TEXT DEFAULT '',
    tags TEXT DEFAULT '',
    privacy TEXT DEFAULT 'private',
    file_path TEXT NOT NULL,
    file_size_bytes INTEGER DEFAULT 0,
    duration_sec REAL DEFAULT 0,
    resolution TEXT DEFAULT '1920x1080',
    batch_id TEXT,
    source_audio TEXT,
    background_path TEXT,
    render_config_json TEXT,
    upload_status TEXT DEFAULT 'local_only',
    youtube_video_id TEXT,
    youtube_upload_id INTEGER,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_videos_upload_status ON videos(upload_status);
CREATE INDEX IF NOT EXISTS idx_videos_batch_id ON videos(batch_id);
CREATE INDEX IF NOT EXISTS idx_videos_created_at ON videos(created_at);

CREATE TABLE IF NOT EXISTS batches (
    id TEXT PRIMARY KEY,
    name TEXT DEFAULT '',
    total_files INTEGER DEFAULT 0,
    completed_files INTEGER DEFAULT 0,
    failed_files INTEGER DEFAULT 0,
    status TEXT DEFAULT 'pending',
    config_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
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
    validation_status TEXT NOT NULL DEFAULT 'pending',
    validation_error_code TEXT,
    validation_error_message TEXT,
    validated_at TEXT,
    validation_report_json TEXT,
    integrity_retry_count INTEGER NOT NULL DEFAULT 0,
    render_source_type TEXT NOT NULL DEFAULT 'external',
    render_source_id INTEGER,
    error_message   TEXT,
    uploaded_at     TEXT,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS google_drive_credentials (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    access_token    TEXT NOT NULL,
    refresh_token   TEXT NOT NULL,
    token_expiry    TEXT NOT NULL,
    account_email   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS music (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    duration_sec    REAL,
    description     TEXT NOT NULL DEFAULT '',
    license         TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patch_warning (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    patch_id        INTEGER NOT NULL REFERENCES patch(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    position        INTEGER NOT NULL,
    length          INTEGER NOT NULL,
    original        TEXT NOT NULL,
    suggestion      TEXT NOT NULL DEFAULT '',
    accepted        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_patch_warning_patch ON patch_warning(patch_id, kind);

CREATE TABLE IF NOT EXISTS sound_effect (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    marker          TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS patch_export (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    patch_id                INTEGER NOT NULL REFERENCES patch(id) ON DELETE CASCADE,
    -- google_drive_credentials.id of the account the export went to; NULL = legacy
    -- export from before multi-account. Deliberately not a FK: disconnecting an
    -- account must neither be blocked by export history nor erase it.
    drive_account_id        INTEGER,
    sync_target_id          INTEGER,
    local_folder_path       TEXT,
    drive_folder_id         TEXT NOT NULL,
    drive_folder_link       TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'exported',
    exported_chunk_count    INTEGER NOT NULL DEFAULT 0,
    imported_chunk_count    INTEGER NOT NULL DEFAULT 0,
    error_message           TEXT,
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_patch_export_patch ON patch_export(patch_id, id DESC);

CREATE TABLE IF NOT EXISTS voice_meta (
    filename    TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    -- Classification slugs from app/voice_taxonomy.py: one gender, and genre as
    -- a comma-separated list (a voice usually suits several story genres).
    gender      TEXT NOT NULL DEFAULT '',
    genre       TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    schema_version INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS book_media_selection (
    book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    media_asset_id INTEGER NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    PRIMARY KEY (book_id, role, media_asset_id),
    UNIQUE (book_id, role, position)
);

CREATE TABLE IF NOT EXISTS patch_pipeline (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    patch_id INTEGER NOT NULL UNIQUE REFERENCES patch(id) ON DELETE CASCADE,
    stage TEXT NOT NULL DEFAULT 'thumbnail',
    thumbnail_status TEXT NOT NULL DEFAULT 'pending',
    video_status TEXT NOT NULL DEFAULT 'pending',
    upload_status TEXT NOT NULL DEFAULT 'pending',
    playlist_status TEXT NOT NULL DEFAULT 'pending',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    next_retry_at TEXT,
    thumbnail_path TEXT,
    video_path TEXT,
    video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL,
    youtube_upload_id INTEGER REFERENCES youtube_uploads(id) ON DELETE SET NULL,
    config_snapshot TEXT NOT NULL,
    media_snapshot TEXT NOT NULL,
    schema_version INTEGER NOT NULL DEFAULT 1,
    preflight_status TEXT,
    preflight_error_code TEXT,
    preflight_error TEXT,
    checked_at TEXT,
    policy_snapshot TEXT,
    snapshot_schema_version INTEGER NOT NULL DEFAULT 1,
    render_attempts INTEGER NOT NULL DEFAULT 0,
    validation_report_json TEXT,
    republish_confirmed_for TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_patch_pipeline_claim
ON patch_pipeline(stage, next_retry_at, id);

CREATE TABLE IF NOT EXISTS youtube_playlist_map (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    channel_id TEXT NOT NULL,
    playlist_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (book_id, channel_id)
);

-- Trạng thái podcast đã đẩy lên YouTube cho playlist của một sách: giữ riêng
-- khỏi youtube_playlist_map để không đụng vào cơ chế giành quyền tạo playlist.
CREATE TABLE IF NOT EXISTS youtube_podcast_state (
    book_id INTEGER NOT NULL REFERENCES book(id) ON DELETE CASCADE,
    playlist_id TEXT NOT NULL,
    podcast_status TEXT NOT NULL DEFAULT '',
    cover_sha TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (book_id, playlist_id)
);

CREATE TABLE IF NOT EXISTS job (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    priority         INTEGER NOT NULL DEFAULT 100,
    book_id          INTEGER,
    payload_json     TEXT NOT NULL DEFAULT '{}',
    dedupe_key       TEXT,
    phase            TEXT,
    progress_current INTEGER NOT NULL DEFAULT 0,
    progress_total   INTEGER NOT NULL DEFAULT 0,
    result_json      TEXT,
    error_message    TEXT,
    attempt_count    INTEGER NOT NULL DEFAULT 0,
    max_attempts     INTEGER NOT NULL DEFAULT 3,
    next_retry_at    TEXT,
    worker_id        TEXT,
    heartbeat_at     TEXT,
    created_at       TEXT NOT NULL,
    started_at       TEXT,
    finished_at      TEXT,
    updated_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_claim ON job(status, job_type, priority, id);
CREATE INDEX IF NOT EXISTS idx_job_book  ON job(book_id, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_job_dedupe ON job(dedupe_key)
    WHERE dedupe_key IS NOT NULL AND status IN ('pending','running');

CREATE TABLE IF NOT EXISTS material_cache (
    cache_key    TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    file_size    INTEGER,
    width        INTEGER,
    height       INTEGER,
    created_at   TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    use_count    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_material_cache_last_used ON material_cache(last_used_at);

CREATE TABLE IF NOT EXISTS gameplay_theme (
    id TEXT NOT NULL,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 0,
    manifest_json TEXT NOT NULL DEFAULT '{}',
    asset_dir TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (id, version)
);

CREATE TABLE IF NOT EXISTS gameplay_game (
    game_id TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    family TEXT NOT NULL,
    display_order INTEGER NOT NULL DEFAULT 0,
    config_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gameplay_fighter (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fighter_key TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    class_name TEXT NOT NULL,
    matches INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    eliminations INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gameplay_replay (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    replay_key TEXT NOT NULL UNIQUE,
    seed INTEGER NOT NULL,
    duration_seconds REAL NOT NULL,
    roster_json TEXT NOT NULL DEFAULT '[]',
    themes_json TEXT NOT NULL DEFAULT '[]',
    map_json TEXT NOT NULL DEFAULT '{}',
    events_json TEXT NOT NULL DEFAULT '[]',
    top3_json TEXT NOT NULL DEFAULT '[]',
    winner_key TEXT NOT NULL DEFAULT '',
    game_id TEXT,
    schema_version INTEGER,
    simulation_version TEXT,
    ruleset_version TEXT,
    renderer_version TEXT,
    payload_json TEXT,
    result_json TEXT,
    content_sha256 TEXT,
    stats_applied INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gameplay_replay_seed ON gameplay_replay(seed);

CREATE TABLE IF NOT EXISTS gameplay_clip (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_key TEXT NOT NULL,
    replay_id INTEGER NOT NULL REFERENCES gameplay_replay(id) ON DELETE RESTRICT,
    duration_seconds REAL NOT NULL,
    file_path TEXT,
    status TEXT NOT NULL DEFAULT 'available',
    reserved_patch_id INTEGER REFERENCES patch(id) ON DELETE SET NULL,
    reservation_token TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS gameplay_score (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    replay_id INTEGER NOT NULL UNIQUE REFERENCES gameplay_replay(id) ON DELETE CASCADE,
    game_id TEXT NOT NULL,
    seed INTEGER NOT NULL,
    player_tag TEXT NOT NULL DEFAULT '',
    score INTEGER NOT NULL DEFAULT 0,
    total_score INTEGER NOT NULL DEFAULT 0,
    level INTEGER NOT NULL DEFAULT 1,
    games INTEGER NOT NULL DEFAULT 1,
    deaths INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL NOT NULL DEFAULT 0,
    rank_tier TEXT NOT NULL DEFAULT 'D',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    rendered INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    rendered_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_gameplay_score_board
ON gameplay_score(game_id, score DESC, id);

CREATE INDEX IF NOT EXISTS idx_gameplay_clip_profile_status
ON gameplay_clip(profile_key, status, id);
CREATE INDEX IF NOT EXISTS idx_gameplay_clip_reserved_patch
ON gameplay_clip(reserved_patch_id, status);

"""


# Several components deliberately open their own connection to the same file so slow work
# never runs on the shared one (upload worker, playlist heartbeat, publish retry). With the
# default rollback journal any writer locks the whole file against every reader, so those
# short background writes would stall unrelated page loads. WAL lets readers run during a
# write, and busy_timeout makes the remaining writer-vs-writer overlap wait instead of
# failing with "database is locked".
_BUSY_TIMEOUT_MS = 15_000


def connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
    # A :memory: database has no journal file to write ahead to; the pragma is a harmless
    # no-op there (it answers "memory"), so no branch is needed.
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _migrate(conn)
    from app.gameplay_repository import seed_catalog
    seed_catalog(conn)
    conn.commit()


def _backfill_patch_video_links(conn: sqlite3.Connection) -> int:
    """Link legacy `videos` rows to the patch whose MP4 they hold.

    Patch videos are written to books/{book_id}/patch_videos/{patch_id}.mp4, so rows
    inserted before patch_id was populated can be recovered from their path. Without the
    link nothing can find a patch's video through the database, and the UNIQUE index on
    patch_id cannot dedupe the row, so the next writer adds a second one for the same file.
    """
    linked = 0
    rows = conn.execute(
        "SELECT id, file_path FROM videos WHERE patch_id IS NULL AND file_path IS NOT NULL"
    ).fetchall()
    for row in rows:
        path = Path(row["file_path"])
        if path.parent.name != "patch_videos":
            continue
        book_dir = path.parent.parent
        if not (path.stem.isdigit() and book_dir.name.isdigit()):
            continue
        patch_id, book_id = int(path.stem), int(book_dir.name)
        # The path only claims a patch id; the patch may have been deleted since, and
        # another row may already hold this patch_id (the index is UNIQUE).
        patch = conn.execute(
            "SELECT id FROM patch WHERE id=? AND book_id=?", (patch_id, book_id)
        ).fetchone()
        if patch is None:
            continue
        taken = conn.execute("SELECT 1 FROM videos WHERE patch_id=?", (patch_id,)).fetchone()
        if taken is not None:
            continue
        conn.execute(
            "UPDATE videos SET book_id=?, patch_id=? WHERE id=?", (book_id, patch_id, row["id"]))
        linked += 1
    return linked


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a book table already existed on disk."""
    conn.execute("DROP INDEX IF EXISTS idx_patch_pipeline_claim")
    conn.execute(
        "CREATE INDEX idx_patch_pipeline_claim ON patch_pipeline("
        "stage, next_retry_at, id)"
    )
    duplicates = conn.execute("SELECT book_id, channel_id, MIN(id) AS keep_id FROM youtube_playlist_map GROUP BY book_id, channel_id HAVING COUNT(*) > 1").fetchall()
    for duplicate in duplicates:
        conn.execute("DELETE FROM youtube_playlist_map WHERE book_id=? AND channel_id=? AND id<>?", (duplicate["book_id"], duplicate["channel_id"], duplicate["keep_id"]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_youtube_playlist_map_book_channel ON youtube_playlist_map(book_id, channel_id)")
    replay_existing = {row["name"] for row in conn.execute("PRAGMA table_info(gameplay_replay)")}
    for name, definition in {
        "game_id": "TEXT",
        "schema_version": "INTEGER",
        "simulation_version": "TEXT",
        "ruleset_version": "TEXT",
        "renderer_version": "TEXT",
        "payload_json": "TEXT",
        "result_json": "TEXT",
        "content_sha256": "TEXT",
    }.items():
        if name not in replay_existing:
            conn.execute(f"ALTER TABLE gameplay_replay ADD COLUMN {name} {definition}")
    clip_existing = {row["name"] for row in conn.execute("PRAGMA table_info(gameplay_clip)")}
    for name, definition in {
        "game_id": "TEXT",
        "render_profile_json": "TEXT",
        "validated_at": "TEXT",
        "validation_report_json": "TEXT",
    }.items():
        if name not in clip_existing:
            conn.execute(f"ALTER TABLE gameplay_clip ADD COLUMN {name} {definition}")
    pipeline_existing = {row["name"] for row in conn.execute("PRAGMA table_info(patch_pipeline)")}
    for name, definition in {
        "thumbnail_status": "TEXT NOT NULL DEFAULT 'pending'",
        "video_status": "TEXT NOT NULL DEFAULT 'pending'",
        "upload_status": "TEXT NOT NULL DEFAULT 'pending'",
        "playlist_status": "TEXT NOT NULL DEFAULT 'pending'",
        "config_snapshot": "TEXT NOT NULL DEFAULT '{}'",
        "media_snapshot": "TEXT NOT NULL DEFAULT '{}'",
    }.items():
        if name not in pipeline_existing:
            conn.execute(f"ALTER TABLE patch_pipeline ADD COLUMN {name} {definition}")
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(book)")}
    job_existing = {row["name"] for row in conn.execute("PRAGMA table_info(job)")}
    for name, definition in {
        "flow_run_id": "INTEGER REFERENCES flow_run(id) ON DELETE CASCADE",
        "node_id": "TEXT",
        "patch_id": "INTEGER REFERENCES patch(id) ON DELETE CASCADE",
    }.items():
        if name not in job_existing:
            conn.execute(f"ALTER TABLE job ADD COLUMN {name} {definition}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_job_flow_run ON job(flow_run_id, patch_id, id)")
    conn.execute(
        """UPDATE job SET patch_id=CAST(json_extract(payload_json, '$.patch_id') AS INTEGER)
           WHERE patch_id IS NULL
             AND json_type(payload_json, '$.patch_id') IN ('integer', 'real')
             AND EXISTS (
                 SELECT 1 FROM patch
                  WHERE patch.id=CAST(json_extract(job.payload_json, '$.patch_id') AS INTEGER)
             )"""
    )
    live_duplicates = conn.execute(
        """SELECT job_type, patch_id, MIN(id) AS keep_id FROM job
            WHERE patch_id IS NOT NULL
              AND status IN ('pending', 'running', 'cancelling')
            GROUP BY job_type, patch_id HAVING COUNT(*) > 1"""
    ).fetchall()
    for duplicate in live_duplicates:
        conn.execute(
            """UPDATE job SET status='cancelled',
                              error_message='duplicate job_type/patch_id removed by migration',
                              finished_at=updated_at
                WHERE job_type=? AND patch_id=? AND id<>?
                  AND status IN ('pending', 'running', 'cancelling')""",
            (duplicate["job_type"], duplicate["patch_id"], duplicate["keep_id"]),
        )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_job_live_type_patch
            ON job(job_type, patch_id)
            WHERE patch_id IS NOT NULL
              AND status IN ('pending', 'running', 'cancelling')"""
    )
    if "voice_clip_path" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN voice_clip_path TEXT")
    if "voice_transcript" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN voice_transcript TEXT")
    if "automation_config" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN automation_config TEXT")
    if "tts_model" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN tts_model TEXT")
    if "tts_max_chars" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN tts_max_chars INTEGER")
    if "tts_with_effects" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN tts_with_effects INTEGER NOT NULL DEFAULT 0")
    if "tts_voice_id" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN tts_voice_id TEXT")
    if "export_tts_model" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN export_tts_model TEXT")
    if "export_tts_max_chars" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN export_tts_max_chars INTEGER")
    if "export_tts_with_effects" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN export_tts_with_effects INTEGER")
    if "export_tts_voice_id" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN export_tts_voice_id TEXT")
    # book_job and app_state are CREATE TABLE IF NOT EXISTS, so they're picked up by
    # init_schema on a fresh DB and are a no-op on an existing DB; no per-column migration
    # is needed for them.
    chapter_existing = {row["name"] for row in conn.execute("PRAGMA table_info(chapter)")}
    if "is_excluded" not in chapter_existing:
        conn.execute("ALTER TABLE chapter ADD COLUMN is_excluded INTEGER NOT NULL DEFAULT 0")
    # chapter_no + text_hash let a re-imported EPUB be matched against the chapters already
    # stored, so new chapters are appended instead of rebuilding the whole book.
    if "chapter_no" not in chapter_existing:
        conn.execute("ALTER TABLE chapter ADD COLUMN chapter_no INTEGER")
    if "text_hash" not in chapter_existing:
        conn.execute("ALTER TABLE chapter ADD COLUMN text_hash TEXT")
    patch_existing = {row["name"] for row in conn.execute("PRAGMA table_info(patch)")}
    if "image_path" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN image_path TEXT")
    if "image_type" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN image_type TEXT NOT NULL DEFAULT 'static'")
    if "name" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN name TEXT")
    # Khoảng số chương của patch — nguồn để căn lại chỉ số sau khi re-import EPUB.
    if "chapter_no_start" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN chapter_no_start INTEGER")
    if "chapter_no_end" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN chapter_no_end INTEGER")
    if "chunk_count" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN chunk_count INTEGER NOT NULL DEFAULT 0")
    if "next_chunk_index" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN next_chunk_index INTEGER NOT NULL DEFAULT 0")
    if "chunk_count_exact" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN chunk_count_exact INTEGER NOT NULL DEFAULT 0")
    if "video_resolution" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN video_resolution TEXT NOT NULL DEFAULT '1920x1080'")
    if "video_fps" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN video_fps INTEGER NOT NULL DEFAULT 30")
    if "default_image_animation" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN default_image_animation TEXT NOT NULL DEFAULT 'none'")
    if "max_chars" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN max_chars INTEGER")
    if "clean_text" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN clean_text TEXT")
    if "clean_text_hash" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN clean_text_hash TEXT")
    if "text_fingerprint" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN text_fingerprint TEXT")
    if "youtube_override" not in patch_existing:
        conn.execute("ALTER TABLE patch ADD COLUMN youtube_override TEXT")
    if "normalize_numbers_enabled" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN normalize_numbers_enabled INTEGER NOT NULL DEFAULT 1")
    if "normalize_junk_enabled" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN normalize_junk_enabled INTEGER NOT NULL DEFAULT 1")
    if "normalize_spellcheck_enabled" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN normalize_spellcheck_enabled INTEGER NOT NULL DEFAULT 1")
    if "normalize_dictionary_enabled" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN normalize_dictionary_enabled INTEGER NOT NULL DEFAULT 0")
    if "normalize_transliteration_enabled" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN normalize_transliteration_enabled INTEGER NOT NULL DEFAULT 0")
    if "normalize_abbreviations_enabled" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN normalize_abbreviations_enabled INTEGER NOT NULL DEFAULT 1")
    if "normalize_breaks_enabled" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN normalize_breaks_enabled INTEGER NOT NULL DEFAULT 1")
    if "music_id" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN music_id INTEGER REFERENCES music(id)")
    if "music_volume" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN music_volume REAL NOT NULL DEFAULT 0.15")
    if "overlay_config" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN overlay_config TEXT")
    voice_meta_existing = {row["name"] for row in conn.execute("PRAGMA table_info(voice_meta)")}
    if "gender" not in voice_meta_existing:
        conn.execute("ALTER TABLE voice_meta ADD COLUMN gender TEXT NOT NULL DEFAULT ''")
    if "genre" not in voice_meta_existing:
        conn.execute("ALTER TABLE voice_meta ADD COLUMN genre TEXT NOT NULL DEFAULT ''")
    music_existing = {row["name"] for row in conn.execute("PRAGMA table_info(music)")}
    if "description" not in music_existing:
        conn.execute("ALTER TABLE music ADD COLUMN description TEXT NOT NULL DEFAULT ''")
    if "license" not in music_existing:
        conn.execute("ALTER TABLE music ADD COLUMN license TEXT NOT NULL DEFAULT ''")
    export_existing = {row["name"] for row in conn.execute("PRAGMA table_info(patch_export)")}
    if "drive_account_id" not in export_existing:
        conn.execute("ALTER TABLE patch_export ADD COLUMN drive_account_id INTEGER")
    if "sync_target_id" not in export_existing:
        conn.execute("ALTER TABLE patch_export ADD COLUMN sync_target_id INTEGER")
    if "local_folder_path" not in export_existing:
        conn.execute("ALTER TABLE patch_export ADD COLUMN local_folder_path TEXT")
    gdc_existing = {row["name"] for row in conn.execute("PRAGMA table_info(google_drive_credentials)")}
    if "oauth_client_id" not in gdc_existing:
        conn.execute("ALTER TABLE google_drive_credentials ADD COLUMN oauth_client_id INTEGER")
    uploads_existing = {row["name"] for row in conn.execute("PRAGMA table_info(youtube_uploads)")}
    if "video_id" not in uploads_existing:
        conn.execute("ALTER TABLE youtube_uploads ADD COLUMN video_id INTEGER REFERENCES videos(id) ON DELETE SET NULL")
    upload_columns = {
        "upload_progress": "REAL NOT NULL DEFAULT 0",
        "thumbnail_status": "TEXT NOT NULL DEFAULT 'pending'",
        "thumbnail_error": "TEXT",
        "playlist_status": "TEXT NOT NULL DEFAULT 'pending'",
        "playlist_error": "TEXT",
        "playlist_id": "TEXT",
        "metadata_snapshot": "TEXT",
        "retry_count": "INTEGER NOT NULL DEFAULT 0",
        "next_retry_at": "TEXT",
        "validation_status": "TEXT NOT NULL DEFAULT 'pending'",
        "validation_error_code": "TEXT",
        "validation_error_message": "TEXT",
        "validated_at": "TEXT",
        "integrity_retry_count": "INTEGER NOT NULL DEFAULT 0",
        "render_source_type": "TEXT NOT NULL DEFAULT 'external'",
        "render_source_id": "INTEGER",
    }
    for name, definition in upload_columns.items():
        if name not in uploads_existing:
            conn.execute(f"ALTER TABLE youtube_uploads ADD COLUMN {name} {definition}")
    if "validation_report_json" not in uploads_existing:
        conn.execute("ALTER TABLE youtube_uploads ADD COLUMN validation_report_json TEXT")
    pipeline_late = {row["name"] for row in conn.execute("PRAGMA table_info(patch_pipeline)")}
    for name, definition in {
        "preflight_status": "TEXT",
        "preflight_error_code": "TEXT",
        "preflight_error": "TEXT",
        "checked_at": "TEXT",
        "policy_snapshot": "TEXT",
        "snapshot_schema_version": "INTEGER NOT NULL DEFAULT 1",
        "render_attempts": "INTEGER NOT NULL DEFAULT 0",
        "validation_report_json": "TEXT",
        "republish_confirmed_for": "TEXT",
    }.items():
        if name not in pipeline_late:
            conn.execute(f"ALTER TABLE patch_pipeline ADD COLUMN {name} {definition}")
    # Per-book automation flags. New books default both ON (auto_create_video = auto
    # upload implies create). Legacy books keep their old behavior read from the
    # youtube.auto_upload flag in automation_config — strict: JSON true -> both on,
    # anything else (false/missing/broken) -> both off. Runs only when the column has
    # just been created, so later saves via youtube-settings drive the flags instead.
    if "auto_create_video" not in existing:
        conn.execute("ALTER TABLE book ADD COLUMN auto_create_video INTEGER NOT NULL DEFAULT 1")
        conn.execute("ALTER TABLE book ADD COLUMN auto_upload_youtube INTEGER NOT NULL DEFAULT 1")
        for row in conn.execute("SELECT id, automation_config FROM book").fetchall():
            legacy = False
            try:
                raw = json.loads(row["automation_config"] or "{}")
                youtube_cfg = raw.get("youtube")
                legacy = isinstance(youtube_cfg, dict) and isinstance(
                    youtube_cfg.get("auto_upload"), bool) and youtube_cfg["auto_upload"] is True
            except (TypeError, ValueError, json.JSONDecodeError):
                legacy = False
            conn.execute(
                "UPDATE book SET auto_create_video=?, auto_upload_youtube=? WHERE id=?",
                (1 if legacy else 0, 1 if legacy else 0, row["id"]),
            )
    videos_existing = {row["name"] for row in conn.execute("PRAGMA table_info(videos)")}
    if "book_id" not in videos_existing:
        conn.execute("ALTER TABLE videos ADD COLUMN book_id INTEGER REFERENCES book(id) ON DELETE SET NULL")
    if "patch_id" not in videos_existing:
        conn.execute("ALTER TABLE videos ADD COLUMN patch_id INTEGER REFERENCES patch(id) ON DELETE SET NULL")
    if "render_config_json" not in videos_existing:
        conn.execute("ALTER TABLE videos ADD COLUMN render_config_json TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_videos_patch_id ON videos(patch_id) WHERE patch_id IS NOT NULL")
    _backfill_patch_video_links(conn)
    sync_target_existing = {row["name"] for row in conn.execute("PRAGMA table_info(drive_sync_target)")}
    if "rclone_remote" not in sync_target_existing:
        conn.execute("ALTER TABLE drive_sync_target ADD COLUMN rclone_remote TEXT")
    from app.config import settings
    if settings.google_drive_client_id:
        row = conn.execute("SELECT 1 FROM drive_oauth_client LIMIT 1").fetchone()
        if row is None:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """INSERT INTO drive_oauth_client (name, client_id, client_secret, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?)""",
                ("Default OAuth Client", settings.google_drive_client_id, settings.google_drive_client_secret, now, now),
            )
