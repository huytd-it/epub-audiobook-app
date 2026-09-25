"""Extract ordered chapter text from an EPUB file."""
from __future__ import annotations

import logging
import posixpath
import re
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

import warnings

import ebooklib
from bs4 import BeautifulSoup, NavigableString, XMLParsedAsHTMLWarning
from ebooklib import epub

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

logger = logging.getLogger(__name__)

_HEADING_TAGS = ("h1", "h2", "h3")
_MIN_CHAPTER_CHARS = 50  # below this, treat the spine doc as cover/nav, not a chapter
_SPLIT_MARKER = "\x00CHAPTER_SPLIT\x00"
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")
# Ideographs plus CJK punctuation/fullwidth forms \u2014 stripping only the ideographs leaves
# behind lines of bare \uff0c\u3002\u300c\u300d that TTS cannot speak.
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\ufe30-\ufe4f\uff01-\uff65]+")
_OPF_NS = "http://www.idpf.org/2007/opf"
_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"

# TOC detection thresholds (see design.md, decision 1).
_TOC_MEAN_LINE_LEN = 40      # mean non-blank line length below this is suspicious
_TOC_SHORT_LINE_LEN = 50     # lines shorter than this count as "short"
_TOC_SHORT_LINE_RATIO = 0.7  # fraction of non-blank lines that are "short"


@dataclass
class ParsedChapter:
    title: str
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class EpubMetadata:
    """Descriptive metadata read from the EPUB's OPF (Dublin Core).

    Kept separate from chapter text: this is what lets AI generation produce
    content that actually matches the book (real title/author/genre/synopsis
    instead of guessing from the filename). All fields are plain strings
    (subjects is a list) and safe to persist directly on the book row.
    """

    title: str = ""
    creator: str = ""  # author — Dublin Core uses "creator"
    language: str = ""
    publisher: str = ""
    description: str = ""
    subjects: list[str] | None = None
    identifier: str = ""
    date: str = ""

    def __post_init__(self) -> None:
        if self.subjects is None:
            self.subjects = []

    def as_dict(self) -> dict:
        return {
            "title": self.title,
            "creator": self.creator,
            "language": self.language,
            "publisher": self.publisher,
            "description": self.description,
            "subjects": list(self.subjects or []),
            "identifier": self.identifier,
            "date": self.date,
        }


def _remove_cjk(text: str) -> str:
    return _CJK_RE.sub("", text)


def _clean_text(raw: str) -> str:
    text = _WHITESPACE_RE.sub(" ", raw)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    text = _remove_cjk(text)
    return text.strip()


def _find_opf_path(zf: zipfile.ZipFile) -> str:
    container = ET.fromstring(zf.read("META-INF/container.xml"))
    rootfile = container.find(f".//{_CONTAINER_NS}rootfile")
    return rootfile.get("full-path")


def _sanitize_epub(path: str) -> str:
    """Return a path to a usable epub: strips manifest <item> entries that reference files
    missing from the zip (e.g. a deleted cover image), which otherwise crash ebooklib's loader.
    Returns the original path unchanged if nothing needed fixing.
    """
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        opf_path = _find_opf_path(zf)
        opf_dir = posixpath.dirname(opf_path)
        opf_bytes = zf.read(opf_path)

        ET.register_namespace("", _OPF_NS)
        root = ET.fromstring(opf_bytes)
        manifest = root.find(f"{{{_OPF_NS}}}manifest")
        if manifest is None:
            return path

        missing_ids = set()
        for item in list(manifest.findall(f"{{{_OPF_NS}}}item")):
            href = item.get("href", "")
            resolved = posixpath.normpath(posixpath.join(opf_dir, href.split("#")[0])) if opf_dir else href.split("#")[0]
            if resolved not in names:
                missing_ids.add(item.get("id"))
                manifest.remove(item)

        if not missing_ids:
            return path

        spine = root.find(f"{{{_OPF_NS}}}spine")
        if spine is not None:
            for itemref in list(spine.findall(f"{{{_OPF_NS}}}itemref")):
                if itemref.get("idref") in missing_ids:
                    spine.remove(itemref)

        fixed_opf = ET.tostring(root, encoding="utf-8", xml_declaration=True)

        tmp_path = tempfile.mktemp(suffix=".epub")
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as dst:
            for info in src.infolist():
                data = fixed_opf if info.filename == opf_path else src.read(info.filename)
                dst.writestr(info, data)
        return tmp_path


def _spine_documents(book: epub.EpubBook) -> list:
    """Return spine document items in reading order (spine order, not manifest order)."""
    items_by_id = {item.get_id(): item for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)}
    ordered = []
    for idref, _linear in book.spine:
        item = items_by_id.get(idref)
        if item is not None:
            ordered.append(item)
    return ordered


def _split_by_headings(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """Split a single spine document into one or more (title, text) chapters at heading boundaries."""
    headings = soup.find_all(_HEADING_TAGS)
    if len(headings) <= 1:
        title = _remove_cjk(headings[0].get_text(strip=True)) if headings else ""
        text = _clean_text(soup.get_text(separator="\n"))
        return [(title, text)]

    titles = [_remove_cjk(h.get_text(strip=True)) for h in headings]
    for heading in headings:
        heading.insert_before(NavigableString(_SPLIT_MARKER))

    full_text = soup.get_text(separator="\n")
    # parts[0] is content before the first heading (junk/empty); parts[1:] map 1:1 to headings.
    parts = full_text.split(_SPLIT_MARKER)[1:]

    return [(title, _clean_text(part)) for title, part in zip(titles, parts)]


def _is_toc_chapter(chapter: ParsedChapter) -> bool:
    """Heuristic: a chapter is a TOC if its non-blank lines are mostly short.

    Returns True when either:
      - mean non-blank line length < _TOC_MEAN_LINE_LEN, OR
      - more than _TOC_SHORT_LINE_RATIO of non-blank lines are shorter than _TOC_SHORT_LINE_LEN.
    A chapter with very few non-blank lines (< 5) is treated as not-a-TOC (too little signal).
    """
    lines = [ln for ln in chapter.text.split("\n") if ln.strip()]
    if len(lines) < 5:
        return False
    lengths = [len(ln) for ln in lines]
    mean_len = sum(lengths) / len(lengths)
    short_ratio = sum(1 for n in lengths if n < _TOC_SHORT_LINE_LEN) / len(lengths)
    return mean_len < _TOC_MEAN_LINE_LEN or short_ratio > _TOC_SHORT_LINE_RATIO


def _dc_first(book, name: str) -> str:
    """First Dublin Core value for `name` (title/creator/language/...), or ""."""
    try:
        values = book.get_metadata("DC", name) or []
    except Exception:
        return ""
    for value, _attrs in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _dc_all(book, name: str) -> list[str]:
    """All non-empty Dublin Core values for `name` (used for multi-value subject)."""
    try:
        values = book.get_metadata("DC", name) or []
    except Exception:
        return []
    seen: list[str] = []
    for value, _attrs in values:
        text = str(value or "").strip()
        if text and text not in seen:
            seen.append(text)
    return seen


def extract_epub_metadata(path: str) -> EpubMetadata:
    """Read descriptive metadata (title/author/language/...) from an EPUB file.

    Never raises: a broken or metadata-less file yields an empty EpubMetadata
    so the upload flow can always fall back to filename/chapter heuristics.
    """
    try:
        sanitized_path = _sanitize_epub(path)
    except Exception:
        return EpubMetadata()
    try:
        try:
            book = epub.read_epub(sanitized_path, options={"ignore_ncx": True})
        except Exception:
            logger.warning("extract_epub_metadata: cannot read %s", path, exc_info=True)
            return EpubMetadata()
        subjects = _dc_all(book, "subject")
        # Some converters join several genres into one "a, b" subject entry.
        expanded: list[str] = []
        for entry in subjects:
            for part in re.split(r"[;,/|]", entry):
                part = part.strip()
                if part and part not in expanded:
                    expanded.append(part)
        return EpubMetadata(
            title=_dc_first(book, "title"),
            creator=_dc_first(book, "creator"),
            language=_dc_first(book, "language"),
            publisher=_dc_first(book, "publisher"),
            description=_clean_text(_dc_first(book, "description"))[:2000],
            subjects=expanded[:20],
            identifier=_dc_first(book, "identifier"),
            date=_dc_first(book, "date"),
        )
    finally:
        if sanitized_path != path:
            try:
                Path(sanitized_path).unlink(missing_ok=True)
            except OSError:
                pass


def extract_epub_cover(path: str) -> tuple[bytes, str] | None:
    """Return (image_bytes, extension) of the EPUB cover, or None.

    Strategy: ebooklib-flagged cover item first, then a filename heuristic
    (cover/cover-image/bia...), preferring the largest image as a last resort.
    Never raises — returns None when no usable image is found.
    """
    try:
        sanitized_path = _sanitize_epub(path)
    except Exception:
        return None
    try:
        try:
            book = epub.read_epub(sanitized_path, options={"ignore_ncx": True})
        except Exception:
            return None
        import ebooklib as _ebooklib

        images = list(book.get_items_of_type(_ebooklib.ITEM_IMAGE))
        if not images:
            return None

        def _ext(item) -> str:
            name = (item.get_name() or "").lower()
            for ext in (".jpg", ".jpeg", ".png", ".webp"):
                if name.endswith(ext):
                    return ".jpg" if ext == ".jpeg" else ext
            media = (item.media_type or "").lower()
            if "png" in media:
                return ".png"
            if "webp" in media:
                return ".webp"
            return ".jpg"

        # 1. Explicit cover id / properties.
        for item in images:
            item_id = (item.get_id() or "").lower()
            name = (item.get_name() or "").lower()
            if item_id in {"cover", "cover-image", "cover_image"} or "cover" in item_id or "cover" in name:
                try:
                    content = item.get_content()
                except Exception:
                    continue
                if content:
                    return bytes(content), _ext(item)
        # 2. Filename heuristic for non-English covers (bia = Vietnamese "cover").
        for item in images:
            name = (item.get_name() or "").lower()
            if any(key in name for key in ("bia", "front", "titlepage")):
                try:
                    content = item.get_content()
                except Exception:
                    continue
                if content:
                    return bytes(content), _ext(item)
        # 3. Largest image — usually the cover in novels.
        best = None
        best_len = 0
        for item in images:
            try:
                content = item.get_content()
            except Exception:
                continue
            if content and len(content) > best_len:
                best, best_len = item, len(content)
        if best is not None and best_len > 0:
            try:
                return bytes(best.get_content()), _ext(best)
            except Exception:
                return None
        return None
    finally:
        if sanitized_path != path:
            try:
                Path(sanitized_path).unlink(missing_ok=True)
            except OSError:
                pass


def parse_epub(path: str, *, skip_toc: bool = True) -> list[ParsedChapter]:
    """Parse an EPUB file into an ordered list of chapters (spine order).

    When skip_toc is True (default), the leading chapter is dropped if it looks like a
    table of contents (see _is_toc_chapter). An empty-result guard returns the original
    list if filtering would yield no chapters, so a single-chapter book is never emptied.
    """
    sanitized_path = _sanitize_epub(path)
    try:
        book = epub.read_epub(sanitized_path, options={"ignore_ncx": True})
        chapters: list[ParsedChapter] = []

        for item in _spine_documents(book):
            soup = BeautifulSoup(item.get_content(), "lxml")
            for tag in soup.find_all(("script", "style")):
                tag.decompose()

            # Some EPUBs (common in web-novel converters) put the real chapter title
            # only in <head><title>, with no h1-h3 in the body — falling straight to
            # "Chapter N" would silently discard it. ebooklib's get_content() rebuilds
            # <head> from item.title (usually empty on read) and drops the original, so
            # the raw item.content is the only place the real <title> survives.
            doc_title = ""
            raw_title_tag = BeautifulSoup(item.content, "lxml").title
            if raw_title_tag:
                doc_title = _remove_cjk(raw_title_tag.get_text(strip=True))

            for title, text in _split_by_headings(soup):
                if len(text) < _MIN_CHAPTER_CHARS:
                    continue
                if not title:
                    title = doc_title or f"Chapter {len(chapters) + 1}"
                chapters.append(ParsedChapter(title=title, text=text))

        if skip_toc and chapters and _is_toc_chapter(chapters[0]):
            skipped = chapters[0]
            filtered = chapters[1:]
            if filtered:  # empty-result guard: keep the only chapter rather than return []
                logger.info("toc-filter: skipped 1 chapter (was %r)", skipped.title)
                return filtered
            logger.info("toc-filter: would have skipped the only chapter %r, keeping it", skipped.title)

        return chapters
    finally:
        if sanitized_path != path:
            try:
                Path(sanitized_path).unlink(missing_ok=True)
            except OSError:
                pass
