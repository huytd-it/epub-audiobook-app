"""Chuẩn hóa văn bản cho TTS tiếng Việt.

Áp dụng động trước khi chia chunk TTS (giữ nguyên văn bản gốc).
Thứ tự xử lý:
  1. Xóa token rác (ví dụ OO@@ từ EPUB).
  2. Xóa dấu chấm trong từ tiếng Việt (ví dụ ch.ế.t → chết).
  3. Chuyển số/tiền tệ/ngày tháng/giờ/phần trăm thành chữ.
  4. Mở rộng đuôi tập tin (.wav, .mp3 ...) thành dạng nói.
  5. Đảm bảo mỗi dòng kết thúc bằng dấu câu.
  6. Quy tắc thay thế do người dùng định nghĩa chạy sau để ghi đè kết quả.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from num2words import num2words
from vietnormalizer import VietnameseNormalizer, VietnameseTextProcessor

_VIETNAMESE_PROCESSOR = VietnameseTextProcessor()
_VIETNAMESE_NORMALIZER = VietnameseNormalizer()

_VIETNAMESE_LOWER = (
    "àáảãạăắằẳẵặâấầẩẫậđéèẻẽẹêếềểễệíìỉĩịóòỏõọôốồổỗộơớờởỡợúùủũụưứừửữựýỳỷỹỵ"
)
_VIETNAMESE_UPPER = _VIETNAMESE_LOWER.upper()
_VIETNAMESE_LETTERS = f"a-zA-Z{_VIETNAMESE_LOWER}{_VIETNAMESE_UPPER}"
_VIETNAMESE_DIACRITICS = set(_VIETNAMESE_LOWER)

_DIGIT_WORDS = [
    "không", "một", "hai", "ba", "bốn", "năm", "sáu", "bảy", "tám", "chín"
]

DEFAULT_JUNK_TOKENS = ["OO@@", "@@", "##", "**"]

# Ideographs + CJK punctuation (\uff0c\u3002\u300c\u300d\u2026) and fullwidth forms. B\u1ecf s\u00f3t d\u1ea5u c\u00e2u CJK s\u1ebd \u0111\u1ec3 l\u1ea1i
# nh\u1eefng \u0111o\u1ea1n ch\u1ec9 c\u00f2n d\u1ea5u, v\u00e0 TTS tr\u1ea3 v\u1ec1 l\u1ed7i "No audio was received".
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\ufe30-\ufe4f\uff01-\uff65]+")

# Currency: requires either a prefix symbol/code ($/₫/đ/VND/USD/EUR)
# or a suffix symbol/code.
_CURRENCY_CODES = r"VND|vnd|USD|usd|EUR|eur"
_CURRENCY_SYMBOLS = r"\$|₫|đ"
_CURRENCY_RE = re.compile(
    rf"(?<![\w])"
    rf"(?:"
    rf"(?P<prefix>{_CURRENCY_CODES}|{_CURRENCY_SYMBOLS})\s*(?P<amount1>[0-9]{{1,3}}(?:[.][0-9]{{3}})+|[0-9]+)"
    rf"|"
    rf"(?P<amount2>[0-9]{{1,3}}(?:[.][0-9]{{3}})+|[0-9]+)\s*(?P<suffix>{_CURRENCY_CODES}|{_CURRENCY_SYMBOLS})"
    rf")"
    rf"(?![\w])"
)


# File extension: .wav, .mp3, .jpg etc.
# Two patterns: one with a preceding filename (needs space separator),
# one standalone (preceded by non-word).
_FILE_EXT_WITH_FILENAME_RE = re.compile(
    r"(?<=\w)"                       # preceded by word char (the filename)
    r"\."
    r"(?P<ext>[a-zA-Z][a-zA-Z0-9]{1,3})"  # extension: letter + 1-3 alnum = 2-4 total
    r"(?!\w)"                        # not followed by a word char
)
_FILE_EXT_STANDALONE_RE = re.compile(
    r"(?<![.\w])"                    # not preceded by dot or word char
    r"\."
    r"(?P<ext>[a-zA-Z][a-zA-Z0-9]{1,3})"
    r"(?!\w)"
)

@dataclass
class NormalizationOptions:
    numbers: bool = True
    junk: bool = True
    spellcheck: bool = True
    file_extensions: bool = True
    punctuation: bool = True
    dictionary: bool = False
    transliteration: bool = False
    junk_extra_tokens: list[str] | None = None


def _digit_to_word(d: str) -> str:
    return _DIGIT_WORDS[int(d)]


def _currency_unit(match_text: str) -> str:
    text = match_text.lower()
    if "usd" in text or "$" in text:
        return "đô la"
    if "eur" in text or "€" in text:
        return "euro"
    return "đồng"


def _replace_currency(m: re.Match) -> str:
    raw = m.group(0)
    amount_str = (m.group("amount1") or m.group("amount2")).replace(".", "")
    n = int(amount_str)
    unit = _currency_unit(raw)
    # Currency always gets semantic reading regardless of digit count.
    return f"{num2words(n, lang='vi')} {unit}"


def _read_extension(ext: str) -> str:
    """Read a file extension for TTS: if all letters keep as-is, else spell character-by-character."""
    if ext.isalpha():
        return ext
    words = []
    for ch in ext:
        if ch.isdigit():
            words.append(_digit_to_word(ch))
        else:
            words.append(ch)
    return " ".join(words)


def _replace_file_ext_spaced(m: re.Match) -> str:
    return f" chấm {_read_extension(m.group('ext'))}"


def _replace_file_ext_standalone(m: re.Match) -> str:
    return f"chấm {_read_extension(m.group('ext'))}"


def normalize_file_extensions(text: str) -> str:
    """Mở rộng đuôi tập tin thành dạng nói (.wav → 'chấm wav', .mp3 → 'chấm m p ba')."""
    text = _FILE_EXT_WITH_FILENAME_RE.sub(_replace_file_ext_spaced, text)
    text = _FILE_EXT_STANDALONE_RE.sub(_replace_file_ext_standalone, text)
    return text


_SENTENCE_PUNCTUATION = frozenset(".!?…:;,)\]\"'»")


def ensure_sentence_punctuation(text: str) -> str:
    """Thêm dấu chấm vào mỗi dòng chưa kết thúc bằng dấu câu."""
    lines = text.split("\n")
    result: list[str] = []
    for line in lines:
        stripped = line.rstrip()
        if stripped and stripped[-1] not in _SENTENCE_PUNCTUATION:
            stripped += "."
        result.append(stripped)
    return "\n".join(result)


def _has_vietnamese_diacritic(word: str) -> bool:
    return any(ch in _VIETNAMESE_DIACRITICS for ch in word.lower())


def _remove_dots_in_word(m: re.Match) -> str:
    word = m.group(0)
    if _has_vietnamese_diacritic(word):
        return word.replace(".", "")
    # Vietnamese without diacritics: remove dots if the word has
    # 2+ dots, short segments (≤3 chars each), and isn't uppercase
    # (which would be an abbreviation like U.S.A).
    parts = word.split(".")
    if len(parts) >= 3 and all(len(p) <= 3 for p in parts) and not word.isupper():
        return word.replace(".", "")
    return word


def remove_cjk(text: str) -> str:
    """Xóa ký tự CJK (chữ Hán, Nhật, Hàn) khỏi văn bản."""
    return _CJK_RE.sub("", text)


_SPEAKABLE_RE = re.compile(r"[^\W_]", re.UNICODE)


def drop_unspeakable_lines(text: str) -> str:
    """Xóa các dòng không còn chữ/số nào (chỉ còn dấu câu).

    Sau khi bóc CJK, một dòng gốc tiếng Trung thường chỉ còn lại dấu câu. TTS đọc dòng đó
    sẽ trả về audio rỗng ("No audio was received"), làm hỏng cả patch.
    """
    lines = [line for line in text.split("\n") if not line.strip() or _SPEAKABLE_RE.search(line)]
    return "\n".join(lines)


def clean_junk_tokens(text: str, tokens: list[str] | None = None) -> str:
    """Xóa token rác/định dạng khỏi văn bản."""
    tokens = tokens or DEFAULT_JUNK_TOKENS
    if not tokens:
        return text
    # Sort by length descending so longer tokens are removed first.
    sorted_tokens = sorted(set(tokens), key=len, reverse=True)
    pattern = re.compile(
        "|".join(re.escape(t) for t in sorted_tokens)
    )
    return pattern.sub("", text)


def remove_dots_in_vietnamese_words(text: str) -> str:
    """Xóa dấu chấm chèn trong từ tiếng Việt (ví dụ ch.ế.t → chết)."""
    pattern = re.compile(
        rf"(?<!\w)"
        rf"[{_VIETNAMESE_LETTERS}]*"
        rf"(?:\.[{_VIETNAMESE_LETTERS}]+)+"
        rf"(?!\w)"
    )
    return pattern.sub(_remove_dots_in_word, text)


_THU_ORDINALS = {"1": "nhất", "2": "hai", "3": "ba", "4": "tư", "5": "năm",
                 "6": "sáu", "7": "bảy", "8": "tám", "9": "chín", "10": "mười"}
_THU_ORDINAL_RE = re.compile(r"(?<=\bthứ)\s*(\d+)")


def _replace_thu_ordinal(m: re.Match) -> str:
    num = m.group(1)
    return f" {_THU_ORDINALS.get(num, num)}"


def normalize_numbers(text: str) -> str:
    """Chuyển số, ngày, giờ, tiền tệ và đơn vị thành chữ cho TTS tiếng Việt."""
    processor = _VIETNAMESE_PROCESSOR
    text = _CURRENCY_RE.sub(_replace_currency, text)
    text = processor.convert_address_number(text)
    text = processor.convert_year_range(text)
    text = processor.convert_date(text)
    text = processor.convert_time(text)
    text = _THU_ORDINAL_RE.sub(_replace_thu_ordinal, text)
    text = processor.remove_thousand_separators(text)
    text = processor.convert_currency(text)
    text = processor.convert_percentage(text)
    text = processor.convert_phone_number(text)
    text = processor.convert_decimal(text)
    text = processor.convert_measurement_units(text)
    text = processor.convert_roman_numerals(text)
    return processor.convert_standalone_numbers(text)


def normalize_text(text: str, opts: NormalizationOptions | None = None) -> str:
    """Chạy pipeline chuẩn hóa đầy đủ theo tùy chọn."""
    opts = opts or NormalizationOptions()
    if opts.junk:
        text = clean_junk_tokens(text, opts.junk_extra_tokens)
    text = remove_cjk(text)
    text = drop_unspeakable_lines(text)
    if opts.spellcheck:
        text = remove_dots_in_vietnamese_words(text)
    if opts.numbers:
        text = normalize_numbers(text)
    if opts.file_extensions:
        text = normalize_file_extensions(text)
    if opts.dictionary or opts.transliteration:
        text = _VIETNAMESE_NORMALIZER.normalize(
            text,
            enable_preprocessing=False,
            enable_transliteration=opts.transliteration,
        )
    if opts.punctuation:
        text = ensure_sentence_punctuation(text)
    return text


# --- Chapter title normalization for TTS ---

_CHAPTER_TITLE_PATTERNS = [
    # Vietnamese: "Chương 1", "Chương 1:", "Chương 1 -", "Phần 1", "Hồi 1"
    re.compile(r"^((?:Chương|Phần|Hồi|Phần|Quyển|Tập)\s+\d+)(\s*[:\-–—]?\s*)$", re.IGNORECASE | re.MULTILINE),
    # English: "Chapter 1", "Part 1", "Volume 1"
    re.compile(r"^((?:Chapter|Part|Volume|Book)\s+\d+)(\s*[:\-–—]?\s*)$", re.IGNORECASE | re.MULTILINE),
    # Numeric only: "1.", "1:", "1 -", "I.", "II."
    re.compile(r"^(\d{1,3})(\s*[.:\-–—]\s*)$", re.MULTILINE),
]

# A title line that already carries a name after the number, e.g. "Chương 12: Bão" —
# the plain _CHAPTER_TITLE_PATTERNS above only match a bare "Chương 12" with nothing
# after it, so a canonical "Chương N: Tên" title used to skip spoken-number conversion
# and the TTS breathing pause entirely. Handled separately because the name must be
# kept (not swallowed) after the number is converted.
_CHAPTER_TITLE_WITH_NAME_RE = re.compile(
    r"^((?:Chương|Phần|Hồi|Quyển|Tập|Chapter|Part|Volume|Book)\s+)(\d{1,5})(\s*[:\-–—]\s*)(\S.*?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

_ORDINAL_MAP_VI = {
    "1": "một", "2": "hai", "3": "ba", "4": "bốn", "5": "năm",
    "6": "sáu", "7": "bảy", "8": "tám", "9": "chín", "10": "mười",
    "11": "mười một", "12": "mười hai", "13": "mười ba", "14": "mười bốn",
    "15": "mười lăm", "16": "mười sáu", "17": "mười bảy", "18": "mười tám",
    "19": "mười chín", "20": "hai mươi",
}


def _normalize_chapter_number(num_str: str) -> str:
    """Convert chapter number to spoken Vietnamese."""
    n = num_str.strip()
    if n in _ORDINAL_MAP_VI:
        return _ORDINAL_MAP_VI[n]
    try:
        return num2words(int(n), lang="vi")
    except (ValueError, OverflowError):
        return n


def normalize_chapter_titles(text: str) -> str:
    """Normalize chapter titles for TTS: add pauses, normalize numbers."""
    def _replace_title_with_name(m: re.Match) -> str:
        prefix = m.group(1)
        spoken_num = _normalize_chapter_number(m.group(2))
        name = m.group(4).strip()
        return f"{prefix}{spoken_num}... {name}.\n\n"

    def _replace_title(m: re.Match) -> str:
        title = m.group(1).strip()
        # Extract number from title
        num_match = re.search(r"\d+", title)
        if num_match:
            spoken_num = _normalize_chapter_number(num_match.group())
            title = title[:num_match.start()] + spoken_num + title[num_match.end():]
        # Add pause after title for TTS breathing
        return f"{title}...\n\n"

    # Titles that already carry a name (e.g. "Chương 12: Bão") first, so the plain
    # number-only patterns below don't get a second, name-less crack at the same line.
    text = _CHAPTER_TITLE_WITH_NAME_RE.sub(_replace_title_with_name, text)
    for pattern in _CHAPTER_TITLE_PATTERNS:
        text = pattern.sub(_replace_title, text)
    return text
