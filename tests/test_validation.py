"""Chapter/patch validation, chapter-number detection and incremental EPUB update."""
from __future__ import annotations

import sqlite3

import pytest

from app import repository
from app.chunker import split_into_tts_chunks
from app.db import init_schema
from app.epub_parser import ParsedChapter
from app import text_analysis
from app.normalization import NormalizationOptions, normalize_chapter_titles, normalize_text
from app.validation import (
    analyze_chapter_spans,
    analyze_numbering,
    canonical_chapter_title,
    check_title_format,
    detect_chapter_number,
    find_issue_spans,
    numbering_flags,
    validate_chapter_text,
    validate_chapters,
    validate_patch_plan,
)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    return conn


def _chapters(count: int, *, start: int = 1) -> list[ParsedChapter]:
    return [
        ParsedChapter(title=f"Chương {number}", text=f"Nội dung chương {number}. " * 20)
        for number in range(start, start + count)
    ]


# --- chapter number detection -------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Chương 12: Kỳ vật", 12),
        ("chương 7", 7),
        ("Chapter 41 - The End", 41),
        ("Hồi 3", 3),
        ("135", 135),
        ("135. Mở đầu", 135),
        ("Lời nói đầu", None),
        ("", None),
        (None, None),
    ],
)
def test_detect_chapter_number(title, expected):
    assert detect_chapter_number(title) == expected


def test_analyze_numbering_reports_gaps_and_duplicates():
    reports = [
        validate_chapter_text(chapter_index=index, title=title, text="Nội dung. " * 30)
        for index, title in enumerate(["Chương 1", "Chương 2", "Chương 4", "Chương 4"])
    ]
    numbering = analyze_numbering(reports)

    assert numbering["missing_numbers"] == [3]
    assert numbering["duplicate_count"] == 1
    assert numbering["is_continuous"] is False


# --- chapter validation -------------------------------------------------------


def test_punctuation_only_chapter_is_an_error():
    report = validate_chapter_text(chapter_index=0, title="Chương 1: Mở đầu", text="...  !!!  ???")
    assert report.severity == "error"
    assert [issue.code for issue in report.issues] == ["unspeakable"]


def test_unsplittable_paragraph_is_an_error():
    report = validate_chapter_text(chapter_index=0, title="Chương 1", text="a" * 900, max_chars=400)
    assert "unsplittable_paragraph" in [issue.code for issue in report.issues]
    assert report.severity == "error"


def test_clean_chapter_has_no_blocking_issue():
    report = validate_chapter_text(
        chapter_index=0, title="Chương 1", text="Trời hôm nay rất đẹp. " * 20
    )
    assert report.is_valid
    assert report.chapter_no == 1


def test_duplicate_chapter_text_is_flagged():
    class Row:
        def __init__(self, index, text):
            self.chapter_index = index
            self.title = f"Chương {index + 1}: Tên"
            self.text = text
            self.is_excluded = False

    body = "Cùng một nội dung. " * 20
    reports = validate_chapters([Row(0, body), Row(1, body), Row(2, "Khác hẳn. " * 20)])
    assert [issue.code for issue in reports[0].issues] == ["duplicate_text"]
    assert [issue.code for issue in reports[2].issues] == []


# --- patch validation ---------------------------------------------------------


def test_patch_plan_flags_empty_and_oversized_chunks():
    plan = [{"text": "Xin chào."}, {"text": "   "}, {"text": "b" * 900}]
    report = validate_patch_plan(
        patch_id=1, patch_index=0, chapter_start=0, chapter_end=1, plan=plan, max_chars=400
    )
    codes = {issue.code for issue in report.issues}

    assert codes == {"empty_chunks", "oversized_chunks"}
    assert report.empty_chunks == 1
    assert report.oversized_chunks == 1
    assert report.max_chunk_chars == 900


def test_patch_plan_on_clean_chunks_is_valid():
    plan = [{"text": "Câu một."}, {"text": "Câu hai."}]
    report = validate_patch_plan(
        patch_id=1, patch_index=0, chapter_start=0, chapter_end=0, plan=plan, max_chars=400
    )
    assert report.severity == "ok"
    assert report.chunk_count == 2


# --- the TTS failure this was built for ---------------------------------------


def test_cjk_punctuation_leftovers_are_removed():
    """Chinese source text stripped of ideographs used to leave speakable-free lines."""
    text = "「，，。」\nXin chào các bạn.\n，，，。"
    cleaned = normalize_text(text, NormalizationOptions())

    assert "，" not in cleaned and "。" not in cleaned
    assert "Xin chào các bạn." in cleaned
    for line in cleaned.split("\n"):
        assert not line.strip() or any(character.isalnum() for character in line)


def test_chunker_never_emits_an_oversized_chunk():
    paragraph = "từ " * 400  # no sentence-ending punctuation anywhere
    chunks = split_into_tts_chunks(paragraph, max_chars=200)

    assert chunks
    assert max(len(chunk) for chunk in chunks) <= 200


# --- incremental update -------------------------------------------------------


def _book_with_patches(conn, chapter_count=20, patch_size=10):
    book = repository.create_book(
        conn,
        title="Sách",
        original_filename="sach.epub",
        epub_path="",
        patch_size=patch_size,
        chapters=_chapters(chapter_count),
        background_image_path=None,
    )
    repository.auto_build_patches(conn, book.id, 0, None, patch_size)
    return book


def test_diff_detects_new_chapters_only():
    conn = _conn()
    book = _book_with_patches(conn, chapter_count=20)
    parsed = _chapters(25)  # same 20 chapters plus 5 new ones

    plan = repository.diff_chapters_against_epub(conn, book.id, parsed)

    assert plan["matched_count"] == 20
    assert len(plan["added"]) == 5
    assert plan["removed"] == []
    assert plan["next_chapter_index"] == 20


def test_append_new_chapters_keeps_existing_patches_and_audio():
    conn = _conn()
    book = _book_with_patches(conn, chapter_count=20)
    before = repository.list_patches(conn, book.id)
    conn.execute(
        "UPDATE patch SET status = 'done', audio_path = 'a.wav' WHERE book_id = ?", (book.id,)
    )
    conn.commit()

    result = repository.append_new_chapters(conn, book.id, _chapters(25))

    assert result["inserted"] == 5
    after = repository.list_patches(conn, book.id)
    assert [patch.id for patch in after] == [patch.id for patch in before]
    assert all(patch.status == "done" for patch in after)
    assert len(repository.list_chapters(conn, book.id)) == 25


def test_extend_patches_covers_only_new_chapters():
    conn = _conn()
    book = _book_with_patches(conn, chapter_count=20, patch_size=10)
    repository.append_new_chapters(conn, book.id, _chapters(25))

    assert repository.uncovered_chapter_indices(conn, book.id) == [20, 21, 22, 23, 24]

    created = repository.extend_patches(conn, book.id, patch_size=10)

    assert len(created) == 1
    assert (created[0].chapter_start, created[0].chapter_end) == (20, 24)
    assert created[0].patch_index == 2
    assert len(repository.list_patches(conn, book.id)) == 3
    assert repository.uncovered_chapter_indices(conn, book.id) == []


def test_extend_patches_is_a_no_op_when_everything_is_covered():
    conn = _conn()
    book = _book_with_patches(conn, chapter_count=20)
    assert repository.extend_patches(conn, book.id) == []


def test_changed_chapter_is_not_rewritten_under_a_done_patch():
    conn = _conn()
    book = _book_with_patches(conn, chapter_count=20)
    conn.execute("UPDATE patch SET status = 'done' WHERE book_id = ?", (book.id,))
    conn.commit()

    edited = _chapters(20)
    edited[0] = ParsedChapter(title="Chương 1", text="Nội dung đã sửa hoàn toàn. " * 20)

    result = repository.append_new_chapters(conn, book.id, edited, update_changed=True)

    assert result["updated"] == 0
    assert result["skipped_changed"] == 1
    chapters = repository.list_chapters(conn, book.id)
    assert "đã sửa" not in chapters[0].text


# --- canonical title format ----------------------------------------------------


def test_canonical_title_is_accepted():
    assert check_title_format("Chương 12: Kỳ vật") == ("canonical", 12, "Kỳ vật")


@pytest.mark.parametrize(
    "title",
    [
        "Chương 12 - Kỳ vật",
        "Chương 12 – Kỳ vật",
        "Chapter 12 - The End",
        "12. Mở đầu",
        "chương 12: kỳ vật",
    ],
)
def test_title_variants_are_fixable(title):
    state, number, name = check_title_format(title)
    assert state == "fixable"
    assert number == 12
    assert canonical_chapter_title(number, name).startswith("Chương 12: ")


def test_title_without_name_needs_manual_fix():
    state, number, name = check_title_format("Chương 12")
    assert state == "no_name"
    assert number == 12

    report = validate_chapter_text(chapter_index=0, title="Chương 12", text="Nội dung. " * 30)
    assert "title_missing_name" in [issue.code for issue in report.issues]


def test_title_without_number_is_unknown():
    state, number, name = check_title_format("Lời nói đầu")
    assert state == "unknown"
    assert number is None

    report = validate_chapter_text(chapter_index=0, title="Lời nói đầu", text="Nội dung. " * 30)
    assert "no_chapter_number" in [issue.code for issue in report.issues]


def test_non_canonical_title_carries_a_suggestion():
    report = validate_chapter_text(chapter_index=0, title="Chương 12 - Kỳ vật", text="Nội dung. " * 30)
    assert report.title_state == "fixable"
    assert report.suggested_title == "Chương 12: Kỳ vật"
    assert report.severity == "warning"


def test_numbering_flags_marks_the_break():
    reports = [
        validate_chapter_text(chapter_index=index, title=title, text="Nội dung. " * 30)
        for index, title in enumerate(["Chương 1: A", "Chương 2: B", "Chương 4: C"])
    ]
    flags = numbering_flags(reports)
    assert flags[2] == "gap_before"


# --- positional highlight spans -------------------------------------------------


def test_find_issue_spans_positions_are_exact():
    text = "Xin chào. Xem thêm tại https://example.com nhé. 你好世界。"
    spans = find_issue_spans(text)

    url_span = next(s for s in spans if s.code == "url")
    assert text[url_span.start : url_span.start + url_span.length] == "https://example.com"

    cjk_span = next(s for s in spans if s.code == "cjk_residue")
    assert text[cjk_span.start : cjk_span.start + cjk_span.length] == "你好世界"


def test_spans_are_computed_on_raw_text_not_stripped():
    body = "Xem thêm https://example.com nhé."
    padded = "\n\n\n" + body

    padded_spans = find_issue_spans(padded)
    url_span = next(s for s in padded_spans if s.code == "url")
    assert padded[url_span.start : url_span.start + url_span.length] == "https://example.com"


def test_analyze_chapter_spans_has_no_overlaps():
    text = "你好 https://example.com [tiếng khóc] rất buồn." * 3
    spans = analyze_chapter_spans(text)
    for a, b in zip(spans, spans[1:]):
        assert a.start + a.length <= b.start


def test_analyze_chapter_spans_prefers_the_error_span():
    # The CJK run is both a validation.py ERROR span and a text_analysis "junk" WARNING
    # span (its CJK junk pattern) — the higher-severity one must win.
    text = "Trước đó bình thường. 你好世界 và sau đó cũng bình thường."
    spans = analyze_chapter_spans(text)
    cjk_spans = [s for s in spans if "你好世界" in s.excerpt or (s.start <= text.index("你好世界") < s.start + s.length)]
    assert any(s.code == "cjk_residue" and s.severity == "error" for s in cjk_spans)
    assert not any(s.code == "junk" for s in cjk_spans)


# --- repository: chapter read/write ---------------------------------------------


def test_update_chapter_recomputes_metadata():
    conn = _conn()
    book = _book_with_patches(conn, chapter_count=5)

    new_text = "Nội dung hoàn toàn mới. " * 10
    assert repository.update_chapter(conn, book.id, 0, title="Chương 1: Mới", text=new_text)

    chapter = repository.get_chapter(conn, book.id, 0)
    assert chapter.title == "Chương 1: Mới"
    assert chapter.char_count == len(new_text)
    assert chapter.chapter_no == 1
    assert chapter.text_hash == text_analysis.text_hash(new_text)


def test_update_chapter_keeps_reimport_matching_stable():
    conn = _conn()
    book = _book_with_patches(conn, chapter_count=5)

    new_text = "Nội dung đã chỉnh sửa và ổn định. " * 10
    repository.update_chapter(conn, book.id, 0, text=new_text)

    parsed = _chapters(5)
    parsed[0] = ParsedChapter(title="Chương 1", text=new_text)
    plan = repository.diff_chapters_against_epub(conn, book.id, parsed)

    assert plan["matched_count"] == 5
    assert plan["changed"] == []


def test_set_chapter_titles_bulk():
    conn = _conn()
    book = _book_with_patches(conn, chapter_count=3)

    written = repository.set_chapter_titles(
        conn, book.id, [(0, "Chương 1: Mới"), (1, "Chương 2: Cũng mới")]
    )
    assert written == 2

    chapters = {c.chapter_index: c for c in repository.list_chapters(conn, book.id)}
    assert chapters[0].title == "Chương 1: Mới"
    assert chapters[0].chapter_no == 1
    assert chapters[1].title == "Chương 2: Cũng mới"
    assert chapters[2].title == "Chương 3"  # untouched


def test_list_patches_covering_chapter():
    conn = _conn()
    book = _book_with_patches(conn, chapter_count=20, patch_size=10)

    patches = repository.list_patches_covering_chapter(conn, book.id, 12)
    assert len(patches) == 1
    assert patches[0].patch_index == 1


# --- normalization: canonical titles must still speak the number ----------------


def test_normalize_chapter_titles_handles_canonical_titles():
    text = "Chương 12: Bão\n\nNội dung chương."
    normalized = normalize_chapter_titles(text)

    assert "mười hai" in normalized
    assert "Bão" in normalized
    assert "Chương 12:" not in normalized
