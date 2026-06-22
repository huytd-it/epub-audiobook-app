"""CRUD operations for book/chapter/patch, plus combined DB+filesystem operations."""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from app.chunker import group_into_patches
from app.epub_parser import ParsedChapter
from app.models import Book, Chapter, Patch, TextReplaceRule

ACTIVE_PATCH_STATUSES = {"pending", "done", "failed"}  # never 'processing' - that's worker-owned


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def reset_done_patches_for_book(conn: sqlite3.Connection, book_id: int) -> int:
    now = _now()
    cur = conn.execute(
        """UPDATE patch SET status = 'pending', audio_path = NULL, error_message = NULL,
           updated_at = ? WHERE book_id = ? AND status = 'done'""",
        (now, book_id),
    )
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
    conn.execute("DELETE FROM patch WHERE book_id = ?", (book_id,))
    now = _now()
    conn.executemany(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, status,
                               created_at, updated_at)
           VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
        [(book_id, idx, start, end, now, now) for idx, (start, end) in enumerate(ranges)],
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


def build_patch_text(conn: sqlite3.Connection, patch: Patch) -> str:
    """Return the full text for a patch: included chapter texts joined, with
    the book's replace rules applied."""
    chapters = get_chapters_in_range(
        conn, patch.book_id, patch.chapter_start, patch.chapter_end
    )
    included = [ch for ch in chapters if not ch.is_excluded]
    raw = "\n\n".join(ch.text for ch in included)
    rules = list_replace_rules(conn, patch.book_id)
    return apply_replace_rules(raw, rules)
