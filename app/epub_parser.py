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


def _clean_text(raw: str) -> str:
    text = _WHITESPACE_RE.sub(" ", raw)
    text = _BLANK_LINES_RE.sub("\n\n", text)
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
        title = headings[0].get_text(strip=True) if headings else ""
        text = _clean_text(soup.get_text(separator="\n"))
        return [(title, text)]

    titles = [h.get_text(strip=True) for h in headings]
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

            for title, text in _split_by_headings(soup):
                if len(text) < _MIN_CHAPTER_CHARS:
                    continue
                if not title:
                    title = f"Chapter {len(chapters) + 1}"
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
