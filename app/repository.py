"""CRUD operations for book/chapter/patch, plus combined DB+filesystem operations."""
from __future__ import annotations

import logging
import math
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.audio_merge import cleanup_chunk_dir
from app.chunker import group_into_patches, split_into_tts_chunks
from app.epub_parser import ParsedChapter
from app.models import Book, BookJob, Chapter, Patch, PatchExport, TextReplaceRule

logger = logging.getLogger(__name__)

ACTIVE_PATCH_STATUSES = {"pending", "done", "failed"}  # never 'processing' - that's worker-owned
_TTS_MAX_CHARS = 400  # default matches config.settings.tts_max_chars


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunk_dir_for(book_id: int, patch_id: int) -> Path:
    return Path("data") / "books" / str(book_id) / "patches" / f"{patch_id}_chunks"


def _delete_chunk_dir(book_id: int, patch_id: int) -> None:
    cleanup_chunk_dir(str(_chunk_dir_for(book_id, patch_id)))


def _book_from_row(row: sqlite3.Row) -> Book:
    return Book(**{k: row[k] for k in row.keys()})


def _chapter_from_row(row: sqlite3.Row) -> Chapter:
    d = {k: row[k] for k in row.keys()}
    d["is_excluded"] = bool(d.get("is_excluded", False))
    return Chapter(**d)


def _patch_from_row(row: sqlite3.Row) -> Patch:
    return Patch(**{k: row[k] for k in row.keys()})


def _rule_from_row(row: sqlite3.Row) -> TextReplaceRule:
    d = {k: row[k] for k in row.keys()}
    d["is_regex"] = bool(d["is_regex"])
    return TextReplaceRule(**d)


def _bookjob_from_row(row: sqlite3.Row) -> BookJob:
    return BookJob(**{k: row[k] for k in row.keys()})


def create_book(
    conn: sqlite3.Connection,
    *,
    title: str,
    original_filename: str,
    epub_path: str,
    patch_size: int,
    chapters: list[ParsedChapter],
    background_image_path: str | None,
    voice_clip_path: str | None = None,
    voice_transcript: str | None = None,
) -> Book:
    now = _now()
    cur = conn.execute(
        """INSERT INTO book (title, original_filename, epub_path, patch_size, status,
                              background_image_path, voice_clip_path, voice_transcript,
                              created_at, updated_at)
           VALUES (?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?)""",
        (
            title, original_filename, epub_path, patch_size, background_image_path,
            voice_clip_path, voice_transcript, now, now,
        ),
    )
    book_id = cur.lastrowid

    conn.executemany(
        """INSERT INTO chapter (book_id, chapter_index, title, text, char_count)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (book_id, idx, ch.title, ch.text, ch.char_count)
            for idx, ch in enumerate(chapters)
        ],
    )

    conn.commit()
    return get_book(conn, book_id)


def get_book(conn: sqlite3.Connection, book_id: int) -> Book | None:
    row = conn.execute("SELECT * FROM book WHERE id = ?", (book_id,)).fetchone()
    return _book_from_row(row) if row else None


def list_books(conn: sqlite3.Connection) -> list[Book]:
    rows = conn.execute("SELECT * FROM book ORDER BY created_at DESC").fetchall()
    return [_book_from_row(r) for r in rows]


def list_chapters(conn: sqlite3.Connection, book_id: int) -> list[Chapter]:
    rows = conn.execute(
        "SELECT * FROM chapter WHERE book_id = ? ORDER BY chapter_index", (book_id,)
    ).fetchall()
    return [_chapter_from_row(r) for r in rows]


def get_chapters_in_range(
    conn: sqlite3.Connection, book_id: int, chapter_start: int, chapter_end: int
) -> list[Chapter]:
    rows = conn.execute(
        """SELECT * FROM chapter WHERE book_id = ? AND chapter_index BETWEEN ? AND ?
           ORDER BY chapter_index""",
        (book_id, chapter_start, chapter_end),
    ).fetchall()
    return [_chapter_from_row(r) for r in rows]


def get_chapters_by_indices(
    conn: sqlite3.Connection, book_id: int, indices: list[int]
) -> list[Chapter]:
    """Return chapters matching any of the given chapter_index values, in ascending
    chapter_index order. Unknown indices are silently skipped."""
    if not indices:
        return []
    placeholders = ",".join("?" for _ in indices)
    rows = conn.execute(
        f"""SELECT * FROM chapter WHERE book_id = ? AND chapter_index IN ({placeholders})
            ORDER BY chapter_index""",
        (book_id, *indices),
    ).fetchall()
    return [_chapter_from_row(r) for r in rows]


def get_chapter_text(
    conn: sqlite3.Connection, book_id: int, chapter_index: int
) -> str | None:
    """Return the full text of a single chapter, or None if it doesn't exist."""
    row = conn.execute(
        "SELECT text FROM chapter WHERE book_id = ? AND chapter_index = ?",
        (book_id, chapter_index),
    ).fetchone()
    return row["text"] if row else None


def list_patches(conn: sqlite3.Connection, book_id: int) -> list[Patch]:
    rows = conn.execute(
        "SELECT * FROM patch WHERE book_id = ? ORDER BY patch_index", (book_id,)
    ).fetchall()
    return [_patch_from_row(r) for r in rows]


def get_patch(conn: sqlite3.Connection, patch_id: int) -> Patch | None:
    row = conn.execute("SELECT * FROM patch WHERE id = ?", (patch_id,)).fetchone()
    return _patch_from_row(row) if row else None


def claim_next_pending_patch(conn: sqlite3.Connection) -> Patch | None:
    """Atomically claim the next pending patch (lowest book_id, then patch_index) by flipping
    its status to 'processing'. Uses BEGIN IMMEDIATE to hold the write lock across the
    select-then-update, avoiding a check-then-act race if more than one worker ever runs."""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT id FROM patch WHERE status = 'pending' ORDER BY book_id, patch_index LIMIT 1"
    ).fetchone()
    if row is None:
        conn.commit()
        return None
    conn.execute(
        """UPDATE patch SET status = 'processing', updated_at = ?, attempt_count = attempt_count + 1
           WHERE id = ?""",
        (_now(), row["id"]),
    )
    conn.commit()
    return get_patch(conn, row["id"])


def mark_patch_done(conn: sqlite3.Connection, patch_id: int, audio_path: str) -> None:
    conn.execute(
        """UPDATE patch SET status = 'done', audio_path = ?, error_message = NULL, updated_at = ?
           WHERE id = ?""",
        (audio_path, _now(), patch_id),
    )
    conn.commit()


def update_patch_chunk_count(conn: sqlite3.Connection, patch_id: int, chunk_count: int) -> None:
    conn.execute(
        "UPDATE patch SET chunk_count = ?, updated_at = ? WHERE id = ?",
        (chunk_count, _now(), patch_id),
    )
    conn.commit()


def update_patch_chunk_progress(
    conn: sqlite3.Connection, patch_id: int, next_chunk_index: int
) -> None:
    """Persist how many chunks of a patch have been written to disk so a worker
    restart can resume from this index instead of redoing the patch from scratch.
    next_chunk_index is 1-based: the count of completed chunks."""
    conn.execute(
        "UPDATE patch SET next_chunk_index = ?, updated_at = ? WHERE id = ?",
        (next_chunk_index, _now(), patch_id),
    )
    conn.commit()


def set_patch_max_chars(conn: sqlite3.Connection, patch_id: int, max_chars: int | None) -> bool:
    """Override the TTS chunk-size cap for a single patch. Only allowed while the patch
    hasn't started synthesis (status == 'pending') - once chunk files exist on disk,
    changing max_chars would desync their indices from a re-split of the text. Recomputes
    chunk_count with the same estimate formula as rebuild_patches/preview_auto_build so the
    UI shows an accurate preview before the worker actually chunks the text."""
    patch = get_patch(conn, patch_id)
    if patch is None or patch.status != "pending":
        return False
    effective = max_chars if max_chars else _TTS_MAX_CHARS
    total_chars = conn.execute(
        "SELECT COALESCE(SUM(char_count), 0) AS c FROM chapter WHERE book_id = ? AND chapter_index BETWEEN ? AND ?",
        (patch.book_id, patch.chapter_start, patch.chapter_end),
    ).fetchone()["c"]
    chunk_count = max(1, math.ceil(total_chars / effective))
    conn.execute(
        "UPDATE patch SET max_chars = ?, chunk_count = ?, updated_at = ? WHERE id = ?",
        (max_chars, chunk_count, _now(), patch_id),
    )
    conn.commit()
    return True


def resume_patch_from_chunk(conn: sqlite3.Connection, patch_id: int, from_index: int) -> bool:
    """Resume a failed patch starting at a chosen chunk index instead of regenerating the
    whole patch from scratch. Chunk files before from_index are left alone (already
    synthesized); files at or after from_index are deleted so the worker's normal chunk
    loop (worker.py _synthesize, start_index logic) regenerates them cleanly."""
    patch = get_patch(conn, patch_id)
    if patch is None or patch.status != "failed":
        return False
    from_index = max(0, min(from_index, patch.next_chunk_index))
    chunk_dir = _chunk_dir_for(patch.book_id, patch_id)
    if chunk_dir.exists():
        for i in range(from_index, max(patch.chunk_count, patch.next_chunk_index)):
            (chunk_dir / f"chunk_{i:03d}.wav").unlink(missing_ok=True)
    conn.execute(
        """UPDATE patch SET status = 'pending', next_chunk_index = ?, error_message = NULL,
           updated_at = ? WHERE id = ?""",
        (from_index, _now(), patch_id),
    )
    conn.commit()
    return True


def get_patch_chunk_view(conn: sqlite3.Connection, patch: Patch, worker=None) -> list[dict]:
    """Compute per-chunk status on demand instead of maintaining a separate chunk table.
    Chunk texts always come from the same split_into_tts_chunks call the worker makes
    (worker.py _synthesize), so indices line up with whatever chunk_NNN.wav files are (or
    aren't) currently on disk."""
    text = build_patch_text(conn, patch)
    max_chars = patch.max_chars or _TTS_MAX_CHARS
    chunks = split_into_tts_chunks(text, max_chars=max_chars)

    current_index = None
    if worker is not None and getattr(worker, "current_patch_id", None) == patch.id:
        current_index = worker.current_chunk_index

    result = []
    for i, chunk_text in enumerate(chunks):
        if patch.status == "done":
            status = "done"
        elif patch.status == "processing" and i == current_index:
            status = "processing"
        elif i < patch.next_chunk_index:
            status = "done"
        elif i == patch.next_chunk_index and patch.status == "failed":
            status = "failed"
        else:
            status = "pending"
        result.append({
            "index": i,
            "char_count": len(chunk_text),
            "status": status,
            "preview_text": chunk_text[:160],
        })
    return result


def mark_patch_failed(conn: sqlite3.Connection, patch_id: int, error_message: str) -> None:
    conn.execute(
        "UPDATE patch SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
        (error_message, _now(), patch_id),
    )
    conn.commit()


def requeue_stuck_processing(conn: sqlite3.Connection) -> int:
    """Call once at startup: any patch left 'processing' means the previous run crashed mid-job.
    next_chunk_index is preserved so the worker resumes the patch at the chunk level instead of
    redoing every chunk from scratch."""
    cur = conn.execute(
        """UPDATE patch SET status = 'pending', error_message = 'requeued after restart', updated_at = ?
           WHERE status = 'processing'""",
        (_now(),),
    )
    conn.commit()
    return cur.rowcount


def requeue_stuck_processing_returning(conn: sqlite3.Connection) -> list[dict]:
    """Same as requeue_stuck_processing, but returns the rows it touched so callers can report
    what was preserved (in particular next_chunk_index) to the operator / UI."""
    rows = conn.execute(
        """SELECT id, book_id, chunk_count, next_chunk_index FROM patch
            WHERE status = 'processing'"""
    ).fetchall()
    if not rows:
        return []
    conn.execute(
        """UPDATE patch SET status = 'pending', error_message = 'requeued after restart',
           updated_at = ? WHERE status = 'processing'""",
        (_now(),),
    )
    conn.commit()
    return [
        {
            "patch_id": r["id"],
            "book_id": r["book_id"],
            "chunk_count": r["chunk_count"],
            "next_chunk_index": r["next_chunk_index"],
        }
        for r in rows
    ]


def reset_patch(conn: sqlite3.Connection, patch_id: int) -> bool:
    """Reset a patch back to pending (used for both 'regenerate' and 'delete output' UI actions -
    deleting the row outright would break chapter-range bookkeeping). Deletes its wav file and
    invalidates the book's stale final outputs. Refuses if the patch is currently 'processing'."""
    patch = get_patch(conn, patch_id)
    if patch is None or patch.status not in ACTIVE_PATCH_STATUSES:
        return False

    if patch.audio_path:
        Path(patch.audio_path).unlink(missing_ok=True)

    video_dir = Path("data") / "books" / str(patch.book_id) / "patch_videos"
    video_file = video_dir / f"{patch_id}.mp4"
    if video_file.exists():
        video_file.unlink(missing_ok=True)

    _delete_chunk_dir(patch.book_id, patch_id)

    conn.execute(
        """UPDATE patch SET status = 'pending', audio_path = NULL, error_message = NULL,
           next_chunk_index = 0, updated_at = ? WHERE id = ?""",
        (_now(), patch_id),
    )
    conn.execute(
        """UPDATE book SET final_audio_path = NULL, final_video_path = NULL, status = 'processing', updated_at = ?
           WHERE id = ?""",
        (_now(), patch.book_id),
    )
    conn.commit()
    return True


def delete_patch(conn: sqlite3.Connection, patch_id: int) -> bool:
    """Delete a single patch, clean up its files, and renumber remaining patches.
    Refuses if the patch is currently 'processing'."""
    patch = get_patch(conn, patch_id)
    if patch is None or patch.status == "processing":
        return False

    book_id = patch.book_id

    if patch.audio_path:
        Path(patch.audio_path).unlink(missing_ok=True)
    if patch.image_path:
        Path(patch.image_path).unlink(missing_ok=True)
    video_dir = Path("data") / "books" / str(book_id) / "patch_videos"
    video_file = video_dir / f"{patch_id}.mp4"
    if video_file.exists():
        video_file.unlink(missing_ok=True)

    _delete_chunk_dir(book_id, patch_id)

    conn.execute("DELETE FROM patch WHERE id = ?", (patch_id,))

    remaining = conn.execute(
        "SELECT id FROM patch WHERE book_id = ? ORDER BY patch_index",
        (book_id,),
    ).fetchall()
    for new_idx, row in enumerate(remaining):
        conn.execute(
            "UPDATE patch SET patch_index = ?, updated_at = ? WHERE id = ?",
            (new_idx, _now(), row["id"]),
        )

    conn.execute(
        """UPDATE book SET final_audio_path = NULL, final_video_path = NULL,
           status = 'ready', updated_at = ? WHERE id = ?""",
        (_now(), book_id),
    )
    conn.commit()
    return True


def all_patches_done(conn: sqlite3.Connection, book_id: int) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM patch WHERE book_id = ? AND status != 'done'", (book_id,)
    ).fetchone()
    return row["c"] == 0


def any_patch_failed(conn: sqlite3.Connection, book_id: int) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM patch WHERE book_id = ? AND status = 'failed'", (book_id,)
    ).fetchone()
    return row["c"] > 0


def set_book_status(conn: sqlite3.Connection, book_id: int, status: str) -> None:
    conn.execute(
        "UPDATE book SET status = ?, updated_at = ? WHERE id = ?", (status, _now(), book_id)
    )
    conn.commit()


def set_book_final_audio(conn: sqlite3.Connection, book_id: int, final_audio_path: str) -> None:
    conn.execute(
        """UPDATE book SET final_audio_path = ?, status = 'done', updated_at = ? WHERE id = ?""",
        (final_audio_path, _now(), book_id),
    )
    conn.commit()


def set_book_final_video(conn: sqlite3.Connection, book_id: int, final_video_path: str) -> None:
    conn.execute(
        "UPDATE book SET final_video_path = ?, updated_at = ? WHERE id = ?",
        (final_video_path, _now(), book_id),
    )
    conn.commit()


def delete_book(conn: sqlite3.Connection, book_id: int, data_root: str) -> bool:
    """Delete a book's DB rows (cascades to chapter/patch/book_job) and its files on disk.

    If any of its patches is still 'processing' (the most likely cause is a worker crash
    that left a patch in this state), the patch is requeued to 'pending' first so the
    guard doesn't block the delete. The worker, if still alive, will fail gracefully
    because the book / chapter / patch rows are now gone (ON DELETE CASCADE)."""
    now = _now()
    cur = conn.execute(
        """UPDATE patch SET status = 'pending',
           error_message = COALESCE(error_message, 'requeued before book deletion'),
           updated_at = ? WHERE book_id = ? AND status = 'processing'""",
        (now, book_id),
    )
    if cur.rowcount > 0:
        logger.info(
            "delete_book: requeued %s processing patch(es) for book_id=%s before delete",
            cur.rowcount, book_id,
        )

    for row in conn.execute(
        "SELECT id FROM patch WHERE book_id = ?", (book_id,)
    ).fetchall():
        _delete_chunk_dir(book_id, row["id"])

    book = get_book(conn, book_id)
    if book is None:
        logger.warning("delete_book refused: book_id=%s not found in DB", book_id)
        return False

    conn.execute("DELETE FROM book WHERE id = ?", (book_id,))
    conn.commit()

    try:
        Path(book.epub_path).unlink(missing_ok=True)
    except OSError as e:
        logger.warning("delete_book: could not unlink epub %s: %s", book.epub_path, e)
    book_dir = Path(data_root) / "books" / str(book_id)
    if book_dir.exists():
        import shutil
        shutil.rmtree(book_dir, ignore_errors=True)
    uploads_patch_dir = Path(data_root) / "uploads" / str(book_id)
    if uploads_patch_dir.exists():
        import shutil
        shutil.rmtree(uploads_patch_dir, ignore_errors=True)
    logger.info("delete_book succeeded for book_id=%s", book_id)
    return True


# ---------------------------------------------------------------------------
# Chapter exclude
# ---------------------------------------------------------------------------


def set_chapter_excluded(
    conn: sqlite3.Connection, book_id: int, chapter_index: int, excluded: bool
) -> bool:
    cur = conn.execute(
        "UPDATE chapter SET is_excluded = ? WHERE book_id = ? AND chapter_index = ?",
        (1 if excluded else 0, book_id, chapter_index),
    )
    conn.commit()
    return cur.rowcount > 0


def list_included_chapters(
    conn: sqlite3.Connection, book_id: int
) -> list[Chapter]:
    rows = conn.execute(
        "SELECT * FROM chapter WHERE book_id = ? AND is_excluded = 0 ORDER BY chapter_index",
        (book_id,),
    ).fetchall()
    return [_chapter_from_row(r) for r in rows]


# ---------------------------------------------------------------------------
# Replace rules repository
# ---------------------------------------------------------------------------


def list_replace_rules(
    conn: sqlite3.Connection, book_id: int
) -> list[TextReplaceRule]:
    rows = conn.execute(
        "SELECT * FROM text_replace_rule WHERE book_id = ? ORDER BY position, id",
        (book_id,),
    ).fetchall()
    return [_rule_from_row(r) for r in rows]


def create_replace_rule(
    conn: sqlite3.Connection,
    book_id: int,
    find: str,
    replace: str,
    is_regex: bool,
    position: int,
) -> TextReplaceRule:
    if not find:
        raise ValueError("find must not be empty")
    if is_regex:
        try:
            re.compile(find)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
    cur = conn.execute(
        """INSERT INTO text_replace_rule (book_id, find, replace, is_regex, position)
           VALUES (?, ?, ?, ?, ?)""",
        (book_id, find, replace, 1 if is_regex else 0, position),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM text_replace_rule WHERE id = ?", (cur.lastrowid,)
    ).fetchone()
    return _rule_from_row(row)


def update_replace_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    find: str | None = None,
    replace: str | None = None,
    is_regex: bool | None = None,
    position: int | None = None,
) -> TextReplaceRule | None:
    existing = conn.execute(
        "SELECT * FROM text_replace_rule WHERE id = ?", (rule_id,)
    ).fetchone()
    if existing is None:
        return None
    new_find = find if find is not None else existing["find"]
    new_replace = replace if replace is not None else existing["replace"]
    new_is_regex = is_regex if is_regex is not None else bool(existing["is_regex"])
    new_position = position if position is not None else existing["position"]
    if not new_find:
        raise ValueError("find must not be empty")
    if new_is_regex:
        try:
            re.compile(new_find)
        except re.error as exc:
            raise ValueError(f"invalid regex: {exc}") from exc
    conn.execute(
        """UPDATE text_replace_rule
           SET find = ?, replace = ?, is_regex = ?, position = ?
           WHERE id = ?""",
        (new_find, new_replace, 1 if new_is_regex else 0, new_position, rule_id),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM text_replace_rule WHERE id = ?", (rule_id,)
    ).fetchone()
    return _rule_from_row(row)


def delete_replace_rule(conn: sqlite3.Connection, rule_id: int) -> bool:
    cur = conn.execute("DELETE FROM text_replace_rule WHERE id = ?", (rule_id,))
    conn.commit()
    return cur.rowcount > 0


def apply_replace_rules(text: str, rules: list[TextReplaceRule]) -> str:
    """Pure function: apply rules in position order, then insertion order for ties."""
    result = text
    for rule in rules:
        if rule.is_regex:
            result = re.sub(rule.find, rule.replace, result)
        else:
            result = result.replace(rule.find, rule.replace)
    return result


# ---------------------------------------------------------------------------
# Patch image CRUD
# ---------------------------------------------------------------------------


def save_patch_image(conn: sqlite3.Connection, patch_id: int, file_path: str) -> str:
    """Set image_path for a patch. Old image file is NOT deleted here (caller handles cleanup)."""
    now = _now()
    conn.execute(
        "UPDATE patch SET image_path = ?, updated_at = ? WHERE id = ?",
        (file_path, now, patch_id),
    )
    conn.commit()
    return file_path


def clear_patch_image(conn: sqlite3.Connection, patch_id: int) -> bool:
    """Set image_path = NULL for a patch. Returns True if a row was updated."""
    now = _now()
    cur = conn.execute(
        "UPDATE patch SET image_path = NULL, updated_at = ? WHERE id = ?",
        (now, patch_id),
    )
    conn.commit()
    return cur.rowcount > 0


def update_patch_image_type(conn: sqlite3.Connection, patch_id: int, image_type: str) -> bool:
    """Update image_type for a patch (static | zoom-in | zoom-out | pan-left | pan-right)."""
    now = _now()
    cur = conn.execute(
        "UPDATE patch SET image_type = ?, updated_at = ? WHERE id = ?",
        (image_type, now, patch_id),
    )
    conn.commit()
    return cur.rowcount > 0


def update_book_video_settings(
    conn: sqlite3.Connection,
    book_id: int,
    *,
    video_resolution: str | None = None,
    video_fps: int | None = None,
    default_image_animation: str | None = None,
) -> None:
    """Update video settings for a book."""
    parts = []
    params = []
    if video_resolution is not None:
        parts.append("video_resolution = ?")
        params.append(video_resolution)
    if video_fps is not None:
        parts.append("video_fps = ?")
        params.append(video_fps)
    if default_image_animation is not None:
        parts.append("default_image_animation = ?")
        params.append(default_image_animation)
    if not parts:
        return
    parts.append("updated_at = ?")
    params.append(_now())
    params.append(book_id)
    conn.execute(f"UPDATE book SET {', '.join(parts)} WHERE id = ?", params)
    conn.commit()


def reset_done_patches_for_book(conn: sqlite3.Connection, book_id: int) -> int:
    done_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM patch WHERE book_id = ? AND status = 'done'",
            (book_id,),
        ).fetchall()
    ]
    now = _now()
    cur = conn.execute(
        """UPDATE patch SET status = 'pending', audio_path = NULL, error_message = NULL,
           next_chunk_index = 0, updated_at = ? WHERE book_id = ? AND status = 'done'""",
        (now, book_id),
    )
    for pid in done_ids:
        _delete_chunk_dir(book_id, pid)
    conn.execute(
        """UPDATE book SET final_audio_path = NULL, final_video_path = NULL,
           status = 'ready', updated_at = ? WHERE id = ?""",
        (now, book_id),
    )
    conn.commit()
    return cur.rowcount


# ---------------------------------------------------------------------------
# Custom patch rebuild
# ---------------------------------------------------------------------------


def rebuild_patches(
    conn: sqlite3.Connection,
    book_id: int,
    ranges: list[tuple[int, int]],
    reset_done: bool = False,
) -> list[Patch]:
    """Replace all patches for this book. Validates ranges, deletes old patches,
    inserts new ones. Resets book state."""
    ranges = list(ranges)
    if not ranges:
        raise ValueError("ranges must not be empty")

    for i, (a_start, a_end) in enumerate(ranges):
        if a_start > a_end:
            raise ValueError(f"range {i} [{a_start},{a_end}]: start must be <= end")
        for j, (b_start, b_end) in enumerate(ranges):
            if j <= i:
                continue
            if a_end >= b_start and b_end >= a_start:
                raise ValueError(
                    f"overlapping ranges: [{a_start},{a_end}] and [{b_start},{b_end}]"
                )

    excluded_indices = {
        r["chapter_index"]
        for r in conn.execute(
            "SELECT chapter_index FROM chapter WHERE book_id = ? AND is_excluded = 1",
            (book_id,),
        )
    }
    for i, (start, end) in enumerate(ranges):
        for ci in range(start, end + 1):
            if ci in excluded_indices:
                raise ValueError(
                    f"range {i} [{start},{end}] includes excluded chapter {ci}"
                )

    existing = conn.execute(
        "SELECT chapter_index FROM chapter WHERE book_id = ?", (book_id,)
    ).fetchall()
    max_index = max(r["chapter_index"] for r in existing) if existing else -1
    for i, (start, end) in enumerate(ranges):
        if start < 0 or end > max_index:
            raise ValueError(
                f"range {i} [{start},{end}] out of bounds (0-{max_index})"
            )

    if reset_done:
        patterns = list_patches(conn, book_id)
        for p in patterns:
            if p.status == "done" and p.audio_path:
                Path(p.audio_path).unlink(missing_ok=True)
            if p.image_path:
                Path(p.image_path).unlink(missing_ok=True)
            _delete_chunk_dir(book_id, p.id)
    conn.execute("DELETE FROM patch WHERE book_id = ?", (book_id,))
    now = _now()
    patch_rows = []
    for idx, (start, end) in enumerate(ranges):
        row = conn.execute(
            "SELECT title FROM chapter WHERE book_id = ? AND chapter_index = ?",
            (book_id, start),
        ).fetchone()
        name = row["title"] if row else ""
        total_chars = conn.execute(
            "SELECT COALESCE(SUM(char_count), 0) AS c FROM chapter WHERE book_id = ? AND chapter_index BETWEEN ? AND ?",
            (book_id, start, end),
        ).fetchone()["c"]
        chunk_count = max(1, math.ceil(total_chars / _TTS_MAX_CHARS))
        patch_rows.append((book_id, idx, start, end, name, chunk_count, now, now))
    conn.executemany(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, name,
                               chunk_count, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        patch_rows,
    )
    conn.execute(
        """UPDATE book SET final_audio_path = NULL, final_video_path = NULL,
           status = 'ready', updated_at = ? WHERE id = ?""",
        (now, book_id),
    )
    conn.commit()
    return list_patches(conn, book_id)


# ---------------------------------------------------------------------------
# Patch text builder
# ---------------------------------------------------------------------------


_TITLE_END_PUNCTUATION = frozenset(".!?…:;,)]\"'»")


def build_patch_text(conn: sqlite3.Connection, patch: Patch) -> str:
    """Return the full text for a patch: included chapter texts joined, with
    the book's replace rules applied."""
    chapters = get_chapters_in_range(
        conn, patch.book_id, patch.chapter_start, patch.chapter_end
    )
    included = [ch for ch in chapters if not ch.is_excluded]
    texts: list[str] = []
    for ch in included:
        t = ch.text
        if ch.title and t.startswith(ch.title) and ch.title[-1] not in _TITLE_END_PUNCTUATION:
            suffix = t[len(ch.title):].lstrip()
            if suffix:
                t = ch.title + ".\n\n" + suffix
        texts.append(t)
    raw = "\n\n".join(texts)
    rules = list_replace_rules(conn, patch.book_id)
    return apply_replace_rules(raw, rules)


# ---------------------------------------------------------------------------
# Auto-build patches
# ---------------------------------------------------------------------------


def preview_auto_build(
    conn: sqlite3.Connection,
    book_id: int,
    start_chapter: int,
    end_chapter: int | None = None,
    patch_size: int | None = None,
) -> list[dict]:
    """Compute planned patches without writing to DB. Returns list of
    {patch_index, chapter_start, chapter_end, chunk_count} dicts."""
    book = get_book(conn, book_id)
    if book is None:
        raise ValueError(f"book {book_id} not found")

    if patch_size is None:
        patch_size = book.patch_size
    if patch_size < 1:
        raise ValueError("patch_size must be >= 1")

    max_idx_row = conn.execute(
        "SELECT MAX(chapter_index) AS m FROM chapter WHERE book_id = ?",
        (book_id,),
    ).fetchone()
    max_index = max_idx_row["m"] if max_idx_row["m"] is not None else -1

    if start_chapter < 0:
        raise ValueError("start_chapter must be >= 0")
    if start_chapter > max_index:
        raise ValueError(f"start_chapter {start_chapter} out of bounds (max chapter is {max_index})")

    if end_chapter is None:
        end_chapter = max_index
    if end_chapter < start_chapter:
        raise ValueError(f"end_chapter must be >= start_chapter ({start_chapter})")
    if end_chapter > max_index:
        end_chapter = max_index

    rows = conn.execute(
        """SELECT chapter_index FROM chapter
           WHERE book_id = ? AND is_excluded = 0
             AND chapter_index >= ? AND chapter_index <= ?
           ORDER BY chapter_index""",
        (book_id, start_chapter, end_chapter),
    ).fetchall()
    included = [r["chapter_index"] for r in rows]
    if not included:
        raise ValueError(f"no included chapters in range [{start_chapter}, {end_chapter}]")

    ranges: list[tuple[int, int]] = []
    for i in range(0, len(included), patch_size):
        chunk = included[i : i + patch_size]
        ranges.append((chunk[0], chunk[-1]))

    result: list[dict] = []
    for idx, (start, end) in enumerate(ranges):
        row = conn.execute(
            "SELECT title FROM chapter WHERE book_id = ? AND chapter_index = ?",
            (book_id, start),
        ).fetchone()
        name = row["title"] if row else ""
        total_chars = conn.execute(
            "SELECT COALESCE(SUM(char_count), 0) AS c FROM chapter WHERE book_id = ? AND chapter_index BETWEEN ? AND ?",
            (book_id, start, end),
        ).fetchone()["c"]
        chunk_count = max(1, math.ceil(total_chars / _TTS_MAX_CHARS))
        result.append({
            "patch_index": idx,
            "chapter_start": start,
            "chapter_end": end,
            "name": name,
            "chunk_count": chunk_count,
        })
    return result


def auto_build_patches(
    conn: sqlite3.Connection,
    book_id: int,
    start_chapter: int,
    end_chapter: int | None = None,
    patch_size: int | None = None,
) -> list[Patch]:
    """Generate a patch list from start_chapter to end_chapter (or max chapter)
    in chunks of patch_size (or book.patch_size), skipping excluded chapters."""

    book = get_book(conn, book_id)
    if book is None:
        raise ValueError(f"book {book_id} not found")

    if patch_size is None:
        patch_size = book.patch_size
    if patch_size < 1:
        raise ValueError("patch_size must be >= 1")

    max_idx_row = conn.execute(
        "SELECT MAX(chapter_index) AS m FROM chapter WHERE book_id = ?",
        (book_id,),
    ).fetchone()
    max_index = max_idx_row["m"] if max_idx_row["m"] is not None else -1

    if start_chapter < 0:
        raise ValueError("start_chapter must be >= 0")
    if start_chapter > max_index:
        raise ValueError(f"start_chapter {start_chapter} out of bounds (max chapter is {max_index})")

    if end_chapter is None:
        end_chapter = max_index
    if end_chapter < start_chapter:
        raise ValueError(f"end_chapter must be >= start_chapter ({start_chapter})")
    if end_chapter > max_index:
        end_chapter = max_index

    rows = conn.execute(
        """SELECT chapter_index FROM chapter
           WHERE book_id = ? AND is_excluded = 0
             AND chapter_index >= ? AND chapter_index <= ?
           ORDER BY chapter_index""",
        (book_id, start_chapter, end_chapter),
    ).fetchall()
    included = [r["chapter_index"] for r in rows]
    if not included:
        raise ValueError(f"no included chapters in range [{start_chapter}, {end_chapter}]")

    ranges: list[tuple[int, int]] = []
    for i in range(0, len(included), patch_size):
        chunk = included[i : i + patch_size]
        ranges.append((chunk[0], chunk[-1]))

    return rebuild_patches(conn, book_id, ranges, reset_done=True)


# ---------------------------------------------------------------------------
# Book job (video) repository
# ---------------------------------------------------------------------------


def get_book_job(
    conn: sqlite3.Connection, book_id: int, job_type: str
) -> BookJob | None:
    row = conn.execute(
        "SELECT * FROM book_job WHERE book_id = ? AND job_type = ?",
        (book_id, job_type),
    ).fetchone()
    return _bookjob_from_row(row) if row else None


def claim_next_pending_book_job(conn: sqlite3.Connection) -> BookJob | None:
    """Atomically claim the next pending book_job (lowest book_id, then id) by flipping
    its status to 'processing' and bumping attempt_count. Mirrors claim_next_pending_patch."""
    conn.execute("BEGIN IMMEDIATE")
    row = conn.execute(
        "SELECT id FROM book_job WHERE status = 'pending' ORDER BY book_id, id LIMIT 1"
    ).fetchone()
    if row is None:
        conn.commit()
        return None
    conn.execute(
        """UPDATE book_job SET status = 'processing', updated_at = ?,
           attempt_count = attempt_count + 1 WHERE id = ?""",
        (_now(), row["id"]),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM book_job WHERE id = ?", (row["id"],)).fetchone()
    return _bookjob_from_row(row) if row else None


def mark_book_job_done(
    conn: sqlite3.Connection, job_id: int, output_path: str
) -> None:
    conn.execute(
        """UPDATE book_job SET status = 'done', output_path = ?, error_message = NULL,
           updated_at = ? WHERE id = ?""",
        (output_path, _now(), job_id),
    )
    conn.commit()


def mark_book_job_failed(
    conn: sqlite3.Connection, job_id: int, error_message: str
) -> None:
    conn.execute(
        """UPDATE book_job SET status = 'failed', error_message = ?, updated_at = ?
           WHERE id = ?""",
        (error_message, _now(), job_id),
    )
    conn.commit()


def enqueue_book_job(
    conn: sqlite3.Connection, book_id: int, job_type: str = "video"
) -> BookJob:
    """Idempotent: returns the existing (book_id, job_type) row if one exists in any status,
    else inserts a new 'pending' row. The UNIQUE(book_id, job_type) constraint guarantees
    no duplicates even under concurrent callers (the second insert would fail and the caller
    can re-read)."""
    existing = get_book_job(conn, book_id, job_type)
    if existing is not None:
        return existing
    now = _now()
    try:
        cur = conn.execute(
            """INSERT INTO book_job (book_id, job_type, status, attempt_count,
                                     error_message, output_path, created_at, updated_at)
               VALUES (?, ?, 'pending', 0, NULL, NULL, ?, ?)""",
            (book_id, job_type, now, now),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM book_job WHERE id = ?", (cur.lastrowid,)).fetchone()
        return _bookjob_from_row(row)
    except sqlite3.IntegrityError:
        # Another caller inserted the same (book_id, job_type) between our SELECT and INSERT.
        # Re-read and return the existing row.
        existing = get_book_job(conn, book_id, job_type)
        assert existing is not None
        return existing


def delete_book_job(conn: sqlite3.Connection, book_id: int, job_type: str) -> bool:
    cur = conn.execute(
        "DELETE FROM book_job WHERE book_id = ? AND job_type = ?", (book_id, job_type)
    )
    conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# App state (pause flag, etc.)
# ---------------------------------------------------------------------------


def get_app_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_app_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        """INSERT INTO app_state (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, value),
    )
    conn.commit()


def is_queue_paused(conn: sqlite3.Connection) -> bool:
    return get_app_state(conn, "queue.paused") == "1"


# ---------------------------------------------------------------------------
# Queue statistics and operator helpers
# ---------------------------------------------------------------------------


def get_queue_stats(conn: sqlite3.Connection) -> dict:
    """Aggregate counts for /queue/stats. One read-only pass over patch + book_job."""
    patch_counts = {s: 0 for s in ("pending", "processing", "done", "failed")}
    for row in conn.execute("SELECT status, COUNT(*) AS c FROM patch GROUP BY status"):
        if row["status"] in patch_counts:
            patch_counts[row["status"]] = row["c"]

    resume_rows = conn.execute(
        """SELECT id, book_id, chunk_count, next_chunk_index
             FROM patch
            WHERE status IN ('pending', 'processing')
              AND next_chunk_index > 0 AND chunk_count > 0
            ORDER BY (next_chunk_index * 1.0 / NULLIF(chunk_count, 0)) DESC, id
            LIMIT 10"""
    ).fetchall()
    resume_candidates = [
        {
            "patch_id": r["id"],
            "book_id": r["book_id"],
            "chunk_count": r["chunk_count"],
            "next_chunk_index": r["next_chunk_index"],
            "remaining": max(0, r["chunk_count"] - r["next_chunk_index"]),
        }
        for r in resume_rows
    ]

    bj_counts = {s: 0 for s in ("pending", "processing", "done", "failed")}
    for row in conn.execute("SELECT status, COUNT(*) AS c FROM book_job GROUP BY status"):
        if row["status"] in bj_counts:
            bj_counts[row["status"]] = row["c"]

    oldest = conn.execute(
        "SELECT MIN(updated_at) AS m FROM patch WHERE status = 'pending'"
    ).fetchone()["m"]
    oldest_age_seconds = 0.0
    if oldest is not None:
        try:
            oldest_dt = datetime.fromisoformat(oldest)
            oldest_age_seconds = max(
                0.0, (datetime.now(timezone.utc) - oldest_dt).total_seconds()
            )
        except ValueError:
            oldest_age_seconds = 0.0

    last_errors: list[dict] = []
    rows = conn.execute(
        """SELECT * FROM (
               SELECT 'patch' AS entity, id, book_id, error_message, updated_at
                 FROM patch
                WHERE status = 'failed' AND error_message IS NOT NULL
               UNION ALL
               SELECT 'book_job' AS entity, id, book_id, error_message, updated_at
                 FROM book_job
                WHERE status = 'failed' AND error_message IS NOT NULL
           ) ORDER BY updated_at DESC LIMIT 5"""
    ).fetchall()
    for row in rows:
        last_errors.append(
            {
                "entity": row["entity"],
                "id": row["id"],
                "book_id": row["book_id"],
                "error_message": row["error_message"],
                "updated_at": row["updated_at"],
            }
        )

    return {
        "patch": patch_counts,
        "book_job": bj_counts,
        "oldest_pending_patch_age_seconds": oldest_age_seconds,
        "last_errors": last_errors,
        "resume_candidates": resume_candidates,
    }


def get_last_error_for_book(conn: sqlite3.Connection, book_id: int) -> str | None:
    """Return the most recent error_message from any failed patch or book_job for this book."""
    rows = conn.execute(
        """SELECT * FROM (
               SELECT 'patch' AS entity, id, error_message, updated_at
                 FROM patch
                WHERE book_id = ? AND status = 'failed' AND error_message IS NOT NULL
               UNION ALL
               SELECT 'book_job' AS entity, id, error_message, updated_at
                 FROM book_job
                WHERE book_id = ? AND status = 'failed' AND error_message IS NOT NULL
           ) ORDER BY updated_at DESC LIMIT 1""",
        (book_id, book_id),
    ).fetchall()
    return rows[0]["error_message"] if rows else None


def retry_all_failed_patches_for_book(conn: sqlite3.Connection, book_id: int) -> int:
    """Reset every failed patch of a book to pending. Skips patches currently 'processing'.
    Also clears the book's stale final outputs (consistent with reset_patch)."""
    now = _now()
    failed_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM patch WHERE book_id = ? AND status = 'failed'",
            (book_id,),
        ).fetchall()
    ]
    cur = conn.execute(
        """UPDATE patch SET status = 'pending', audio_path = NULL, error_message = NULL,
           next_chunk_index = 0, updated_at = ? WHERE book_id = ? AND status = 'failed'""",
        (now, book_id),
    )
    for pid in failed_ids:
        _delete_chunk_dir(book_id, pid)
    if cur.rowcount > 0:
        conn.execute(
            """UPDATE book SET final_audio_path = NULL, final_video_path = NULL,
               status = 'processing', updated_at = ? WHERE id = ?""",
            (now, book_id),
        )
    conn.commit()
    return cur.rowcount


def count_pending_patches_for_book(conn: sqlite3.Connection, book_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM patch WHERE book_id = ? AND status = 'pending'",
        (book_id,),
    ).fetchone()
    return row["c"]


def get_stuck_processing_book_jobs(conn: sqlite3.Connection) -> list[BookJob]:
    """Mirror of requeue_stuck_processing for the book_job table. Returns rows that
    were left in 'processing' from a previous crashed run; the caller is expected to
    reset them to 'pending'."""
    rows = conn.execute(
        "SELECT * FROM book_job WHERE status = 'processing' ORDER BY id"
    ).fetchall()
    return [_bookjob_from_row(r) for r in rows]


def requeue_stuck_book_jobs(conn: sqlite3.Connection) -> int:
    """Call once at startup: any book_job left 'processing' means the previous run
    crashed mid-job."""
    cur = conn.execute(
        """UPDATE book_job SET status = 'pending',
           error_message = COALESCE(error_message, 'requeued after restart'),
           updated_at = ? WHERE status = 'processing'""",
        (_now(),),
    )
    conn.commit()
    return cur.rowcount


def backfill_video_book_jobs(conn: sqlite3.Connection) -> int:
    """One-shot at startup: for each book with status='done', non-NULL final_audio_path,
    and (background_image_path OR at least one patch with image_path), and no existing
    book_job of type='video', insert a 'pending' book_job. Returns the count inserted."""
    rows = conn.execute(
        """SELECT b.id FROM book b
            WHERE b.status = 'done'
              AND b.final_audio_path IS NOT NULL
              AND (
                  b.background_image_path IS NOT NULL
                  OR EXISTS (
                      SELECT 1 FROM patch p
                       WHERE p.book_id = b.id AND p.image_path IS NOT NULL
                  )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM book_job bj
                   WHERE bj.book_id = b.id AND bj.job_type = 'video'
              )"""
    ).fetchall()
    now = _now()
    n = 0
    for row in rows:
        try:
            conn.execute(
                """INSERT INTO book_job (book_id, job_type, status, attempt_count,
                                         error_message, output_path, created_at, updated_at)
                   VALUES (?, 'video', 'pending', 0, NULL, NULL, ?, ?)""",
                (row["id"], now, now),
            )
            n += 1
        except sqlite3.IntegrityError:
            continue
    conn.commit()
    return n


# ---------------------------------------------------------------------------
# Dev-mode bulk reset
# ---------------------------------------------------------------------------


def reset_all_jobs(conn: sqlite3.Connection) -> dict:
    """Nuke every patch and book_job back to pending, reset every book to 'ready',
    and delete any produced audio/video files from disk. Returns what was touched.

    Skips 'processing' rows (those shouldn't exist at startup — they were already
    requeued by requeue_stuck_processing earlier in the lifespan — but we guard
    anyway so this is safe to call from a running server too).
    """
    now = _now()

    # Collect paths before we overwrite the columns.
    audio_rows = conn.execute(
        "SELECT audio_path FROM patch WHERE audio_path IS NOT NULL"
    ).fetchall()
    video_rows = conn.execute(
        "SELECT output_path FROM book_job WHERE output_path IS NOT NULL"
    ).fetchall()
    chunk_rows = conn.execute("SELECT book_id, id FROM patch").fetchall()
    paths_to_delete = [r["audio_path"] for r in audio_rows] + [
        r["output_path"] for r in video_rows
    ]

    cur_p = conn.execute(
        """UPDATE patch SET status = 'pending', audio_path = NULL, error_message = NULL,
           next_chunk_index = 0, updated_at = ? WHERE status != 'processing'""",
        (now,),
    )
    cur_bj = conn.execute(
        """UPDATE book_job SET status = 'pending', error_message = NULL, output_path = NULL,
           updated_at = ? WHERE status != 'processing'""",
        (now,),
    )
    cur_book = conn.execute(
        """UPDATE book SET final_audio_path = NULL, final_video_path = NULL,
           status = 'ready', updated_at = ? WHERE status != 'parsing'""",
        (now,),
    )

    conn.commit()

    for path in paths_to_delete:
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass

    for row in chunk_rows:
        _delete_chunk_dir(row["book_id"], row["id"])

    return {
        "patches_reset": cur_p.rowcount,
        "book_jobs_reset": cur_bj.rowcount,
        "books_reset": cur_book.rowcount,
        "files_deleted": len(paths_to_delete),
    }


# ---------------------------------------------------------------------------
# Patch export (Google Drive / Colab / Kaggle round trip)
# ---------------------------------------------------------------------------


def _patch_export_from_row(row: sqlite3.Row) -> PatchExport:
    return PatchExport(**{k: row[k] for k in row.keys()})


def create_patch_export(
    conn: sqlite3.Connection,
    patch_id: int,
    drive_folder_id: str,
    drive_folder_link: str,
    exported_chunk_count: int,
) -> PatchExport:
    now = _now()
    cur = conn.execute(
        """INSERT INTO patch_export (patch_id, drive_folder_id, drive_folder_link, status,
                                      exported_chunk_count, imported_chunk_count, created_at, updated_at)
           VALUES (?, ?, ?, 'exported', ?, 0, ?, ?)""",
        (patch_id, drive_folder_id, drive_folder_link, exported_chunk_count, now, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM patch_export WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _patch_export_from_row(row)


def list_patch_exports(conn: sqlite3.Connection, patch_id: int) -> list[PatchExport]:
    rows = conn.execute(
        "SELECT * FROM patch_export WHERE patch_id = ? ORDER BY id DESC", (patch_id,)
    ).fetchall()
    return [_patch_export_from_row(r) for r in rows]


def get_latest_patch_export(conn: sqlite3.Connection, patch_id: int) -> PatchExport | None:
    row = conn.execute(
        "SELECT * FROM patch_export WHERE patch_id = ? ORDER BY id DESC LIMIT 1", (patch_id,)
    ).fetchone()
    return _patch_export_from_row(row) if row else None


def update_patch_export(
    conn: sqlite3.Connection,
    export_id: int,
    *,
    status: str | None = None,
    imported_chunk_count: int | None = None,
    error_message: str | None = None,
) -> None:
    parts: list[str] = []
    params: list = []
    if status is not None:
        parts.append("status = ?")
        params.append(status)
    if imported_chunk_count is not None:
        parts.append("imported_chunk_count = ?")
        params.append(imported_chunk_count)
    if error_message is not None:
        parts.append("error_message = ?")
        params.append(error_message)
    if not parts:
        return
    parts.append("updated_at = ?")
    params.append(_now())
    params.append(export_id)
    conn.execute(f"UPDATE patch_export SET {', '.join(parts)} WHERE id = ?", params)
    conn.commit()


def list_all_patch_exports(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """For the /drive settings page: export history across every book, newest first."""
    rows = conn.execute(
        """SELECT pe.*, p.patch_index, p.book_id, b.title AS book_title
             FROM patch_export pe
             JOIN patch p ON p.id = pe.patch_id
             JOIN book b ON b.id = p.book_id
            ORDER BY pe.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]
