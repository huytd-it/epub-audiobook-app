"""CRUD operations for book/chapter/patch, plus combined DB+filesystem operations."""
from __future__ import annotations

import logging
import math
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from app.audio_merge import cleanup_chunk_dir
from app.chunker import group_into_patches, split_into_tts_chunks
from app.epub_parser import ParsedChapter
from app.models import Book, BookJob, Chapter, Music, Patch, PatchExport, TextReplaceRule
from app.normalization import NormalizationOptions, clean_junk_tokens, normalize_chapter_titles, normalize_text, remove_cjk
from app.production_defaults import get_effective_normalization_options
from app.text_analysis import text_hash
from app.validation import canonical_chapter_title, detect_chapter_number, split_chapter_title
from app.youtube_metadata import format_chapter_range, resolve_patch_chapter_range

logger = logging.getLogger(__name__)

ACTIVE_PATCH_STATUSES = {"pending", "done", "failed"}  # never 'processing' - that's worker-owned
_TTS_MAX_CHARS = 400  # default matches config.settings.tts_max_chars


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _chunk_dir_for(book_id: int, patch_id: int) -> Path:
    # NOTE: Legacy. Use new helper get_patch_chunk_dir(book_id, patch_index)
    return Path("data") / "books" / str(book_id) / "patches" / f"{patch_id}_chunks"


def get_patch_audio_path(book_id: int, patch_index: int) -> Path:
    episode = f"{patch_index + 1:03d}"
    return Path("data") / "books" / str(book_id) / "audio" / f"{book_id}_{episode}.wav"


def get_patch_chunk_dir(book_id: int, patch_index: int) -> Path:
    episode = f"{patch_index + 1:03d}"
    return Path("data") / "books" / str(book_id) / "audio" / f"{book_id}_{episode}_chunks"


def get_backup_path(book_id: int, patch_index: int, extension: str, timestamp: str) -> Path:
    episode = f"{patch_index + 1:03d}"
    return Path("data") / "books" / str(book_id) / "backup_audio" / f"{book_id}_{episode}_{timestamp}{extension}"


def backup_patch_audio_files(book_id: int, patch_index: int, old_audio_path: str) -> None:
    old_path = Path(old_audio_path)
    if not old_path.exists():
        return

    timestamp = datetime.now().strftime("%Y_%m_%d_%H%M%S")
    backup_dir = Path("data") / "books" / str(book_id) / "backup_audio"
    backup_dir.mkdir(parents=True, exist_ok=True)

    for ext in (".wav", ".timeline.json", ".ass"):
        src = old_path.with_suffix(ext)
        if src.exists():
            dst = get_backup_path(book_id, patch_index, ext, timestamp)
            shutil.copy2(src, dst)  # Use copy2 to keep metadata


def backup_all_book_audio(book_id: int) -> None:
    """Backup every wav+sidecar under data/books/{book_id}/audio/ into backup_audio/.
    Used by rebuild_patches and reset_all_jobs, which wipe the whole audio folder."""
    audio_dir = Path("data") / "books" / str(book_id) / "audio"
    if not audio_dir.exists():
        return
    backup_dir = Path("data") / "books" / str(book_id) / "backup_audio"
    backup_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y_%m_%d_%H%M%S")
    for src in audio_dir.glob("*.wav"):
        for ext in (".wav", ".timeline.json", ".ass"):
            side = src.with_suffix(ext)
            if side.exists():
                shutil.copy2(side, backup_dir / f"{side.stem}_{timestamp}{side.suffix}")


def _delete_chunk_dir(book_id: int, patch_index: int, patch_id: int) -> None:
    # Handle both new and legacy locations.
    cleanup_chunk_dir(str(get_patch_chunk_dir(book_id, patch_index)))
    cleanup_chunk_dir(str(_chunk_dir_for(book_id, patch_id)))


def delete_patch_audio_files(audio_path: str | None) -> None:
    if not audio_path:
        return
    path = Path(audio_path)
    # Also delete sidecars (.timeline.json, .ass)
    for ext in (".wav", ".timeline.json", ".ass"):
        try:
            path.with_suffix(ext).unlink(missing_ok=True)
        except OSError:
            pass


def _update_status(conn, table, id, *, status=None, **extra):
    sets = []
    values = []
    if status is not None:
        sets.append("status = ?")
        values.append(status)
    for k, v in extra.items():
        sets.append(f"{k} = ?")
        values.append(v)
    sets.append("updated_at = ?")
    values.append(_now())
    values.append(id)
    conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = ?", values)
    conn.commit()


def _claim_next(conn, table, order_col="id"):
    now = _now()
    row = conn.execute(
        f"UPDATE {table} SET status='processing', attempt_count=attempt_count+1, updated_at=? "
        f"WHERE id=(SELECT id FROM {table} WHERE status='pending' ORDER BY {order_col} LIMIT 1) "
        f"AND status='pending' RETURNING *",
        (now,),
    ).fetchone()
    conn.commit()
    return row


def _update_patch_chunk(conn, patch_id, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    values = list(cols.values()) + [_now(), patch_id]
    conn.execute(f"UPDATE patch SET {sets}, updated_at=? WHERE id=?", values)
    conn.commit()


def _from_row(Model, row):
    return Model(**{k: row[k] for k in row.keys()})


def _book_from_row(row): return _from_row(Book, row)


def _chapter_from_row(row):
    d = {k: row[k] for k in row.keys()}
    d["is_excluded"] = bool(d.get("is_excluded", False))
    return Chapter(**d)


def _patch_from_row(row): return _from_row(Patch, row)


def _rule_from_row(row):
    d = {k: row[k] for k in row.keys()}
    d["is_regex"] = bool(d["is_regex"])
    return TextReplaceRule(**d)


def _bookjob_from_row(row): return _from_row(BookJob, row)


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
        """INSERT INTO chapter (book_id, chapter_index, title, text, char_count, chapter_no, text_hash)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                book_id, idx, ch.title, ch.text, ch.char_count,
                detect_chapter_number(ch.title), text_hash(ch.text),
            )
            for idx, ch in enumerate(chapters)
        ],
    )

    conn.commit()
    return get_book(conn, book_id)


def get_book(conn: sqlite3.Connection, book_id: int) -> Book | None:
    row = conn.execute("SELECT * FROM book WHERE id = ?", (book_id,)).fetchone()
    return _book_from_row(row) if row else None


def list_books(conn: sqlite3.Connection, page: int = 1, per_page: int = 20) -> tuple[list[Book], int, int]:
    offset = (page - 1) * per_page
    rows = conn.execute("SELECT * FROM book ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset)).fetchall()
    count_row = conn.execute("SELECT COUNT(*) AS c FROM book").fetchone()
    total = count_row["c"]
    total_pages = max(1, math.ceil(total / per_page))
    return [_book_from_row(r) for r in rows], total, total_pages


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


def get_chapter(conn: sqlite3.Connection, book_id: int, chapter_index: int) -> Chapter | None:
    row = conn.execute(
        "SELECT * FROM chapter WHERE book_id = ? AND chapter_index = ?",
        (book_id, chapter_index),
    ).fetchone()
    return _chapter_from_row(row) if row else None


def update_chapter(
    conn: sqlite3.Connection,
    book_id: int,
    chapter_index: int,
    *,
    title: str | None = None,
    text: str | None = None,
    is_excluded: bool | None = None,
) -> bool:
    """Write only the fields that were passed, recomputing every derived column so the
    row stays consistent with what ``diff_chapters_against_epub`` and the numbering
    checks expect. ``None`` means "field not supplied" — an omitted ``is_excluded``
    never clobbers the existing flag.
    """
    sets: list[str] = []
    values: list = []
    if title is not None:
        sets.append("title = ?")
        values.append(title)
        sets.append("chapter_no = ?")
        values.append(detect_chapter_number(title))
    if text is not None:
        sets.append("text = ?")
        values.append(text)
        sets.append("char_count = ?")
        values.append(len(text))
        sets.append("text_hash = ?")
        values.append(text_hash(text))
    if is_excluded is not None:
        sets.append("is_excluded = ?")
        values.append(1 if is_excluded else 0)

    if not sets:
        return get_chapter(conn, book_id, chapter_index) is not None

    values.extend([book_id, chapter_index])
    cur = conn.execute(
        f"UPDATE chapter SET {', '.join(sets)} WHERE book_id = ? AND chapter_index = ?",
        values,
    )
    conn.commit()
    return cur.rowcount > 0


def set_chapter_titles(conn: sqlite3.Connection, book_id: int, updates: list[tuple[int, str]]) -> int:
    """Bulk title rewrite for the "chuẩn hoá tiêu đề" action. ``updates`` is a list of
    (chapter_index, new_title) pairs; chapter_no is recomputed for each."""
    if not updates:
        return 0
    rows = [
        (title, detect_chapter_number(title), book_id, chapter_index)
        for chapter_index, title in updates
    ]
    cur = conn.executemany(
        "UPDATE chapter SET title = ?, chapter_no = ? WHERE book_id = ? AND chapter_index = ?",
        rows,
    )
    conn.commit()
    return cur.rowcount if cur.rowcount is not None else len(updates)


def chapter_no_range(
    conn: sqlite3.Connection, book_id: int, chapter_start: int, chapter_end: int
) -> tuple[int | None, int | None]:
    """Số chương nhỏ nhất/lớn nhất đọc được từ tiêu đề trong khoảng chỉ số này.

    Trả về (None, None) khi không chương nào trong khoảng có số — patch vẫn hợp lệ,
    chỉ là không neo được theo số chương.
    """
    row = conn.execute(
        """SELECT MIN(chapter_no) AS lo, MAX(chapter_no) AS hi FROM chapter
           WHERE book_id = ? AND chapter_index BETWEEN ? AND ? AND chapter_no IS NOT NULL""",
        (book_id, chapter_start, chapter_end),
    ).fetchone()
    return (row["lo"], row["hi"]) if row else (None, None)


def backfill_patch_chapter_numbers(conn: sqlite3.Connection, book_id: int) -> int:
    """Điền chapter_no_start/chapter_no_end cho patch tạo trước khi có hai cột này."""
    rows = conn.execute(
        """SELECT id, chapter_start, chapter_end FROM patch
           WHERE book_id = ? AND (chapter_no_start IS NULL OR chapter_no_end IS NULL)""",
        (book_id,),
    ).fetchall()
    updates = []
    for row in rows:
        lo, hi = chapter_no_range(conn, book_id, row["chapter_start"], row["chapter_end"])
        if lo is not None or hi is not None:
            updates.append((lo, hi, row["id"]))
    if updates:
        conn.executemany(
            "UPDATE patch SET chapter_no_start = ?, chapter_no_end = ? WHERE id = ?", updates
        )
        conn.commit()
    return len(updates)


def resync_patch_ranges_from_chapter_numbers(conn: sqlite3.Connection, book_id: int) -> list[dict]:
    """Căn lại chapter_start/chapter_end của từng patch theo khoảng số chương đã lưu.

    Đây là cách sửa "lệch" sau khi re-import EPUB: chỉ số chương xê dịch khi có chương
    mới chèn vào, nhưng số chương trong tiêu đề thì không đổi — nên số chương mới là
    thứ dùng để tìm lại đúng vùng chỉ số của patch. Patch đang tổng hợp dở
    (``processing``) được bỏ qua vì worker đang bám theo chỉ số hiện tại.
    """
    numbers_by_index: dict[int, int] = {}
    first_index: dict[int, int] = {}
    last_index: dict[int, int] = {}
    for row in conn.execute(
        """SELECT chapter_index, chapter_no FROM chapter
           WHERE book_id = ? AND chapter_no IS NOT NULL ORDER BY chapter_index""",
        (book_id,),
    ):
        numbers_by_index[row["chapter_index"]] = row["chapter_no"]
        first_index.setdefault(row["chapter_no"], row["chapter_index"])
        last_index[row["chapter_no"]] = row["chapter_index"]

    changes: list[dict] = []
    for patch in list_patches(conn, book_id):
        if patch.status == "processing":
            continue
        if patch.chapter_no_start is None or patch.chapter_no_end is None:
            continue

        # Chỉ đụng vào patch thực sự lệch. Một patch mà số chương đã neo vẫn khớp với
        # số chương đang nằm trong khoảng chỉ số của nó thì không có gì để sửa — căn
        # lại lúc đó chỉ tổ cắt mất các chương không đánh số ở hai đầu patch.
        in_range = [
            numbers_by_index[index]
            for index in range(patch.chapter_start, patch.chapter_end + 1)
            if index in numbers_by_index
        ]
        if in_range and (min(in_range), max(in_range)) == (patch.chapter_no_start, patch.chapter_no_end):
            continue

        start = first_index.get(patch.chapter_no_start)
        end = last_index.get(patch.chapter_no_end)
        if start is None or end is None or start > end:
            continue
        if start == patch.chapter_start and end == patch.chapter_end:
            continue
        conn.execute(
            "UPDATE patch SET chapter_start = ?, chapter_end = ?, updated_at = ? WHERE id = ?",
            (start, end, _now(), patch.id),
        )
        changes.append(
            {
                "patch_id": patch.id,
                "patch_index": patch.patch_index,
                "chapter_no_start": patch.chapter_no_start,
                "chapter_no_end": patch.chapter_no_end,
                "old_range": [patch.chapter_start, patch.chapter_end],
                "new_range": [start, end],
            }
        )
    if changes:
        conn.commit()
    return changes


def list_patches_covering_chapter(conn: sqlite3.Connection, book_id: int, chapter_index: int) -> list[Patch]:
    rows = conn.execute(
        """SELECT * FROM patch WHERE book_id = ? AND ? BETWEEN chapter_start AND chapter_end
           ORDER BY patch_index""",
        (book_id, chapter_index),
    ).fetchall()
    return [_patch_from_row(r) for r in rows]


def list_patches(conn: sqlite3.Connection, book_id: int) -> list[Patch]:
    rows = conn.execute(
        "SELECT * FROM patch WHERE book_id = ? ORDER BY patch_index", (book_id,)
    ).fetchall()
    return [_patch_from_row(r) for r in rows]


def get_patch(conn: sqlite3.Connection, patch_id: int) -> Patch | None:
    row = conn.execute("SELECT * FROM patch WHERE id = ?", (patch_id,)).fetchone()
    return _patch_from_row(row) if row else None


def claim_next_pending_patch(conn: sqlite3.Connection) -> Patch | None:
    row = _claim_next(conn, "patch", "patch_index")
    return _patch_from_row(row) if row else None


def mark_patch_done(conn: sqlite3.Connection, patch_id: int, audio_path: str) -> None:
    _update_status(conn, "patch", patch_id, status="done", audio_path=audio_path)


def update_patch_chunk_count(conn: sqlite3.Connection, patch_id: int, chunk_count: int) -> None:
    _update_patch_chunk(conn, patch_id, chunk_count=chunk_count, chunk_count_exact=1)


def list_stale_chunk_count_patch_ids(conn: sqlite3.Connection, book_id: int) -> list[int]:
    """Patch ids whose stored chunk_count is still the fast estimate (not yet the
    real split), skipping any currently processing. Ordered by patch_index."""
    rows = conn.execute(
        """SELECT id FROM patch
           WHERE book_id = ? AND chunk_count_exact = 0 AND status != 'processing'
           ORDER BY patch_index""",
        (book_id,),
    ).fetchall()
    return [r["id"] for r in rows]


def update_patch_chunk_progress(
    conn: sqlite3.Connection, patch_id: int, next_chunk_index: int
) -> None:
    _update_patch_chunk(conn, patch_id, next_chunk_index=next_chunk_index)


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
        "UPDATE patch SET max_chars = ?, chunk_count = ?, chunk_count_exact = 0, updated_at = ? WHERE id = ?",
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
    plan = build_patch_chunk_plan(conn, patch)

    current_index = None
    if worker is not None and getattr(worker, "current_patch_id", None) == patch.id:
        current_index = worker.current_chunk_index

    result = []
    for i, item in enumerate(plan):
        chunk_text = item["text"]
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
    _update_status(conn, "patch", patch_id, status="failed", error_message=error_message)


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
        backup_patch_audio_files(patch.book_id, patch.patch_index, patch.audio_path)
        delete_patch_audio_files(patch.audio_path)

    video_dir = Path("data") / "books" / str(patch.book_id) / "patch_videos"
    video_file = video_dir / f"{patch_id}.mp4"
    if video_file.exists():
        video_file.unlink(missing_ok=True)

    _delete_chunk_dir(patch.book_id, patch.patch_index, patch.id)

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
    """Delete a single patch and clean up its files.
    Refuses if the patch is currently 'processing'.

    The surviving patches keep their patch_index, leaving a gap: the index is the
    episode number in the YouTube metadata and in the export filenames, so
    renumbering would relabel episodes that are already rendered or published.
    A full rebuild_patches() call is what renumbers from 0."""
    patch = get_patch(conn, patch_id)
    if patch is None or patch.status == "processing":
        return False

    book_id = patch.book_id

    if patch.audio_path:
        backup_patch_audio_files(patch.book_id, patch.patch_index, patch.audio_path)
        delete_patch_audio_files(patch.audio_path)
    if patch.image_path:
        Path(patch.image_path).unlink(missing_ok=True)
    video_dir = Path("data") / "books" / str(book_id) / "patch_videos"
    video_file = video_dir / f"{patch_id}.mp4"
    if video_file.exists():
        video_file.unlink(missing_ok=True)

    _delete_chunk_dir(book_id, patch.patch_index, patch.id)

    conn.execute("DELETE FROM patch WHERE id = ?", (patch_id,))

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


def update_book_normalization(
    conn: sqlite3.Connection,
    book_id: int,
    *,
    numbers: bool | None = None,
    junk: bool | None = None,
    spellcheck: bool | None = None,
    dictionary: bool | None = None,
    transliteration: bool | None = None,
    abbreviations: bool | None = None,
    breaks: bool | None = None,
) -> Book | None:
    """Update one or more TTS normalization toggles for a book."""
    book = get_book(conn, book_id)
    if book is None:
        return None
    fields = []
    params = []
    if numbers is not None:
        fields.append("normalize_numbers_enabled = ?")
        params.append(1 if numbers else 0)
    if junk is not None:
        fields.append("normalize_junk_enabled = ?")
        params.append(1 if junk else 0)
    if spellcheck is not None:
        fields.append("normalize_spellcheck_enabled = ?")
        params.append(1 if spellcheck else 0)
    if dictionary is not None:
        fields.append("normalize_dictionary_enabled = ?")
        params.append(1 if dictionary else 0)
    if transliteration is not None:
        fields.append("normalize_transliteration_enabled = ?")
        params.append(1 if transliteration else 0)
    if abbreviations is not None:
        fields.append("normalize_abbreviations_enabled = ?")
        params.append(1 if abbreviations else 0)
    if breaks is not None:
        fields.append("normalize_breaks_enabled = ?")
        params.append(1 if breaks else 0)
    if not fields:
        return book
    params.extend([_now(), book_id])
    conn.execute(
        f"UPDATE book SET {', '.join(fields)}, updated_at = ? WHERE id = ?",
        params,
    )
    conn.commit()
    return get_book(conn, book_id)


def set_book_final_audio(conn: sqlite3.Connection, book_id: int, final_audio_path: str) -> None:
    _update_status(conn, "book", book_id, status="done", final_audio_path=final_audio_path)


def set_book_final_video(conn: sqlite3.Connection, book_id: int, final_video_path: str) -> None:
    _update_status(conn, "book", book_id, final_video_path=final_video_path)


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


def update_book_automation_flags(
    conn: sqlite3.Connection,
    book_id: int,
    *,
    auto_create_video: bool | None = None,
    auto_upload_youtube: bool | None = None,
) -> None:
    """Update per-book automation flags (auto_create_video / auto_upload_youtube)."""
    parts = []
    params = []
    if auto_create_video is not None:
        parts.append("auto_create_video = ?")
        params.append(1 if auto_create_video else 0)
    if auto_upload_youtube is not None:
        parts.append("auto_upload_youtube = ?")
        params.append(1 if auto_upload_youtube else 0)
    if not parts:
        return
    parts.append("updated_at = ?")
    params.append(_now())
    params.append(book_id)
    conn.execute(f"UPDATE book SET {', '.join(parts)} WHERE id = ?", params)
    conn.commit()


def rename_book(conn: sqlite3.Connection, book_id: int, new_title: str) -> bool:
    cur = conn.execute(
        "UPDATE book SET title = ?, updated_at = ? WHERE id = ?",
        (new_title, _now(), book_id),
    )
    conn.commit()
    return cur.rowcount > 0


def reset_done_patches_for_book(conn: sqlite3.Connection, book_id: int) -> int:
    done_rows = [
        r for r in conn.execute(
            "SELECT id, book_id, patch_index, audio_path FROM patch WHERE book_id = ? AND status = 'done'",
            (book_id,),
        ).fetchall()
    ]
    now = _now()
    cur = conn.execute(
        """UPDATE patch SET status = 'pending', audio_path = NULL, error_message = NULL,
           next_chunk_index = 0, updated_at = ? WHERE book_id = ? AND status = 'done'""",
        (now, book_id),
    )
    for row in done_rows:
        if row["audio_path"]:
            backup_patch_audio_files(row["book_id"], row["patch_index"], row["audio_path"])
            delete_patch_audio_files(row["audio_path"])
        _delete_chunk_dir(row["book_id"], row["patch_index"], row["id"])
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
    skip_excluded_check: bool = False,
) -> list[Patch]:
    """Replace all patches for this book. Validates ranges, deletes old patches,
    inserts new ones. Resets book state.

    When *skip_excluded_check* is True the excluded-chapter validation is
    skipped — callers like ``auto_build_patches`` already filter excluded
    chapters from the ranges, so checking here would false-positive on gaps
    between ranges.
    """
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

    if not skip_excluded_check:
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
        # Backup everything in audio/ before wiping the book.
        backup_all_book_audio(book_id)
        # Wipe audio directory completely.
        audio_dir = Path("data") / "books" / str(book_id) / "audio"
        if audio_dir.exists():
            for f in audio_dir.glob("*"):
                f.unlink(missing_ok=True)
            # Remove empty dirs.
            for chunk_dir in audio_dir.glob("*_chunks"):
                cleanup_chunk_dir(str(chunk_dir))
        
        patterns = list_patches(conn, book_id)
        for p in patterns:
            # Need to clean up what's referenced in DB.
            if p.image_path:
                Path(p.image_path).unlink(missing_ok=True)
            _delete_chunk_dir(book_id, p.patch_index, p.id)
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
        no_start, no_end = chapter_no_range(conn, book_id, start, end)
        patch_rows.append((book_id, idx, start, end, no_start, no_end, name, chunk_count, now, now))
    conn.executemany(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end,
                               chapter_no_start, chapter_no_end, name,
                               chunk_count, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
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


def has_speakable_text(text: str) -> bool:
    """True when the text holds at least one letter or digit. Cloud TTS backends
    reject punctuation-only input (edge-tts raises NoAudioReceived)."""
    return re.search(r"[^\W_]", text, re.UNICODE) is not None


def prepare_chapter_tts_text(
    chapter: Chapter, opts: NormalizationOptions, rules: list[TextReplaceRule]
) -> str:
    """Build one chapter's speakable body; its TOC title is not narrated twice."""
    text = chapter.text
    title = chapter.title.strip()
    if title and text.startswith(title):
        text = text[len(title):].lstrip()
    text = normalize_chapter_titles(text)
    return apply_replace_rules(normalize_text(text, opts), rules)


def build_patch_text(conn: sqlite3.Connection, patch: Patch) -> str:
    """Return the full text for a patch: included chapter texts joined,
    normalized (if enabled), then with the book's replace rules applied."""
    chapters = get_chapters_in_range(
        conn, patch.book_id, patch.chapter_start, patch.chapter_end
    )
    included = [ch for ch in chapters if not ch.is_excluded]
    book = get_book(conn, patch.book_id)
    rules = list_replace_rules(conn, patch.book_id)
    opts = get_effective_normalization_options(conn, book) if book else NormalizationOptions()
    return "\n\n".join(prepare_chapter_tts_text(ch, opts, rules) for ch in included)


def fetch_patch_chunk_inputs(
    conn: sqlite3.Connection, patch: Patch, max_chars: int | None = None
) -> dict:
    """Read everything build_chunk_plan_from_inputs needs, and nothing else.

    Split out from build_patch_chunk_plan so callers that hold the shared db_lock can
    release it before the expensive part: normalization + chunking is pure CPU over the
    whole patch text, and running it under the lock stalls every other request.
    """
    limit = max_chars or patch.max_chars or _TTS_MAX_CHARS
    if patch.clean_text:
        return {"limit": limit, "clean_text": patch.clean_text}
    book = get_book(conn, patch.book_id)
    return {
        "limit": limit,
        "clean_text": None,
        "chapters": get_chapters_in_range(conn, patch.book_id, patch.chapter_start, patch.chapter_end),
        "rules": list_replace_rules(conn, patch.book_id),
        "opts": get_effective_normalization_options(conn, book) if book else NormalizationOptions(),
    }


def _normalize_chapter_title_for_plan(
    title: str | None, opts: NormalizationOptions, rules: list[TextReplaceRule], chapter_no: int | None = None
) -> str | None:
    """Title as it should appear in chunk plan / manifest / timeline: cleaned and
    with user replace rules applied, plus canonical ``Chương N: Tên`` shape when a
    number+name can be parsed. Keeps the number as digits (display form) and preserves
    the normalized name — this is the ``tên sau khi normalize`` the manifest was missing."""
    if title is None:
        return None
    raw = title.strip()
    if not raw:
        return f"Chương {chapter_no}" if chapter_no is not None else ""
    # Mirror normalize_text's early junk/CJK cleanup so a title like "OO@@ Chương 1"
    # or "Chương 1\u3000" does not propagate the junk into the manifest.
    cleaned = raw
    if opts.junk:
        cleaned = clean_junk_tokens(cleaned, opts.junk_extra_tokens)
    cleaned = remove_cjk(cleaned).strip()
    if not cleaned:
        cleaned = raw.strip()
    # User-defined replacements (the same ones applied to body text) must also
    # apply to the title so "AI" -> "trí tuệ nhân tạo" is reflected in chapter_titles.
    replaced = apply_replace_rules(cleaned, rules)
    # Canonicalize "Chương 12 - Bão" / "12: Bão" -> "Chương 12: Bão" so the name
    # after normalize is not lost to a dash/colon variation.
    number, name = split_chapter_title(replaced)
    if number is not None and name:
        return canonical_chapter_title(number, name)
    # If title was empty/no-name but we know the chapter number, keep the number form.
    if not replaced.strip() and chapter_no is not None:
        return f"Chương {chapter_no}"
    return replaced.strip()


def build_chunk_plan_from_inputs(inputs: dict) -> list[dict]:
    """Pure-CPU half of build_patch_chunk_plan: touches no database."""
    limit = inputs["limit"]
    if inputs["clean_text"]:
        return [
            {"text": chunk, "chapter_index": None, "chapter_title": None, "is_chapter_start": False}
            for chunk in split_into_tts_chunks(inputs["clean_text"], max_chars=limit)
            if has_speakable_text(chunk)
        ]
    chapters, rules, opts = inputs["chapters"], inputs["rules"], inputs["opts"]
    plan = []
    for chapter in chapters:
        if chapter.is_excluded:
            continue
        text = prepare_chapter_tts_text(chapter, opts, rules)
        if not has_speakable_text(text):
            continue
        chunks = [c for c in split_into_tts_chunks(text, max_chars=limit) if has_speakable_text(c)]
        normalized_title = _normalize_chapter_title_for_plan(chapter.title, opts, rules, getattr(chapter, "chapter_no", None))
        for i, chunk in enumerate(chunks):
            plan.append({
                "text": chunk,
                "chapter_index": chapter.chapter_index,
                "chapter_title": normalized_title,
                "is_chapter_start": i == 0,
            })
    return plan


def build_patch_chunk_plan(
    conn: sqlite3.Connection, patch: Patch, max_chars: int | None = None
) -> list[dict]:
    """Build independently split TTS chunks for each included chapter.

    A patch edited in Text Studio (``clean_text`` set) is the exception: the edited
    text wins over the derived chapter texts, so every TTS path — worker, LightTTS,
    Drive/Kaggle export — speaks exactly what the user saved. Free-form editing
    destroys the chapter boundaries, so that plan carries no chapter markers and
    therefore produces no chapter timeline.

    Callers holding db_lock should prefer fetch_patch_chunk_inputs +
    build_chunk_plan_from_inputs so the normalization pass runs off the lock.
    """
    return build_chunk_plan_from_inputs(fetch_patch_chunk_inputs(conn, patch, max_chars))


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
        no_start, no_end = chapter_no_range(conn, book_id, start, end)
        result.append({
            "patch_index": idx,
            "chapter_start": start,
            "chapter_end": end,
            "chapter_no_start": no_start,
            "chapter_no_end": no_end,
            "name": name,
            "chunk_count": chunk_count,
        })
    return result


def backfill_chapter_metadata(conn: sqlite3.Connection, book_id: int) -> int:
    """Fill chapter_no/text_hash for books imported before those columns existed."""
    rows = conn.execute(
        """SELECT id, title, text FROM chapter
           WHERE book_id = ? AND (text_hash IS NULL OR (chapter_no IS NULL AND title IS NOT NULL))""",
        (book_id,),
    ).fetchall()
    updates = [
        (detect_chapter_number(row["title"]), text_hash(row["text"] or ""), row["id"])
        for row in rows
    ]
    if updates:
        conn.executemany("UPDATE chapter SET chapter_no = ?, text_hash = ? WHERE id = ?", updates)
        conn.commit()
    return len(updates)


def diff_chapters_against_epub(
    conn: sqlite3.Connection, book_id: int, parsed: list[ParsedChapter]
) -> dict:
    """Compare a freshly parsed EPUB against the chapters already stored.

    Matching is by content hash first, then by detected chapter number — a chapter that
    only had a typo fixed still matches by number, so its patch (and its audio) survives.
    Returns a plan; nothing is written.
    """
    existing = list_chapters(conn, book_id)
    by_hash = {chapter.text_hash: chapter for chapter in existing if chapter.text_hash}
    by_number: dict[int, Chapter] = {}
    for chapter in existing:
        if chapter.chapter_no is not None:
            by_number.setdefault(chapter.chapter_no, chapter)

    matched: list[dict] = []
    changed: list[dict] = []
    added: list[dict] = []
    used_ids: set[int] = set()

    for parsed_index, parsed_chapter in enumerate(parsed):
        digest = text_hash(parsed_chapter.text)
        number = detect_chapter_number(parsed_chapter.title)
        current = by_hash.get(digest)
        if current is not None and current.id not in used_ids:
            used_ids.add(current.id)
            matched.append({"chapter_index": current.chapter_index, "title": current.title})
            continue

        current = by_number.get(number) if number is not None else None
        if current is not None and current.id not in used_ids:
            used_ids.add(current.id)
            changed.append(
                {
                    "parsed_index": parsed_index,
                    "chapter_index": current.chapter_index,
                    "chapter_no": number,
                    "title": parsed_chapter.title,
                    "old_char_count": current.char_count,
                    "new_char_count": parsed_chapter.char_count,
                }
            )
            continue

        added.append(
            {
                "parsed_index": parsed_index,
                "chapter_no": number,
                "title": parsed_chapter.title,
                "char_count": parsed_chapter.char_count,
            }
        )

    removed = [
        {"chapter_index": chapter.chapter_index, "title": chapter.title, "chapter_no": chapter.chapter_no}
        for chapter in existing
        if chapter.id not in used_ids
    ]

    return {
        "existing_count": len(existing),
        "parsed_count": len(parsed),
        "matched_count": len(matched),
        "changed": changed,
        "added": added,
        "removed": removed,
        "next_chapter_index": (max((c.chapter_index for c in existing), default=-1) + 1),
    }


def append_new_chapters(
    conn: sqlite3.Connection,
    book_id: int,
    parsed: list[ParsedChapter],
    *,
    update_changed: bool = False,
) -> dict:
    """Apply a re-import: append chapters the book does not have yet.

    Existing chapters keep their chapter_index, so every patch range — and therefore every
    audio file already produced — stays valid. Changed chapters are only rewritten when
    update_changed is set, and then only for chapters no completed patch depends on.
    """
    plan = diff_chapters_against_epub(conn, book_id, parsed)
    now = _now()
    next_index = plan["next_chapter_index"]

    updated = 0
    if update_changed and plan["changed"]:
        protected = _chapter_indices_with_done_audio(conn, book_id)
        for entry in plan["changed"]:
            if entry["chapter_index"] in protected:
                continue
            source = parsed[entry["parsed_index"]]
            conn.execute(
                """UPDATE chapter SET title = ?, text = ?, char_count = ?, chapter_no = ?, text_hash = ?
                   WHERE book_id = ? AND chapter_index = ?""",
                (
                    source.title, source.text, source.char_count,
                    entry["chapter_no"], text_hash(source.text),
                    book_id, entry["chapter_index"],
                ),
            )
            updated += 1

    inserted = 0
    for entry in plan["added"]:
        parsed_chapter = parsed[entry["parsed_index"]]
        conn.execute(
            """INSERT INTO chapter (book_id, chapter_index, title, text, char_count, chapter_no, text_hash)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                book_id, next_index, parsed_chapter.title, parsed_chapter.text,
                parsed_chapter.char_count, detect_chapter_number(parsed_chapter.title),
                text_hash(parsed_chapter.text),
            ),
        )
        next_index += 1
        inserted += 1

    if inserted or updated:
        conn.execute("UPDATE book SET updated_at = ? WHERE id = ?", (now, book_id))
        conn.commit()

    return {
        "inserted": inserted,
        "updated": updated,
        "skipped_changed": len(plan["changed"]) - updated,
        "removed_count": len(plan["removed"]),
        "first_new_chapter_index": plan["next_chapter_index"] if inserted else None,
    }


def _chapter_indices_with_done_audio(conn: sqlite3.Connection, book_id: int) -> set[int]:
    covered: set[int] = set()
    for row in conn.execute(
        "SELECT chapter_start, chapter_end FROM patch WHERE book_id = ? AND status = 'done'",
        (book_id,),
    ):
        covered.update(range(row["chapter_start"], row["chapter_end"] + 1))
    return covered


def uncovered_chapter_indices(conn: sqlite3.Connection, book_id: int) -> list[int]:
    """Included chapters that no patch range covers yet."""
    covered: set[int] = set()
    for row in conn.execute(
        "SELECT chapter_start, chapter_end FROM patch WHERE book_id = ?", (book_id,)
    ):
        covered.update(range(row["chapter_start"], row["chapter_end"] + 1))
    rows = conn.execute(
        """SELECT chapter_index FROM chapter
           WHERE book_id = ? AND is_excluded = 0 ORDER BY chapter_index""",
        (book_id,),
    ).fetchall()
    return [row["chapter_index"] for row in rows if row["chapter_index"] not in covered]


def preview_extend_patches(
    conn: sqlite3.Connection, book_id: int, patch_size: int | None = None
) -> list[dict]:
    """Plan the patches that would be appended for chapters no patch covers yet."""
    book = get_book(conn, book_id)
    if book is None:
        raise ValueError(f"book {book_id} not found")
    if patch_size is None:
        patch_size = book.patch_size
    if patch_size < 1:
        raise ValueError("patch_size must be >= 1")

    pending = uncovered_chapter_indices(conn, book_id)
    if not pending:
        return []

    next_patch_index = (
        conn.execute(
            "SELECT COALESCE(MAX(patch_index), -1) AS m FROM patch WHERE book_id = ?", (book_id,)
        ).fetchone()["m"]
        + 1
    )

    planned: list[dict] = []
    for offset in range(0, len(pending), patch_size):
        group = pending[offset : offset + patch_size]
        start, end = group[0], group[-1]
        row = conn.execute(
            "SELECT title FROM chapter WHERE book_id = ? AND chapter_index = ?", (book_id, start)
        ).fetchone()
        total_chars = conn.execute(
            """SELECT COALESCE(SUM(char_count), 0) AS c FROM chapter
               WHERE book_id = ? AND chapter_index BETWEEN ? AND ?""",
            (book_id, start, end),
        ).fetchone()["c"]
        no_start, no_end = chapter_no_range(conn, book_id, start, end)
        planned.append(
            {
                "patch_index": next_patch_index + len(planned),
                "chapter_start": start,
                "chapter_end": end,
                "chapter_no_start": no_start,
                "chapter_no_end": no_end,
                "name": row["title"] if row else "",
                "chunk_count": max(1, math.ceil(total_chars / _TTS_MAX_CHARS)),
            }
        )
    return planned


def extend_patches(
    conn: sqlite3.Connection, book_id: int, patch_size: int | None = None
) -> list[Patch]:
    """Append patches for uncovered chapters. Existing patches and their audio are untouched.

    This is the incremental counterpart of auto_build_patches, which deletes everything
    and starts over.
    """
    planned = preview_extend_patches(conn, book_id, patch_size)
    if not planned:
        return []

    now = _now()
    conn.executemany(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end,
                              chapter_no_start, chapter_no_end, name,
                              chunk_count, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
        [
            (
                book_id, entry["patch_index"], entry["chapter_start"], entry["chapter_end"],
                entry["chapter_no_start"], entry["chapter_no_end"],
                entry["name"], entry["chunk_count"], now, now,
            )
            for entry in planned
        ],
    )
    conn.execute(
        "UPDATE book SET final_audio_path = NULL, final_video_path = NULL, status = 'ready', updated_at = ? WHERE id = ?",
        (now, book_id),
    )
    conn.commit()
    created_indices = {entry["patch_index"] for entry in planned}
    return [patch for patch in list_patches(conn, book_id) if patch.patch_index in created_indices]


def auto_build_patches(
    conn: sqlite3.Connection,
    book_id: int,
    start_chapter: int,
    end_chapter: int | None = None,
    patch_size: int | None = None,
    force_rebuild: bool = False,
) -> list[Patch]:
    """Generate a patch list from start_chapter to end_chapter (or max chapter)
    in chunks of patch_size (or book.patch_size), skipping excluded chapters.

    When *force_rebuild* is True, existing patches for the affected ranges are
    deleted and recreated (via ``rebuild_patches`` with ``reset_done=True``).
    """

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

    return rebuild_patches(conn, book_id, ranges, reset_done=True, skip_excluded_check=True)


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
    row = _claim_next(conn, "book_job", "book_id, id")
    return _bookjob_from_row(row) if row else None


def mark_book_job_done(
    conn: sqlite3.Connection, job_id: int, output_path: str
) -> None:
    _update_status(conn, "book_job", job_id, status="done", output_path=output_path)


def mark_book_job_failed(
    conn: sqlite3.Connection, job_id: int, error_message: str
) -> None:
    _update_status(conn, "book_job", job_id, status="failed", error_message=error_message)


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
    failed_rows = [
        r for r in conn.execute(
            "SELECT id, book_id, patch_index, audio_path FROM patch WHERE book_id = ? AND status = 'failed'",
            (book_id,),
        ).fetchall()
    ]
    cur = conn.execute(
        """UPDATE patch SET status = 'pending', audio_path = NULL, error_message = NULL,
           next_chunk_index = 0, updated_at = ? WHERE book_id = ? AND status = 'failed'""",
        (now, book_id),
    )
    for row in failed_rows:
        if row["audio_path"]:
            backup_patch_audio_files(row["book_id"], row["patch_index"], row["audio_path"])
            delete_patch_audio_files(row["audio_path"])
        _delete_chunk_dir(row["book_id"], row["patch_index"], row["id"])
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
    paths_to_delete = [r["output_path"] for r in video_rows]
    patch_audio_paths = [r["audio_path"] for r in audio_rows]

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

    for path in patch_audio_paths:
        try:
            delete_patch_audio_files(path)
        except OSError:
            pass

    for row in chunk_rows:
        _delete_chunk_dir(row["book_id"], row["id"])

    return {
        "patches_reset": cur_p.rowcount,
        "book_jobs_reset": cur_bj.rowcount,
        "books_reset": cur_book.rowcount,
        "files_deleted": len(paths_to_delete) + len(patch_audio_paths),
    }


# ---------------------------------------------------------------------------
# Google Drive Desktop sync targets and patch export history
# ---------------------------------------------------------------------------


def create_drive_sync_target(conn: sqlite3.Connection, name: str, account_email: str, folder_path: str, rclone_remote: str | None = None):
    now = _now()
    cur = conn.execute(
        "INSERT INTO drive_sync_target (name, account_email, folder_path, rclone_remote, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
        (name, account_email, folder_path, rclone_remote or None, now, now),
    )
    conn.commit()
    return get_drive_sync_target(conn, cur.lastrowid)


def get_drive_sync_target(conn: sqlite3.Connection, target_id: int):
    row = conn.execute("SELECT * FROM drive_sync_target WHERE id = ?", (target_id,)).fetchone()
    return dict(row) if row else None


def list_drive_sync_targets(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM drive_sync_target ORDER BY name, id").fetchall()]


def update_drive_sync_target(conn: sqlite3.Connection, target_id: int, name: str, account_email: str, folder_path: str, rclone_remote: str | None = None) -> bool:
    cur = conn.execute(
        "UPDATE drive_sync_target SET name = ?, account_email = ?, folder_path = ?, rclone_remote = ?, updated_at = ? WHERE id = ?",
        (name, account_email, folder_path, rclone_remote or None, _now(), target_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_drive_sync_target(conn: sqlite3.Connection, target_id: int) -> bool:
    cur = conn.execute("DELETE FROM drive_sync_target WHERE id = ?", (target_id,))
    conn.commit()
    return cur.rowcount > 0


def _patch_export_from_row(row: sqlite3.Row) -> PatchExport:
    return PatchExport(**{k: row[k] for k in row.keys()})


def create_patch_export(
    conn: sqlite3.Connection,
    patch_id: int,
    drive_folder_id: str,
    drive_folder_link: str,
    exported_chunk_count: int,
    drive_account_id: int | None = None,
    sync_target_id: int | None = None,
    local_folder_path: str | None = None,
    commit: bool = True,
) -> PatchExport:
    now = _now()
    cur = conn.execute(
        """INSERT INTO patch_export (patch_id, drive_account_id, sync_target_id, local_folder_path, drive_folder_id, drive_folder_link,
                                      status, exported_chunk_count, imported_chunk_count, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, 'exported', ?, 0, ?, ?)""",
        (patch_id, drive_account_id, sync_target_id, local_folder_path, drive_folder_id, drive_folder_link, exported_chunk_count, now, now),
    )
    if commit:
        conn.commit()
    row = conn.execute("SELECT * FROM patch_export WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _patch_export_from_row(row)


def list_patch_exports(conn: sqlite3.Connection, patch_id: int) -> list[PatchExport]:
    rows = conn.execute(
        """SELECT pe.*, gdc.account_email, dst.name AS sync_target_name,
                  dst.account_email AS sync_target_email
             FROM patch_export pe
             LEFT JOIN google_drive_credentials gdc ON gdc.id = pe.drive_account_id
             LEFT JOIN drive_sync_target dst ON dst.id = pe.sync_target_id
            WHERE pe.patch_id = ? ORDER BY pe.id DESC""",
        (patch_id,),
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
        """SELECT pe.*, p.patch_index, p.book_id, b.title AS book_title, gdc.account_email,
                  dst.name AS sync_target_name, dst.account_email AS sync_target_email
             FROM patch_export pe
             JOIN patch p ON p.id = pe.patch_id
             JOIN book b ON b.id = p.book_id
             LEFT JOIN google_drive_credentials gdc ON gdc.id = pe.drive_account_id
             LEFT JOIN drive_sync_target dst ON dst.id = pe.sync_target_id
            ORDER BY pe.id DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_patch_export(conn: sqlite3.Connection, export_id: int) -> bool:
    """Delete a single patch_export row. Returns False if the row didn't exist."""
    cur = conn.execute("DELETE FROM patch_export WHERE id = ?", (export_id,))
    conn.commit()
    return cur.rowcount > 0


def count_pending_exports_for_account(conn: sqlite3.Connection, account_id: int) -> int:
    """Exports on this account whose audio has not been fully imported yet - shown as a
    warning before the user disconnects the account."""
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM patch_export
            WHERE drive_account_id = ? AND status IN ('exported', 'partially_imported')""",
        (account_id,),
    ).fetchone()
    return row["n"]


# ---------------------------------------------------------------------------
# Music library
# ---------------------------------------------------------------------------


def _music_from_row(row: sqlite3.Row) -> Music:
    return Music(**{k: row[k] for k in row.keys()})


def create_music(
    conn: sqlite3.Connection,
    *,
    name: str,
    file_path: str,
    duration_sec: float | None,
    description: str = "",
    license: str = "",
) -> Music:
    now = _now()
    cur = conn.execute(
        "INSERT INTO music (name, file_path, duration_sec, description, license, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (name, file_path, duration_sec, description, license, now),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM music WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _music_from_row(row)


def list_music(conn: sqlite3.Connection) -> list[Music]:
    return [_music_from_row(r) for r in conn.execute("SELECT * FROM music ORDER BY created_at DESC").fetchall()]


def list_music_paginated(conn: sqlite3.Connection, page: int = 1, per_page: int = 20) -> tuple[list[Music], int, int]:
    offset = (page - 1) * per_page
    rows = conn.execute("SELECT * FROM music ORDER BY created_at DESC LIMIT ? OFFSET ?", (per_page, offset)).fetchall()
    count_row = conn.execute("SELECT COUNT(*) AS c FROM music").fetchone()
    total = count_row["c"]
    total_pages = max(1, math.ceil(total / per_page))
    return [_music_from_row(r) for r in rows], total, total_pages


def get_music(conn: sqlite3.Connection, music_id: int) -> Music | None:
    row = conn.execute("SELECT * FROM music WHERE id = ?", (music_id,)).fetchone()
    return _music_from_row(row) if row else None


def rename_music(conn: sqlite3.Connection, music_id: int, new_name: str) -> bool:
    cur = conn.execute("UPDATE music SET name = ? WHERE id = ?", (new_name, music_id))
    conn.commit()
    return cur.rowcount > 0


def update_music_duration(conn: sqlite3.Connection, music_id: int, duration_sec: float | None) -> bool:
    """Refresh a track's cached duration after it was edited in place."""
    cur = conn.execute("UPDATE music SET duration_sec = ? WHERE id = ?", (duration_sec, music_id))
    conn.commit()
    return cur.rowcount > 0


def update_music_metadata(conn: sqlite3.Connection, music_id: int, description: str, license: str) -> bool:
    cur = conn.execute(
        "UPDATE music SET description = ?, license = ? WHERE id = ?",
        (description, license, music_id),
    )
    conn.commit()
    return cur.rowcount > 0


def delete_music(conn: sqlite3.Connection, music_id: int) -> bool:
    conn.execute("UPDATE book SET music_id = NULL WHERE music_id = ?", (music_id,))
    cur = conn.execute("DELETE FROM music WHERE id = ?", (music_id,))
    conn.commit()
    return cur.rowcount > 0


def set_book_music(
    conn: sqlite3.Connection,
    book_id: int,
    music_id: int | None,
    music_volume: float,
) -> None:
    conn.execute(
        "UPDATE book SET music_id = ?, music_volume = ?, updated_at = ? WHERE id = ?",
        (music_id, music_volume, _now(), book_id),
    )
    conn.commit()


def set_book_voice_clip(
    conn: sqlite3.Connection,
    book_id: int,
    voice_clip_path: str | None,
    voice_transcript: str | None = None,
) -> None:
    conn.execute(
        "UPDATE book SET voice_clip_path = ?, voice_transcript = ?, updated_at = ? WHERE id = ?",
        (voice_clip_path, voice_transcript, _now(), book_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Voice meta (description + classification for voice clips)
# ---------------------------------------------------------------------------


def get_voice_meta(conn: sqlite3.Connection, filename: str) -> dict | None:
    row = conn.execute("SELECT * FROM voice_meta WHERE filename = ?", (filename,)).fetchone()
    if row is None:
        return None
    return {
        "filename": row["filename"],
        "description": row["description"],
        "gender": row["gender"],
        "genre": row["genre"],
    }


def set_voice_meta(
    conn: sqlite3.Connection,
    filename: str,
    description: str | None = None,
    gender: str | None = None,
    genre: str | None = None,
) -> None:
    """Upsert one voice clip's metadata; a None field is left as it was.

    Named parameters (rather than ``excluded.*``) so the COALESCE in the UPDATE
    branch sees the caller's raw None, not the '' the INSERT branch defaults to.
    """
    conn.execute(
        """INSERT INTO voice_meta (filename, description, gender, genre, created_at, updated_at)
           VALUES (:filename, COALESCE(:description, ''), COALESCE(:gender, ''),
                   COALESCE(:genre, ''), :now, :now)
           ON CONFLICT(filename) DO UPDATE SET
               description = COALESCE(:description, description),
               gender      = COALESCE(:gender, gender),
               genre       = COALESCE(:genre, genre),
               updated_at  = :now""",
        {
            "filename": filename,
            "description": description,
            "gender": gender,
            "genre": genre,
            "now": _now(),
        },
    )
    conn.commit()


def copy_voice_meta(conn: sqlite3.Connection, src_filename: str, dest_filename: str) -> None:
    """Carry a clip's classification onto a derived file (e.g. a processed copy)."""
    meta = get_voice_meta(conn, src_filename)
    if meta is None:
        return
    set_voice_meta(
        conn,
        dest_filename,
        description=meta["description"],
        gender=meta["gender"],
        genre=meta["genre"],
    )


def rename_voice_meta(conn: sqlite3.Connection, old_filename: str, new_filename: str) -> None:
    conn.execute(
        "UPDATE voice_meta SET filename = ?, updated_at = ? WHERE filename = ?",
        (new_filename, _now(), old_filename),
    )
    conn.commit()


def delete_voice_meta(conn: sqlite3.Connection, filename: str) -> None:
    conn.execute("DELETE FROM voice_meta WHERE filename = ?", (filename,))
    conn.commit()


# ---------------------------------------------------------------------------
# Text Studio: patch clean text
# ---------------------------------------------------------------------------


def get_effective_patch_text(conn: sqlite3.Connection, patch: Patch) -> str:
    """Return clean_text if set, otherwise derived text."""
    if patch.clean_text:
        return patch.clean_text
    return build_patch_text(conn, patch)


def save_patch_clean_text(conn: sqlite3.Connection, patch_id: int, text: str) -> None:
    from app.text_analysis import text_hash
    h = text_hash(text)
    conn.execute(
        "UPDATE patch SET clean_text = ?, clean_text_hash = ?, updated_at = ? WHERE id = ?",
        (text, h, _now(), patch_id),
    )
    conn.commit()


def reset_patch_clean_text(conn: sqlite3.Connection, patch_id: int) -> None:
    conn.execute(
        "UPDATE patch SET clean_text = NULL, clean_text_hash = NULL, text_fingerprint = NULL, updated_at = ? WHERE id = ?",
        (_now(), patch_id),
    )
    conn.execute("DELETE FROM patch_warning WHERE patch_id = ?", (patch_id,))
    conn.commit()


def list_patch_warnings(conn: sqlite3.Connection, patch_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM patch_warning WHERE patch_id = ? ORDER BY position", (patch_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def save_patch_warnings(conn: sqlite3.Connection, patch_id: int, warnings: list[dict]) -> None:
    conn.execute("DELETE FROM patch_warning WHERE patch_id = ?", (patch_id,))
    now = _now()
    conn.executemany(
        """INSERT INTO patch_warning (patch_id, kind, position, length, original, suggestion, accepted, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?)""",
        [(patch_id, w["kind"], w["position"], w["length"], w["original"], w.get("suggestion", ""), now) for w in warnings],
    )
    conn.commit()


def update_patch_warning_status(conn: sqlite3.Connection, warning_id: int, accepted: int) -> None:
    conn.execute("UPDATE patch_warning SET accepted = ? WHERE id = ?", (accepted, warning_id))
    conn.commit()


def list_sound_effects(conn: sqlite3.Connection, book_id: int | None = None) -> list[dict]:
    rows = conn.execute("SELECT * FROM sound_effect ORDER BY marker").fetchall()
    return [dict(r) for r in rows]


def get_sound_effect(conn: sqlite3.Connection, effect_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM sound_effect WHERE id = ?", (effect_id,)).fetchone()
    return dict(row) if row else None


def create_sound_effect(conn: sqlite3.Connection, book_id: int | None, marker: str, file_path: str, description: str = "") -> int:
    now = _now()
    cur = conn.execute(
        "INSERT INTO sound_effect (marker, file_path, description, created_at) VALUES (?, ?, ?, ?)",
        (marker, file_path, description, now),
    )
    conn.commit()
    return cur.lastrowid


def update_sound_effect(conn: sqlite3.Connection, effect_id: int, marker: str, description: str) -> None:
    conn.execute(
        "UPDATE sound_effect SET marker = ?, description = ? WHERE id = ?",
        (marker, description, effect_id),
    )
    conn.commit()


def delete_sound_effect(conn: sqlite3.Connection, effect_id: int) -> None:
    conn.execute("DELETE FROM sound_effect WHERE id = ?", (effect_id,))
    conn.commit()


def build_patch_metadata_context(conn: sqlite3.Connection, book: Book, patch: Patch) -> dict:
    """Database facts the generated YouTube description needs: the chapters this
    patch actually speaks, and the background track whose licence must be credited."""
    chapters = get_chapters_in_range(conn, patch.book_id, patch.chapter_start, patch.chapter_end)
    music = get_music(conn, book.music_id) if book.music_id else None
    return {
        "chapter_titles": [ch.title for ch in chapters if not ch.is_excluded and ch.title],
        "music": {"name": music.name, "description": music.description, "license": music.license} if music else None,
    }


def build_youtube_description(
    conn: sqlite3.Connection, book_id: int
) -> dict:
    book = get_book(conn, book_id)
    if book is None:
        return {"description": "", "tags": []}

    parts: list[str] = [f"{book.title} - EPUB Audiobook"]

    music = None
    if book.music_id is not None:
        music = get_music(conn, book.music_id)
    if music and (music.description or music.license):
        parts.append("")
        parts.append(f"🎵 Background Music: {music.name}")
        if music.description:
            parts.append(music.description)
        if music.license:
            parts.append(f"License: {music.license}")

    patches = list_patches(conn, book_id)
    if patches:
        parts.append("")
        parts.append("📚 Chapters:")
        for p in patches:
            start, end, clean_name = resolve_patch_chapter_range(p)
            label = clean_name or p.name or f"Patch {p.patch_index}"
            parts.append(f"{format_chapter_range(start, end)}: {label}")

    desc = "\n".join(parts)

    if len(desc) > 5000:
        cut = desc.rfind("\n", 0, 5000)
        desc = desc[:cut] if cut > 0 else desc[:5000]

    words = set(book.title.lower().split())
    defaults = {"audiobook", "epub", "text-to-speech", "vietnamese"}
    tags = list(words | defaults)

    return {"description": desc, "tags": tags}
