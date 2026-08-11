"""Validate ebook data before it reaches TTS.

Two levels, mirroring how the pipeline consumes the book:

* chapter level — what came out of the EPUB parser and lives in the ``chapter`` table,
* patch level  — the TTS chunk plan a patch will actually speak.

Everything here is pure: callers pass in already-loaded rows/chunks so the checks can run
off the database lock.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field

# Severity ordering matters for rollups: error > warning > info.
ERROR = "error"
WARNING = "warning"
INFO = "info"
_SEVERITY_RANK = {INFO: 0, WARNING: 1, ERROR: 2}

# A chapter shorter than this is almost never real content (stray heading, image caption).
MIN_CHAPTER_CHARS = 120
# edge-tts and VoxCPM both choke on a single chunk this long; the chunker only guarantees
# the limit when a sentence boundary exists, so an unsplittable sentence can exceed it.
CHUNK_HARD_LIMIT_RATIO = 1.5

_CJK_RE = re.compile(r"[一-鿿㐀-䶿豈-﫿]")
_HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]{0,80}>")
_HTML_ENTITY_RE = re.compile(r"&(?:[a-zA-Z]{2,10}|#\d{2,5});")
_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_ZERO_WIDTH_RE = re.compile(r"[​-‏  ﻿]")
_REPLACEMENT_RE = re.compile(r"�")
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?…]")
# Speakable = letter or digit in any script. Punctuation/symbols alone make edge-tts
# return "No audio was received".
_SPEAKABLE_RE = re.compile(r"[^\W_]", re.UNICODE)

_CHAPTER_NUMBER_PATTERNS = [
    re.compile(r"^\s*(?:chương|chuong|chapter|hồi|hoi|phần|phan|quyển|quyen|tập|tap)\s*[:\-–—]?\s*(\d{1,5})\b", re.IGNORECASE),
    # "135." / "135 - Tên" and a title that is nothing but the number ("135").
    re.compile(r"^\s*(\d{1,5})\s*(?:[.:\-–—]|$)"),
]

# Canonical shape the user wants every chapter title to have: "Chương N: Tên chương".
CANONICAL_TITLE_RE = re.compile(r"^Chương\s+(\d{1,5}):\s+(\S.*?)\s*$")

TITLE_CANONICAL = "canonical"  # already "Chương N: Tên"
TITLE_FIXABLE = "fixable"      # number + name present, wrong shape -> can auto-rewrite
TITLE_NO_NAME = "no_name"      # only a number, nothing to put after the colon -> manual fix
TITLE_UNKNOWN = "unknown"      # no chapter number detectable at all -> manual fix


@dataclass
class Issue:
    code: str
    severity: str
    message: str
    count: int = 1

    def as_dict(self) -> dict:
        return asdict(self)


def _control_char_count(text: str) -> int:
    return sum(1 for ch in text if unicodedata.category(ch) == "Cc" and ch not in "\n\t\r")


def detect_chapter_number(title: str | None) -> int | None:
    """Return the chapter number written in the title, or None when it has none.

    Handles "Chương 12", "Chương 12: Tên", "Chapter 12 - Tên", "12. Tên", "Hồi 12".
    """
    if not title:
        return None
    for pattern in _CHAPTER_NUMBER_PATTERNS:
        match = pattern.search(title)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def split_chapter_title(title: str | None) -> tuple[int | None, str]:
    """Return (chapter_no, name) parsed out of any title shape.

    Reuses ``_CHAPTER_NUMBER_PATTERNS`` to find the number, then treats whatever comes
    after the matched prefix (stripped of leading separators) as the chapter name.
    """
    if not title:
        return None, ""
    for pattern in _CHAPTER_NUMBER_PATTERNS:
        match = pattern.search(title)
        if not match:
            continue
        try:
            number = int(match.group(1))
        except ValueError:
            return None, ""
        rest = title[match.end():].strip()
        rest = re.sub(r"^[:\-–—.\s]+", "", rest).strip()
        return number, rest
    return None, ""


def canonical_chapter_title(number: int, name: str) -> str:
    return f"Chương {number}: {name.strip()}"


def check_title_format(title: str | None) -> tuple[str, int | None, str]:
    """Classify a chapter title against the canonical "Chương N: Tên" shape.

    Returns (state, chapter_no, name) where state is one of the TITLE_* constants.
    """
    stripped = (title or "").strip()
    if CANONICAL_TITLE_RE.match(stripped):
        match = CANONICAL_TITLE_RE.match(stripped)
        return TITLE_CANONICAL, int(match.group(1)), match.group(2)

    number, name = split_chapter_title(stripped)
    if number is None:
        return TITLE_UNKNOWN, None, ""
    if not name:
        return TITLE_NO_NAME, number, ""
    return TITLE_FIXABLE, number, name


def _worst(issues: list[Issue]) -> str:
    if not issues:
        return "ok"
    return max(issues, key=lambda issue: _SEVERITY_RANK.get(issue.severity, 0)).severity


@dataclass
class ChapterReport:
    chapter_index: int
    title: str
    chapter_no: int | None
    char_count: int
    is_excluded: bool
    issues: list[Issue] = field(default_factory=list)
    title_state: str = TITLE_UNKNOWN
    suggested_title: str | None = None

    @property
    def severity(self) -> str:
        return _worst(self.issues)

    @property
    def is_valid(self) -> bool:
        return self.severity != ERROR

    def as_dict(self) -> dict:
        return {
            "chapter_index": self.chapter_index,
            "title": self.title,
            "chapter_no": self.chapter_no,
            "char_count": self.char_count,
            "is_excluded": self.is_excluded,
            "severity": self.severity,
            "is_valid": self.is_valid,
            "issues": [issue.as_dict() for issue in self.issues],
            "title_state": self.title_state,
            "suggested_title": self.suggested_title,
        }


def validate_chapter_text(
    *,
    chapter_index: int,
    title: str | None,
    text: str,
    is_excluded: bool = False,
    max_chars: int = 400,
) -> ChapterReport:
    """Check one chapter for anything that makes TTS fail or read nonsense."""
    issues: list[Issue] = []
    stripped = (text or "").strip()

    if not stripped:
        issues.append(Issue("empty", ERROR, "Chương không có nội dung."))
    elif not _SPEAKABLE_RE.search(stripped):
        issues.append(Issue("unspeakable", ERROR, "Chương chỉ có dấu câu/ký hiệu, TTS sẽ trả về audio rỗng."))
    elif len(stripped) < MIN_CHAPTER_CHARS:
        issues.append(
            Issue("too_short", WARNING, f"Chỉ {len(stripped)} ký tự — có thể là bìa, mục lục hoặc chú thích ảnh.")
        )

    cjk = len(_CJK_RE.findall(stripped))
    if cjk:
        issues.append(Issue("cjk_residue", ERROR, f"Còn {cjk} ký tự Trung/Nhật/Hàn chưa được lọc.", cjk))

    replacements = len(_REPLACEMENT_RE.findall(stripped))
    if replacements:
        issues.append(Issue("mojibake", ERROR, f"Có {replacements} ký tự lỗi mã hoá (U+FFFD).", replacements))

    controls = _control_char_count(stripped)
    if controls:
        issues.append(Issue("control_chars", ERROR, f"Có {controls} ký tự điều khiển.", controls))

    tags = len(_HTML_TAG_RE.findall(stripped))
    entities = len(_HTML_ENTITY_RE.findall(stripped))
    if tags or entities:
        issues.append(
            Issue("html_residue", WARNING, f"Còn {tags} thẻ HTML và {entities} entity chưa được bóc.", tags + entities)
        )

    zero_width = len(_ZERO_WIDTH_RE.findall(stripped))
    if zero_width:
        issues.append(Issue("zero_width", WARNING, f"Có {zero_width} ký tự vô hình (zero-width).", zero_width))

    urls = len(_URL_RE.findall(stripped))
    if urls:
        issues.append(Issue("url", WARNING, f"Có {urls} đường link sẽ bị đọc thành từng ký tự.", urls))

    # The chunker only splits at sentence boundaries; a paragraph with none stays whole.
    hard_limit = int(max_chars * CHUNK_HARD_LIMIT_RATIO)
    unsplittable = [
        paragraph
        for paragraph in stripped.split("\n\n")
        if len(paragraph) > hard_limit and not _SENTENCE_BOUNDARY_RE.search(paragraph)
    ]
    if unsplittable:
        longest = max(len(p) for p in unsplittable)
        issues.append(
            Issue(
                "unsplittable_paragraph",
                ERROR,
                f"{len(unsplittable)} đoạn dài tới {longest} ký tự không có dấu kết câu — chunk sẽ vượt giới hạn {max_chars}.",
                len(unsplittable),
            )
        )

    title_state, chapter_no, name = check_title_format(title)
    suggested_title: str | None = None
    if title_state == TITLE_UNKNOWN and not is_excluded:
        issues.append(Issue("no_chapter_number", WARNING, "Tiêu đề không có số chương."))
    elif title_state == TITLE_NO_NAME and not is_excluded:
        issues.append(Issue("title_missing_name", WARNING, "Tiêu đề chỉ có số chương, thiếu tên chương."))
    elif title_state == TITLE_FIXABLE:
        suggested_title = canonical_chapter_title(chapter_no, name)
        issues.append(
            Issue(
                "title_not_canonical",
                WARNING,
                f'Tiêu đề chưa đúng định dạng "Chương N: Tên" — gợi ý: "{suggested_title}".',
            )
        )

    return ChapterReport(
        chapter_index=chapter_index,
        title=(title or "").strip(),
        chapter_no=chapter_no,
        char_count=len(stripped),
        is_excluded=is_excluded,
        issues=issues,
        title_state=title_state,
        suggested_title=suggested_title,
    )


def analyze_numbering(reports: list[ChapterReport]) -> dict:
    """Look at detected chapter numbers as a sequence: gaps, duplicates, order.

    This is what tells you an EPUB is missing chapters or has the same chapter twice —
    both of which produce a book that reads fine per-chapter but is wrong as a whole.
    """
    numbered = [report for report in reports if report.chapter_no is not None and not report.is_excluded]
    numbers = [report.chapter_no for report in numbered]

    duplicates: dict[int, list[int]] = {}
    seen: dict[int, int] = {}
    for report in numbered:
        if report.chapter_no in seen:
            duplicates.setdefault(report.chapter_no, [seen[report.chapter_no]]).append(report.chapter_index)
        else:
            seen[report.chapter_no] = report.chapter_index

    missing: list[int] = []
    out_of_order: list[int] = []
    if numbers:
        expected = set(range(min(numbers), max(numbers) + 1))
        missing = sorted(expected - set(numbers))
        for previous, current in zip(numbered, numbered[1:]):
            if current.chapter_no < previous.chapter_no:
                out_of_order.append(current.chapter_index)

    return {
        "numbered_count": len(numbered),
        "unnumbered_count": len(reports) - len(numbered),
        "first_number": min(numbers) if numbers else None,
        "last_number": max(numbers) if numbers else None,
        "missing_numbers": missing[:200],
        "missing_count": len(missing),
        "duplicate_numbers": {str(number): indices for number, indices in sorted(duplicates.items())},
        "duplicate_count": len(duplicates),
        "out_of_order_indices": out_of_order[:200],
        "is_continuous": not missing and not duplicates and not out_of_order,
    }


def numbering_flags(reports: list[ChapterReport]) -> dict[int, str]:
    """Per-chapter continuity flag: chapter_index -> "gap_before" | "duplicate" |
    "out_of_order" | "unnumbered". Lets the UI draw the exact row where a break happens
    instead of only a book-level summary.
    """
    flags: dict[int, str] = {}
    numbered = [report for report in reports if report.chapter_no is not None and not report.is_excluded]

    seen: dict[int, int] = {}
    for report in numbered:
        if report.chapter_no in seen:
            flags[report.chapter_index] = "duplicate"
        else:
            seen[report.chapter_no] = report.chapter_index

    for previous, current in zip(numbered, numbered[1:]):
        if current.chapter_index in flags:
            continue
        if current.chapter_no < previous.chapter_no:
            flags[current.chapter_index] = "out_of_order"
        elif current.chapter_no > previous.chapter_no + 1:
            flags[current.chapter_index] = "gap_before"

    for report in reports:
        if report.chapter_no is None and not report.is_excluded and report.chapter_index not in flags:
            flags[report.chapter_index] = "unnumbered"

    return flags


def validate_chapters(chapters, *, max_chars: int = 400) -> list[ChapterReport]:
    """Validate every chapter row, then flag chapters whose text is byte-identical."""
    reports = [
        validate_chapter_text(
            chapter_index=chapter.chapter_index,
            title=chapter.title,
            text=chapter.text,
            is_excluded=bool(getattr(chapter, "is_excluded", False)),
            max_chars=max_chars,
        )
        for chapter in chapters
    ]

    by_text: dict[int, list[int]] = {}
    for chapter in chapters:
        by_text.setdefault(hash((chapter.text or "").strip()), []).append(chapter.chapter_index)
    duplicated = {index for indices in by_text.values() if len(indices) > 1 for index in indices}
    for report in reports:
        if report.chapter_index in duplicated:
            report.issues.append(Issue("duplicate_text", WARNING, "Nội dung trùng với một chương khác."))

    return reports


@dataclass
class PatchReport:
    patch_id: int
    patch_index: int
    chapter_start: int
    chapter_end: int
    chunk_count: int
    total_chars: int
    max_chunk_chars: int
    oversized_chunks: int
    empty_chunks: int
    unspeakable_chunks: int
    chapter_count: int
    invalid_chapters: list[int]
    issues: list[Issue] = field(default_factory=list)

    @property
    def severity(self) -> str:
        return _worst(self.issues)

    def as_dict(self) -> dict:
        return {
            "patch_id": self.patch_id,
            "patch_index": self.patch_index,
            "chapter_start": self.chapter_start,
            "chapter_end": self.chapter_end,
            "chunk_count": self.chunk_count,
            "total_chars": self.total_chars,
            "max_chunk_chars": self.max_chunk_chars,
            "oversized_chunks": self.oversized_chunks,
            "empty_chunks": self.empty_chunks,
            "unspeakable_chunks": self.unspeakable_chunks,
            "chapter_count": self.chapter_count,
            "invalid_chapters": self.invalid_chapters,
            "severity": self.severity,
            "is_valid": self.severity != ERROR,
            "issues": [issue.as_dict() for issue in self.issues],
        }


def validate_patch_plan(
    *,
    patch_id: int,
    patch_index: int,
    chapter_start: int,
    chapter_end: int,
    plan: list[dict],
    max_chars: int,
    chapter_reports: list[ChapterReport] | None = None,
) -> PatchReport:
    """Validate the exact chunk list a patch would send to TTS."""
    texts = [(chunk.get("text") or "") for chunk in plan]
    lengths = [len(text) for text in texts]
    hard_limit = int(max_chars * CHUNK_HARD_LIMIT_RATIO)

    empty = sum(1 for text in texts if not text.strip())
    unspeakable = sum(1 for text in texts if text.strip() and not _SPEAKABLE_RE.search(text))
    oversized = sum(1 for length in lengths if length > hard_limit)

    covered = [
        report
        for report in (chapter_reports or [])
        if chapter_start <= report.chapter_index <= chapter_end and not report.is_excluded
    ]
    invalid = [report.chapter_index for report in covered if not report.is_valid]

    issues: list[Issue] = []
    if not plan:
        issues.append(Issue("no_chunks", ERROR, "Patch không sinh được chunk nào để đọc."))
    if empty:
        issues.append(Issue("empty_chunks", ERROR, f"{empty} chunk rỗng sau khi chuẩn hoá.", empty))
    if unspeakable:
        issues.append(
            Issue("unspeakable_chunks", ERROR, f"{unspeakable} chunk chỉ có dấu câu — TTS sẽ báo lỗi.", unspeakable)
        )
    if oversized:
        issues.append(
            Issue("oversized_chunks", ERROR, f"{oversized} chunk vượt {hard_limit} ký tự (giới hạn {max_chars}).", oversized)
        )
    if invalid:
        issues.append(Issue("invalid_chapters", ERROR, f"{len(invalid)} chương trong patch có lỗi.", len(invalid)))

    return PatchReport(
        patch_id=patch_id,
        patch_index=patch_index,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
        chunk_count=len(plan),
        total_chars=sum(lengths),
        max_chunk_chars=max(lengths) if lengths else 0,
        oversized_chunks=oversized,
        empty_chunks=empty,
        unspeakable_chunks=unspeakable,
        chapter_count=len(covered),
        invalid_chapters=invalid[:50],
        issues=issues,
    )


def summarize(chapter_reports: list[ChapterReport], patch_reports: list[PatchReport]) -> dict:
    """Roll chapter + patch reports into the counters the UI shows at the top."""
    issue_totals: dict[str, int] = {}
    for report in chapter_reports:
        for issue in report.issues:
            issue_totals[issue.code] = issue_totals.get(issue.code, 0) + 1

    return {
        "chapters_total": len(chapter_reports),
        "chapters_error": sum(1 for report in chapter_reports if report.severity == ERROR),
        "chapters_warning": sum(1 for report in chapter_reports if report.severity == WARNING),
        "chapters_ok": sum(1 for report in chapter_reports if report.severity in ("ok", INFO)),
        "chapters_excluded": sum(1 for report in chapter_reports if report.is_excluded),
        "patches_total": len(patch_reports),
        "patches_error": sum(1 for report in patch_reports if report.severity == ERROR),
        "patches_warning": sum(1 for report in patch_reports if report.severity == WARNING),
        "issue_totals": issue_totals,
    }


# ---------------------------------------------------------------------------
# Positional spans — the source for the "highlight the exact bad text" view.
# ---------------------------------------------------------------------------

_SOFT_SPAN_META = {
    "junk": (WARNING, "Ký tự rác"),
    "spell_vi": (INFO, "Từ nghi sai chính tả"),
    "effect_marker": (INFO, "Đánh dấu hiệu ứng"),
    "sound_desc": (INFO, "Mô tả âm thanh"),
}

_HARD_SPAN_META = {
    "cjk_residue": (ERROR, "Ký tự Trung/Nhật/Hàn"),
    "mojibake": (ERROR, "Lỗi mã hoá"),
    "control_chars": (ERROR, "Ký tự điều khiển"),
    "unsplittable_paragraph": (ERROR, "Đoạn không có dấu kết câu"),
    "html_residue": (WARNING, "Thẻ HTML còn sót"),
    "zero_width": (WARNING, "Ký tự vô hình"),
    "url": (WARNING, "Đường link"),
}


@dataclass
class Span:
    start: int
    length: int
    code: str
    severity: str
    label: str
    excerpt: str

    def as_dict(self) -> dict:
        return asdict(self)


def _merge_runs(matches: list[re.Match]) -> list[tuple[int, int]]:
    """Merge adjacent/overlapping single-char matches into runs, so 5 CJK chars in a
    row become one span instead of five."""
    runs: list[tuple[int, int]] = []
    for match in matches:
        start, end = match.start(), match.end()
        if runs and start <= runs[-1][1]:
            runs[-1] = (runs[-1][0], max(runs[-1][1], end))
        else:
            runs.append((start, end))
    return runs


def _make_span(text: str, start: int, end: int, code: str) -> Span:
    severity, label = _HARD_SPAN_META[code]
    excerpt = text[start:end]
    if len(excerpt) > 60:
        excerpt = excerpt[:60] + "…"
    return Span(start=start, length=end - start, code=code, severity=severity, label=label, excerpt=excerpt)


def find_issue_spans(text: str, *, max_chars: int = 400) -> list[Span]:
    """Positional version of the hard TTS-breaking checks, over the RAW (unstripped)
    text — offsets must line up with what the client renders, so this must NOT strip().
    """
    text = text or ""
    spans: list[Span] = []

    for start, end in _merge_runs(list(_CJK_RE.finditer(text))):
        spans.append(_make_span(text, start, end, "cjk_residue"))
    for start, end in _merge_runs(list(_REPLACEMENT_RE.finditer(text))):
        spans.append(_make_span(text, start, end, "mojibake"))

    control_matches = [
        m for m in re.finditer(r".", text, re.DOTALL)
        if unicodedata.category(m.group()) == "Cc" and m.group() not in "\n\t\r"
    ]
    for start, end in _merge_runs(control_matches):
        spans.append(_make_span(text, start, end, "control_chars"))

    for match in _HTML_TAG_RE.finditer(text):
        spans.append(_make_span(text, match.start(), match.end(), "html_residue"))
    for match in _HTML_ENTITY_RE.finditer(text):
        spans.append(_make_span(text, match.start(), match.end(), "html_residue"))
    for start, end in _merge_runs(list(_ZERO_WIDTH_RE.finditer(text))):
        spans.append(_make_span(text, start, end, "zero_width"))
    for match in _URL_RE.finditer(text):
        spans.append(_make_span(text, match.start(), match.end(), "url"))

    hard_limit = int(max_chars * CHUNK_HARD_LIMIT_RATIO)
    cursor = 0
    for paragraph in text.split("\n\n"):
        start = cursor
        end = start + len(paragraph)
        if len(paragraph) > hard_limit and not _SENTENCE_BOUNDARY_RE.search(paragraph):
            spans.append(_make_span(text, start, end, "unsplittable_paragraph"))
        cursor = end + 2  # account for the "\n\n" separator consumed by split()

    return spans


def _dedupe_spans(spans: list[Span]) -> list[Span]:
    """Flatten possibly-overlapping spans into a non-overlapping, start-ordered list.
    Highest severity wins; among equal severity the longer span wins."""
    ordered = sorted(spans, key=lambda s: (-_SEVERITY_RANK.get(s.severity, 0), -s.length, s.start))
    kept: list[Span] = []
    occupied: list[tuple[int, int]] = []
    for span in ordered:
        end = span.start + span.length
        if any(span.start < o_end and o_start < end for o_start, o_end in occupied):
            continue
        kept.append(span)
        occupied.append((span.start, end))
    return sorted(kept, key=lambda s: s.start)


def analyze_chapter_spans(
    text: str, *, max_chars: int = 400, include_style: bool = True, limit: int = 800
) -> list[Span]:
    """Every highlightable TTS risk in one chapter: the hard checks from this module
    plus the soft style checks from ``app.text_analysis``, deduped and sorted."""
    from app.text_analysis import analyze_text  # local import: avoids a module cycle at import time

    text = text or ""
    spans = find_issue_spans(text, max_chars=max_chars)

    if include_style:
        for warning in analyze_text(text):
            code = warning.get("kind", "")
            meta = _SOFT_SPAN_META.get(code)
            if not meta:
                continue
            severity, label = meta
            start = warning.get("position", 0)
            length = warning.get("length", 0)
            if length <= 0:
                continue
            excerpt = warning.get("original") or text[start:start + length]
            if len(excerpt) > 60:
                excerpt = excerpt[:60] + "…"
            spans.append(Span(start=start, length=length, code=code, severity=severity, label=label, excerpt=excerpt))

    deduped = _dedupe_spans(spans)
    return deduped[:limit]


def summarize_spans(spans: list[Span]) -> dict[str, int]:
    totals: dict[str, int] = {}
    for span in spans:
        totals[span.code] = totals.get(span.code, 0) + 1
    return totals
