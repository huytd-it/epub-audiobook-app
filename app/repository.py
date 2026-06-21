"""CRUD operations for book/chapter/patch, plus combined DB+filesystem operations."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.chunker import group_into_patches
from app.epub_parser import ParsedChapter
from app.models import Book, Chapter, Patch

ACTIVE_PATCH_STATUSES = {"pending", "done", "failed"}  # never 'processing' - that's worker-owned


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _book_from_row(row: sqlite3.Row) -> Book:
    return Book(**{k: row[k] for k in row.keys()})


def _chapter_from_row(row: sqlite3.Row) -> Chapter:
    return Chapter(**{k: row[k] for k in row.keys()})


def _patch_from_row(row: sqlite3.Row) -> Patch:
    return Patch(**{k: row[k] for k in row.keys()})


def create_book(
    conn: sqlite3.Connection,
    *,
    title: str,
    original_filename: str,
    epub_path: str,
    patch_size: int,
    chapters: list[ParsedChapter],
    background_image_path: str | None,
) -> Book:
    now = _now()
    cur = conn.execute(
        """INSERT INTO book (title, original_filename, epub_path, patch_size, status,
                              background_image_path, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'ready', ?, ?, ?)""",
        (title, original_filename, epub_path, patch_size, background_image_path, now, now),
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

    patch_ranges = group_into_patches(len(chapters), patch_size)
    conn.executemany(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                               created_at, updated_at)
           VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
        [
            (book_id, idx, start, end, now, now)
            for idx, (start, end) in enumerate(patch_ranges)
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


def mark_patch_failed(conn: sqlite3.Connection, patch_id: int, error_message: str) -> None:
    conn.execute(
        "UPDATE patch SET status = 'failed', error_message = ?, updated_at = ? WHERE id = ?",
        (error_message, _now(), patch_id),
    )
    conn.commit()


def requeue_stuck_processing(conn: sqlite3.Connection) -> int:
    """Call once at startup: any patch left 'processing' means the previous run crashed mid-job."""
    cur = conn.execute(
        """UPDATE patch SET status = 'pending', error_message = 'requeued after restart', updated_at = ?
           WHERE status = 'processing'""",
        (_now(),),
    )
    conn.commit()
    return cur.rowcount


def reset_patch(conn: sqlite3.Connection, patch_id: int) -> bool:
    """Reset a patch back to pending (used for both 'regenerate' and 'delete output' UI actions -
    deleting the row outright would break chapter-range bookkeeping). Deletes its wav file and
    invalidates the book's stale final outputs. Refuses if the patch is currently 'processing'."""
    patch = get_patch(conn, patch_id)
    if patch is None or patch.status not in ACTIVE_PATCH_STATUSES:
        return False

    if patch.audio_path:
        Path(patch.audio_path).unlink(missing_ok=True)

    conn.execute(
        """UPDATE patch SET status = 'pending', audio_path = NULL, error_message = NULL, updated_at = ?
           WHERE id = ?""",
        (_now(), patch_id),
    )
    conn.execute(
        """UPDATE book SET final_audio_path = NULL, final_video_path = NULL, status = 'processing', updated_at = ?
           WHERE id = ?""",
        (_now(), patch.book_id),
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
    """Delete a book's DB rows (cascades to chapter/patch) and its files on disk.
    Refuses if any of its patches is currently 'processing'."""
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM patch WHERE book_id = ? AND status = 'processing'", (book_id,)
    ).fetchone()
    if row["c"] > 0:
        return False

    book = get_book(conn, book_id)
    if book is None:
        return False

    conn.execute("DELETE FROM book WHERE id = ?", (book_id,))
    conn.commit()

    Path(book.epub_path).unlink(missing_ok=True)
    book_dir = Path(data_root) / "books" / str(book_id)
    if book_dir.exists():
        import shutil

        shutil.rmtree(book_dir, ignore_errors=True)
    return True
