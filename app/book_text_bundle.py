"""Portable text-only export of one book.

A text bundle carries the book's text layer and nothing else: metadata, the
full table of contents (every chapter, including excluded/empty ones), chapter
text, patch ranges/overrides, replace rules and the settings that change how
the text is spoken. It deliberately leaves out audio, video, images and the
EPUB (which are large and machine-specific), so the file can be moved to
another local machine, edited/continued there, and copied back.

Identity and round-trip
-----------------------
Every bundle has a stable ``bundle_id`` derived from its book. The mapping
``book_text_bundle:book:<book_id>`` -> bundle/origin info is stored in
``app_state`` so exporting the same book again reuses the id, and importing on
another machine records the same origin. Importing a bundle back into the
library therefore updates the *same* book instead of creating a duplicate; a
fresh book is only created when no match exists (or ``mode="new"``).

Merge semantics
---------------
Import never touches audio/video state: patch status, ``audio_path``, chunk
progress and pipeline rows are preserved. Only the text layer is written. When
an imported patch's text differs from what the local machine had, the patch is
reset to ``pending`` (its existing audio file is kept, but it is no longer
considered done) and reported in ``text_changed`` so the caller can tell the
user to re-run TTS. Text processing / audio preferences are only applied when
the book is newly created, so copying a bundle back cannot silently change the
origin machine's voice setup.
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from app import repository
from app.text_analysis import text_hash
from app.validation import detect_chapter_number

BUNDLE_FORMAT = "epub-audiobook.book-text"
BUNDLE_VERSION = 1

_BOOK_KEY = "book_text_bundle:book:{}"
_ID_KEY = "book_text_bundle:id:{}"

# Columns whose values are text or text-processing configuration.
_BOOK_TEXT_FIELDS = (
    "title",
    "original_filename",
    "patch_size",
    "author",
    "description",
    "language",
    "publisher",
    "subjects",
    "overlay_config",
    "created_at",
)
_NORMALIZATION_FIELDS = (
    "normalize_numbers_enabled",
    "normalize_junk_enabled",
    "normalize_spellcheck_enabled",
    "normalize_dictionary_enabled",
    "normalize_transliteration_enabled",
    "normalize_abbreviations_enabled",
    "normalize_breaks_enabled",
)
_AUDIO_FIELDS = (
    "tts_model",
    "tts_voice_id",
    "tts_max_chars",
    "tts_with_effects",
    "export_tts_model",
    "export_tts_voice_id",
    "export_tts_max_chars",
    "export_tts_with_effects",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(value: str) -> str:
    """ASCII filename stem that is safe on every OS."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value or "").strip("-.")
    return cleaned[:80] or "book"


def bundle_filename(book: Any) -> str:
    return f"{_slug(getattr(book, 'title', '') or 'book')}.book-text.json"


# ---------------------------------------------------------------------------
# Identity mapping (app_state)
# ---------------------------------------------------------------------------


def _read_mapping(conn: sqlite3.Connection, book_id: int) -> dict | None:
    raw = repository.get_app_state(conn, _BOOK_KEY.format(book_id))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _origin_info(book: Any, mapping: dict | None) -> dict:
    mapping = mapping or {}
    return {
        "bundle_id": mapping.get("bundle_id") or uuid.uuid4().hex,
        "origin_book_id": mapping.get("origin_book_id") or book.id,
        "origin_created_at": (
            mapping.get("origin_created_at") or getattr(book, "created_at", "") or ""
        ),
    }


def _write_mapping(conn: sqlite3.Connection, book_id: int, origin: dict) -> None:
    payload = json.dumps(origin, ensure_ascii=False)
    conn.execute(
        """INSERT INTO app_state (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (_BOOK_KEY.format(book_id), payload),
    )
    conn.execute(
        """INSERT INTO app_state (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (_ID_KEY.format(origin["bundle_id"]), str(book_id)),
    )


def _find_import_target(conn: sqlite3.Connection, bundle: dict) -> Any | None:
    """Locate the book this bundle belongs to on *this* machine."""
    origin = bundle.get("origin") or {}
    bundle_id = origin.get("bundle_id")
    if bundle_id:
        raw = repository.get_app_state(conn, _ID_KEY.format(bundle_id))
        if raw and str(raw).isdigit():
            book = repository.get_book(conn, int(raw))
            if book is not None:
                return book

    # Mapping may exist per book while the id index was lost (DB merged by hand).
    origin_book_id = origin.get("origin_book_id")
    origin_created_at = origin.get("origin_created_at") or ""
    if origin_book_id is not None:
        rows = conn.execute(
            "SELECT key, value FROM app_state WHERE key LIKE 'book_text_bundle:book:%'"
        ).fetchall()
        for row in rows:
            try:
                mapping = json.loads(row["value"])
            except (TypeError, ValueError):
                continue
            if not isinstance(mapping, dict):
                continue
            if mapping.get("origin_book_id") != origin_book_id:
                continue
            if origin_created_at and mapping.get("origin_created_at") != origin_created_at:
                continue
            book = repository.get_book(conn, int(row["key"].rsplit(":", 1)[-1]))
            if book is not None:
                return book

    # Last resort: the original book row itself (mapping was never written).
    created_at = (bundle.get("book") or {}).get("created_at") or ""
    title = (bundle.get("book") or {}).get("title") or ""
    if created_at and title:
        row = conn.execute(
            "SELECT id FROM book WHERE created_at = ? AND title = ? ORDER BY id LIMIT 1",
            (created_at, title),
        ).fetchone()
        if row is not None:
            return repository.get_book(conn, int(row["id"]))
    return None


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def export_book_text(conn: sqlite3.Connection, book_id: int) -> dict[str, Any]:
    """Build the portable text bundle for one book.

    Raises ``LookupError`` when the book does not exist.
    """
    book = repository.get_book(conn, book_id)
    if book is None:
        raise LookupError("book not found")

    origin = _origin_info(book, _read_mapping(conn, book_id))
    _write_mapping(conn, book_id, origin)
    conn.commit()

    chapters = repository.list_chapters(conn, book_id)
    patches = repository.list_patches(conn, book_id)
    rules = repository.list_replace_rules(conn, book_id)

    book_payload = {field: getattr(book, field, None) for field in _BOOK_TEXT_FIELDS}
    book_payload.update({field: int(getattr(book, field, 0) or 0) for field in _NORMALIZATION_FIELDS})
    audio_payload = {field: getattr(book, field, None) for field in _AUDIO_FIELDS}

    chapter_payload = [
        {
            "chapter_index": chapter.chapter_index,
            "title": chapter.title or "",
            "text": chapter.text or "",
            "char_count": chapter.char_count or len(chapter.text or ""),
            "chapter_no": chapter.chapter_no,
            "text_hash": chapter.text_hash,
            "is_excluded": bool(chapter.is_excluded),
        }
        for chapter in chapters
    ]
    # Explicit table of contents: one row per chapter, including excluded and
    # empty ones. `chapters` carries the full text; `toc` keeps the catalog
    # readable (and restorable) even when a consumer only wants the index.
    toc_payload = [
        {
            "chapter_index": chapter["chapter_index"],
            "title": chapter["title"],
            "chapter_no": chapter["chapter_no"],
            "char_count": chapter["char_count"],
            "is_excluded": chapter["is_excluded"],
            "has_text": bool((chapter["text"] or "").strip()),
            "text_hash": chapter["text_hash"],
        }
        for chapter in chapter_payload
    ]
    patch_payload = [
        {
            "patch_index": patch.patch_index,
            "chapter_start": patch.chapter_start,
            "chapter_end": patch.chapter_end,
            "chapter_no_start": patch.chapter_no_start,
            "chapter_no_end": patch.chapter_no_end,
            "name": patch.name or "",
            "chunk_count": patch.chunk_count,
            "max_chars": patch.max_chars,
            "clean_text": patch.clean_text,
            "clean_text_hash": patch.clean_text_hash,
            "youtube_override": patch.youtube_override,
        }
        for patch in patches
    ]
    rule_payload = [
        {
            "find": rule.find,
            "replace": rule.replace or "",
            "is_regex": bool(rule.is_regex),
            "position": rule.position,
        }
        for rule in rules
    ]
    numbered = [row["chapter_no"] for row in toc_payload if row["chapter_no"] is not None]

    return {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "bundle_id": origin["bundle_id"],
        "exported_at": _now(),
        "origin": origin,
        "book": book_payload,
        "audio_settings": audio_payload,
        "toc": toc_payload,
        "chapters": chapter_payload,
        "patches": patch_payload,
        "replace_rules": rule_payload,
        "stats": {
            "chapter_count": len(chapter_payload),
            "excluded_chapter_count": sum(1 for row in toc_payload if row["is_excluded"]),
            "empty_chapter_count": sum(1 for row in toc_payload if not row["has_text"]),
            "first_chapter_no": numbered[0] if numbered else None,
            "last_chapter_no": numbered[-1] if numbered else None,
            "patch_count": len(patch_payload),
            "char_count": sum(int(c.get("char_count") or 0) for c in chapter_payload),
            "edited_patch_count": sum(1 for p in patch_payload if p.get("clean_text") is not None),
        },
    }


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _as_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Trường '{field}' phải là số nguyên.") from exc


def validate_bundle(data: Any) -> dict:
    """Validate and normalise an uploaded bundle; raises ``ValueError``."""
    if not isinstance(data, dict):
        raise ValueError("File không phải JSON object.")
    if data.get("format") != BUNDLE_FORMAT:
        raise ValueError("Đây không phải file text sách của ứng dụng.")

    version = data.get("version")
    if not isinstance(version, int) or version < 1:
        raise ValueError("File không có phiên bản hợp lệ.")
    if version > BUNDLE_VERSION:
        raise ValueError(
            f"File được tạo bởi phiên bản mới hơn (v{version}); hãy cập nhật ứng dụng."
        )

    book = data.get("book")
    if not isinstance(book, dict):
        raise ValueError("File thiếu phần thông tin sách.")

    chapters = data.get("chapters")
    if not isinstance(chapters, list) or not chapters:
        raise ValueError("File không có chương nào để nhập.")

    seen_chapters: set[int] = set()
    for position, chapter in enumerate(chapters):
        if not isinstance(chapter, dict):
            raise ValueError(f"Chương thứ {position + 1} không hợp lệ.")
        index = _as_int(chapter.get("chapter_index"), "chapter_index")
        if index < 0:
            raise ValueError("chapter_index không được âm.")
        if index in seen_chapters:
            raise ValueError(f"chapter_index {index} bị trùng.")
        seen_chapters.add(index)
        if not isinstance(chapter.get("text"), str):
            raise ValueError(f"Chương {index} thiếu nội dung text.")

    toc = data.get("toc")
    if toc is not None:
        if not isinstance(toc, list):
            raise ValueError("Danh mục chương không hợp lệ.")
        toc_indexes: set[int] = set()
        for position, row in enumerate(toc):
            if not isinstance(row, dict):
                raise ValueError(f"Mục lục thứ {position + 1} không hợp lệ.")
            index = _as_int(row.get("chapter_index"), "chapter_index")
            if index in toc_indexes:
                raise ValueError(f"chapter_index {index} bị trùng trong mục lục.")
            toc_indexes.add(index)
            if index not in seen_chapters:
                raise ValueError(f"Mục lục trỏ tới chương {index} không có trong file.")
        missing = seen_chapters - toc_indexes
        if missing:
            sample = ", ".join(str(index) for index in sorted(missing)[:5])
            raise ValueError(f"Mục lục thiếu chương: {sample}.")

    patches = data.get("patches") or []
    if not isinstance(patches, list):
        raise ValueError("Danh sách patch không hợp lệ.")
    seen_patches: set[int] = set()
    for position, patch in enumerate(patches):
        if not isinstance(patch, dict):
            raise ValueError(f"Patch thứ {position + 1} không hợp lệ.")
        index = _as_int(patch.get("patch_index"), "patch_index")
        if index < 0:
            raise ValueError("patch_index không được âm.")
        if index in seen_patches:
            raise ValueError(f"patch_index {index} bị trùng.")
        seen_patches.add(index)
        start = _as_int(patch.get("chapter_start"), "chapter_start")
        end = _as_int(patch.get("chapter_end"), "chapter_end")
        if start > end:
            raise ValueError(f"Patch {index} có khoảng chương không hợp lệ.")
        if start not in seen_chapters:
            raise ValueError(f"Patch {index} trỏ tới chương {start} không có trong file.")
        clean_text = patch.get("clean_text")
        if clean_text is not None and not isinstance(clean_text, str):
            raise ValueError(f"clean_text của patch {index} không hợp lệ.")

    rules = data.get("replace_rules") or []
    if not isinstance(rules, list):
        raise ValueError("Danh sách replace rules không hợp lệ.")
    for position, rule in enumerate(rules):
        if not isinstance(rule, dict) or not isinstance(rule.get("find"), str):
            raise ValueError(f"Replace rule thứ {position + 1} không hợp lệ.")
    return data


# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------


def _chapter_signature(chapter_texts: dict[int, str], start: int, end: int) -> str:
    return text_hash("\n".join(chapter_texts.get(index, "") for index in range(start, end + 1)))


def _patch_signature(
    chapter_texts: dict[int, str], patch: dict, clean_text: str | None
) -> str:
    if clean_text is not None:
        return text_hash(clean_text)
    return _chapter_signature(
        chapter_texts,
        _as_int(patch.get("chapter_start"), "chapter_start"),
        _as_int(patch.get("chapter_end"), "chapter_end"),
    )


def _load_chapter_texts(conn: sqlite3.Connection, book_id: int) -> dict[int, str]:
    """Spoken text per chapter; an excluded chapter contributes nothing to a patch."""
    rows = conn.execute(
        "SELECT chapter_index, text, is_excluded FROM chapter WHERE book_id = ?", (book_id,)
    ).fetchall()
    return {
        int(row["chapter_index"]): "" if row["is_excluded"] else row["text"] or ""
        for row in rows
    }


def _bundle_chapter_texts(bundle: dict) -> dict[int, str]:
    """Same view as ``_load_chapter_texts``, for the chapters in a bundle."""
    return {
        _as_int(chapter.get("chapter_index"), "chapter_index"): (
            "" if chapter.get("is_excluded") else chapter.get("text") or ""
        )
        for chapter in bundle["chapters"]
    }


def _load_patches(conn: sqlite3.Connection, book_id: int) -> dict[int, sqlite3.Row]:
    rows = conn.execute("SELECT * FROM patch WHERE book_id = ?", (book_id,)).fetchall()
    return {int(row["patch_index"]): row for row in rows}


def _create_book(conn: sqlite3.Connection, bundle: dict) -> int:
    book = bundle["book"]
    chapters = bundle["chapters"]
    now = _now()
    created_at = book.get("created_at") or now
    title = str(book.get("title") or "Sách không tên")
    patch_size = _as_int(book.get("patch_size") or 10, "patch_size")
    cur = conn.execute(
        """INSERT INTO book
               (title, original_filename, epub_path, patch_size, status,
                author, description, language, publisher, subjects,
                cover_image_path, overlay_config, created_at, updated_at,
                normalize_numbers_enabled, normalize_junk_enabled,
                normalize_spellcheck_enabled, normalize_dictionary_enabled,
                normalize_transliteration_enabled, normalize_abbreviations_enabled,
                normalize_breaks_enabled)
           VALUES (?, ?, '', ?, 'ready', ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            title,
            str(book.get("original_filename") or ""),
            patch_size,
            str(book.get("author") or ""),
            str(book.get("description") or ""),
            str(book.get("language") or ""),
            str(book.get("publisher") or ""),
            str(book.get("subjects") or ""),
            book.get("overlay_config"),
            created_at,
            now,
            *(int(book.get(field) or 0) for field in _NORMALIZATION_FIELDS),
        ),
    )
    book_id = int(cur.lastrowid)

    # Audio preferences only seed a newly created book; a merge leaves the local
    # machine's voice setup untouched.
    audio = bundle.get("audio_settings") or {}
    if isinstance(audio, dict) and audio:
        assignments = ", ".join(f"{field} = ?" for field in _AUDIO_FIELDS if field in audio)
        if assignments:
            conn.execute(
                f"UPDATE book SET {assignments} WHERE id = ?",
                [audio.get(field) for field in _AUDIO_FIELDS if field in audio] + [book_id],
            )

    for chapter in chapters:
        index = _as_int(chapter.get("chapter_index"), "chapter_index")
        text = chapter.get("text") or ""
        conn.execute(
            """INSERT INTO chapter
                   (book_id, chapter_index, title, text, char_count, chapter_no,
                    text_hash, is_excluded)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                book_id,
                index,
                str(chapter.get("title") or ""),
                text,
                len(text),
                chapter.get("chapter_no", detect_chapter_number(chapter.get("title"))),
                chapter.get("text_hash") or text_hash(text),
                1 if chapter.get("is_excluded") else 0,
            ),
        )
    return book_id


def _merge_book_header(conn: sqlite3.Connection, book_id: int, bundle: dict) -> bool:
    book = bundle["book"]
    fields = [
        "title",
        "original_filename",
        "author",
        "description",
        "language",
        "publisher",
        "subjects",
        "overlay_config",
    ]
    assignments = [field for field in fields if field in book]
    assignments.extend(field for field in _NORMALIZATION_FIELDS if field in book)
    if not assignments:
        return False
    values: list[Any] = [book[field] for field in fields if field in book]
    values.extend(int(book[field] or 0) for field in _NORMALIZATION_FIELDS if field in book)
    values.extend([_now(), book_id])
    conn.execute(
        f"UPDATE book SET {', '.join(f'{field} = ?' for field in assignments)}, updated_at = ? WHERE id = ?",
        values,
    )
    return True


def _apply_chapters(
    conn: sqlite3.Connection, book_id: int, chapters: list[dict]
) -> tuple[int, int]:
    existing = {
        int(row["chapter_index"]): row
        for row in conn.execute(
            "SELECT chapter_index, title, text, is_excluded, chapter_no FROM chapter WHERE book_id = ?",
            (book_id,),
        ).fetchall()
    }
    created = updated = 0
    for chapter in chapters:
        index = _as_int(chapter.get("chapter_index"), "chapter_index")
        text = chapter.get("text") or ""
        title = str(chapter.get("title") or "")
        char_count = len(text)
        chapter_no = chapter.get("chapter_no", detect_chapter_number(title))
        excluded = 1 if chapter.get("is_excluded") else 0
        row = existing.get(index)
        if row is None:
            conn.execute(
                """INSERT INTO chapter
                       (book_id, chapter_index, title, text, char_count, chapter_no,
                        text_hash, is_excluded)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (book_id, index, title, text, char_count, chapter_no,
                 chapter.get("text_hash") or text_hash(text), excluded),
            )
            created += 1
            continue
        changed = (
            (row["title"] or "") != title
            or (row["text"] or "") != text
            or int(row["is_excluded"] or 0) != excluded
            or row["chapter_no"] != chapter_no
        )
        if not changed:
            continue
        conn.execute(
            """UPDATE chapter
                  SET title = ?, text = ?, char_count = ?, chapter_no = ?,
                      text_hash = ?, is_excluded = ?
                WHERE book_id = ? AND chapter_index = ?""",
            (title, text, char_count, chapter_no,
             chapter.get("text_hash") or text_hash(text), excluded, book_id, index),
        )
        updated += 1
    return created, updated


def _apply_patches(
    conn: sqlite3.Connection,
    book_id: int,
    patches: list[dict],
    chapter_texts: dict[int, str],
    old_chapter_texts: dict[int, str],
    old_patches: dict[int, sqlite3.Row],
) -> tuple[int, int, int, list[int], list[int]]:
    max_chapter = max(chapter_texts) if chapter_texts else -1
    now = _now()
    created = updated = skipped = 0
    text_changed: list[int] = []
    new_patch_indexes = {_as_int(p.get("patch_index"), "patch_index") for p in patches}

    for patch in patches:
        index = _as_int(patch.get("patch_index"), "patch_index")
        start = _as_int(patch.get("chapter_start"), "chapter_start")
        end = _as_int(patch.get("chapter_end"), "chapter_end")
        if start > max_chapter:
            skipped += 1
            continue
        end = min(end, max_chapter)
        clean_text = patch.get("clean_text")
        new_signature = _patch_signature(chapter_texts, patch, clean_text)

        row = old_patches.get(index)
        if row is None:
            conn.execute(
                """INSERT INTO patch
                       (book_id, patch_index, chapter_start, chapter_end,
                        chapter_no_start, chapter_no_end, name, chunk_count,
                        max_chars, clean_text, clean_text_hash, youtube_override,
                        status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)""",
                (
                    book_id, index, start, end,
                    patch.get("chapter_no_start"), patch.get("chapter_no_end"),
                    str(patch.get("name") or ""), int(patch.get("chunk_count") or 1),
                    patch.get("max_chars"), clean_text,
                    patch.get("clean_text_hash") or (text_hash(clean_text) if clean_text else None),
                    patch.get("youtube_override"), now, now,
                ),
            )
            created += 1
            continue

        old_signature = _patch_signature(
            old_chapter_texts,
            {"chapter_start": row["chapter_start"], "chapter_end": row["chapter_end"]},
            row["clean_text"],
        )
        values = (
            start,
            end,
            patch.get("chapter_no_start", row["chapter_no_start"]),
            patch.get("chapter_no_end", row["chapter_no_end"]),
            str(patch.get("name") or ""),
            patch.get("max_chars", row["max_chars"]),
            clean_text,
            patch.get("clean_text_hash") or (text_hash(clean_text) if clean_text else None),
            patch.get("youtube_override", row["youtube_override"]),
        )
        conn.execute(
            """UPDATE patch
                  SET chapter_start = ?, chapter_end = ?, chapter_no_start = ?,
                      chapter_no_end = ?, name = ?, max_chars = ?,
                      clean_text = ?, clean_text_hash = ?, youtube_override = ?,
                      updated_at = ?
                WHERE id = ?""",
            values + (now, row["id"]),
        )
        if new_signature != old_signature:
            text_changed.append(index)
            # The text the current audio was built from is gone. Keep the file,
            # but stop advertising the patch as done.
            conn.execute(
                """UPDATE patch
                      SET status = 'pending', next_chunk_index = 0,
                          chunk_count_exact = 0, error_message = NULL,
                          text_fingerprint = ?
                    WHERE id = ?""",
                (new_signature, row["id"]),
            )
        updated += 1

    # Local patches missing from the bundle keep their audio; they are just
    # not part of this text snapshot.
    missing = set(old_patches) - new_patch_indexes
    return created, updated, skipped, text_changed, sorted(missing)


def _apply_replace_rules(conn: sqlite3.Connection, book_id: int, rules: list[dict]) -> int:
    conn.execute("DELETE FROM text_replace_rule WHERE book_id = ?", (book_id,))
    for position, rule in enumerate(rules):
        conn.execute(
            """INSERT INTO text_replace_rule (book_id, find, replace, is_regex, position)
               VALUES (?, ?, ?, ?, ?)""",
            (
                book_id,
                str(rule.get("find") or ""),
                str(rule.get("replace") or ""),
                1 if rule.get("is_regex") else 0,
                int(rule.get("position", position)),
            ),
        )
    return len(rules)


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


def import_book_text(
    conn: sqlite3.Connection,
    data: dict,
    *,
    mode: str = "merge",
    target_book_id: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Import a text bundle, merging into the matching book by default.

    ``mode="merge"`` updates the book this bundle came from (creating it if it
    is not on this machine); ``mode="new"`` always creates a fresh book.
    ``dry_run`` performs the merge inside the transaction, reports the summary,
    then rolls back — handy for a preview before touching a library. A failure
    rolls back too: the connection is shared, so partial writes left open here
    would be persisted by the next unrelated commit.
    """
    if mode not in ("merge", "new"):
        raise ValueError("mode must be 'merge' or 'new'")
    bundle = validate_bundle(data)
    try:
        return _import_validated(conn, bundle, mode, target_book_id, dry_run)
    except Exception:
        conn.rollback()
        raise


def _import_validated(
    conn: sqlite3.Connection,
    bundle: dict,
    mode: str,
    target_book_id: int | None,
    dry_run: bool,
) -> dict[str, Any]:
    existing = None
    if mode == "merge":
        if target_book_id is not None:
            existing = repository.get_book(conn, target_book_id)
        if existing is None:
            existing = _find_import_target(conn, bundle)

    origin = bundle.get("origin") if isinstance(bundle.get("origin"), dict) else {}
    bundle_id = bundle.get("bundle_id") or origin.get("bundle_id")

    if existing is None:
        book_id = _create_book(conn, bundle)
        chapter_texts = _bundle_chapter_texts(bundle)
        patch_created, _, patch_skipped, _, _ = _apply_patches(
            conn, book_id, bundle.get("patches") or [], chapter_texts, {}, {}
        )
        rule_count = _apply_replace_rules(conn, book_id, bundle.get("replace_rules") or [])
        if mode == "new":
            # Fork: do not claim the incoming bundle identity, otherwise importing
            # the same file again would land in this copy instead of the original.
            created_book = repository.get_book(conn, book_id)
            origin_info = {
                "bundle_id": uuid.uuid4().hex,
                "origin_book_id": book_id,
                "origin_created_at": getattr(created_book, "created_at", "") or "",
            }
        else:
            origin_info = _origin_info(
                repository.get_book(conn, book_id),
                {
                    "bundle_id": bundle_id,
                    "origin_book_id": origin.get("origin_book_id"),
                    "origin_created_at": origin.get("origin_created_at"),
                },
            )
        summary = {
            "status": "ok",
            "mode": mode,
            "dry_run": dry_run,
            "created": True,
            "book_id": book_id,
            "title": (bundle.get("book") or {}).get("title") or "",
            "chapters": {"created": len(bundle["chapters"]), "updated": 0},
            "patches": {"created": patch_created, "updated": 0, "skipped": patch_skipped},
            "replace_rules": rule_count,
            "text_changed": [],
            "warnings": [],
        }
        if dry_run:
            summary["warnings"].append("Chế độ xem trước: chưa ghi gì vào thư viện.")
            conn.rollback()
        else:
            _write_mapping(conn, book_id, origin_info)
            conn.commit()
        return summary

    book_id = existing.id
    old_chapter_texts = _load_chapter_texts(conn, book_id)
    old_patches = _load_patches(conn, book_id)
    header_updated = _merge_book_header(conn, book_id, bundle)
    chapter_created, chapter_updated = _apply_chapters(conn, book_id, bundle["chapters"])
    chapter_texts = _bundle_chapter_texts(bundle)
    patch_created, patch_updated, patch_skipped, text_changed, missing_patches = _apply_patches(
        conn, book_id, bundle.get("patches") or [], chapter_texts, old_chapter_texts, old_patches
    )
    rule_count = _apply_replace_rules(conn, book_id, bundle.get("replace_rules") or [])
    conn.execute("UPDATE book SET updated_at = ? WHERE id = ?", (_now(), book_id))

    warnings: list[str] = []
    if patch_skipped:
        warnings.append(f"{patch_skipped} patch nằm ngoài số chương hiện có và đã bị bỏ qua.")
    if missing_patches:
        warnings.append(
            f"{len(missing_patches)} patch chỉ có ở máy này (không có trong file) được giữ nguyên."
        )
    if text_changed:
        warnings.append(
            f"{len(text_changed)} patch có nội dung thay đổi đã được đưa về trạng thái chờ để tạo lại audio."
        )

    origin_info = _origin_info(
        repository.get_book(conn, book_id),
        {
            "bundle_id": bundle_id,
            "origin_book_id": origin.get("origin_book_id"),
            "origin_created_at": origin.get("origin_created_at"),
        },
    )
    if dry_run:
        conn.rollback()
    else:
        _write_mapping(conn, book_id, origin_info)
        conn.commit()

    return {
        "status": "ok",
        "mode": mode,
        "dry_run": dry_run,
        "created": False,
        "book_id": book_id,
        "title": (bundle.get("book") or {}).get("title") or existing.title,
        "chapters": {"created": chapter_created, "updated": chapter_updated},
        "patches": {"created": patch_created, "updated": patch_updated, "skipped": patch_skipped},
        "replace_rules": rule_count,
        "text_changed": text_changed,
        "warnings": warnings,
        "book_updated": header_updated,
    }


__all__ = [
    "BUNDLE_FORMAT",
    "BUNDLE_VERSION",
    "bundle_filename",
    "export_book_text",
    "import_book_text",
    "validate_bundle",
]
