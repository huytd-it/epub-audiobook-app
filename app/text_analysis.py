"""Text analysis for Text Studio: spell check, junk detection, effect markers.

ponytail: no external spell-check library. Regex + word lists for Phase 1.
Upgrade path: swap in pyspellchecker or symspellpy for accuracy.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

# --- Effect marker patterns ---

_EFFECT_PATTERNS: list[re.Pattern] = [
    re.compile(r"\[(tiếng\s+(?:khóc|rên|hét|cười|thở dài|la hét|gào thét|kêu la|nói chuyện|thì thầm|quát mắng|chửi rề))\]", re.IGNORECASE),
    re.compile(r"\[(âm\s+thanh\s+\w+)\]", re.IGNORECASE),
    re.compile(r"\[(sound\s*effect\s*:\s*[^\]]+)\]", re.IGNORECASE),
    re.compile(r"\[(cry|scream|moan|laugh|sigh|sob|whisper|shout|gasp|grunt)\]", re.IGNORECASE),
]

# --- Sound description words (mô tả âm thanh trong text) ---

_SOUND_VI_PATTERNS: list[re.Pattern] = [
    # Crying/sobbing
    re.compile(r"\b(?:khóc\s*(?:nức\s*nở|thút\s*thít|rấm\s*rứt|lóc\s*nhóc|sướt\s*mướt|nhè\s*nhẹ|ù\s*ù|to|lớn)|nức\s*nở|thút\s*thít|rấm\s*rứt)\b", re.IGNORECASE),
    # Screaming/shouting
    re.compile(r"\b(?:hét\s*(?:lên|to|lớn|thất\s*thanh|chói\s*tai|váng\s*trời)|la\s*(?:hét|lên|to|lớn)|gào\s*(?:thét|khóc|lên|to)|kêu\s*(?:la|lên|to)|quát\s*(?:mắng|lên|to)|chửi\s*(?:rề|mắng|bới)|thét\s*lên)\b", re.IGNORECASE),
    # Moaning/groaning
    re.compile(r"\b(?:rên\s*(?:rỉ|siết|lên|to|nhẹ)|thở\s*(?:dài|hắt|hồng\s*hộc|phì\s*phò|dốc|khò\s*khè)|nấc\s*(?:lên|cụt)|ngáp\s*(?:dài|vắn))\b", re.IGNORECASE),
    # Laughing
    re.compile(r"\b(?:cười\s*(?:khúc\s*khích|khà\s*khà|haha|hi\s*hi|hếch|mỉm|nhếch|mím|nham\s*nhở|ngặt\s*nghẽo|sặc\s*sụa|vặt|to|lớn|bò\s*lăn)|phì\s*cười|cợt\s*nhả)\b", re.IGNORECASE),
    # Whispering/soft sounds
    re.compile(r"\b(?:thì\s*thầm|thầm\s*thì|thổn\s*thức|lẩm\s*bẩm|mỉm\s*cười|hừm|ừm|hm|hừ)\b", re.IGNORECASE),
    # Interjections / onomatopoeia (âm thanh cảm thán)
    re.compile(r"(?:^|(?<=[\s,.…!?;:\"'«»]))(?:ha\s*ha+|hi\s*hi+|he\s*he+|hừ\s*hừ+|hả|hả\s*hả|ục\s*ục|ọt\s*ọt|khè|phì|hừ|hự|ứ\s*ừ|ái|ối|trời\s*ơi|chết\s*tôi|ui\s*chao|ối\s*giời|a\s*ha+|ôi|úi|ủa|wow|whoa|oops|ouch|ugh|phew|yay)(?=[\s,.…!?;:\"'«»]|$)", re.IGNORECASE),
    # Vietnamese interjections (standalone exclamations, often followed by ellipsis/punctuation)
    re.compile(r"(?:^|(?<=[\s,.…!?;:\"'«»\u201c\u201d]))(?:ồ+|ố+|ổ+|ỗ+|ộ+|à+|á+|ả+|ã+|ạ+|úi|ôi|ui|ủa|hả|hừ|ứ|ự|è|ê|hỡi|ơi|cha|trời\s*ơi|giời\s*ơi|chao|ui\s*chao|ối|a\s*ha+|ha|hì+|hê+|hể+|hế+|haha|hihi|hehe|hừm|ừm|hm)(?=[\s,.…!?;:\"'«»\u201c\u201d…]|$)", re.IGNORECASE),
    # Stuttering / repetition (ta, ta… ta…)
    re.compile(r"(\w+)(?:\s*,\s*\1){1,}(?:\s*…|\s*\.\.\.)", re.IGNORECASE),
    # Impact/collision sounds
    re.compile(r"\b(?:rầm|bịch|bộp|choang|xoảng|loảng\s*xoảng|leng\s*keng|keng\s*keng|cạch|tách|tách\s*tách|lạch\s*cạch|bụp|đốp|phịch|phụp|uỳnh|oàng|ầm|ầm\s*ầm)\b", re.IGNORECASE),
    # Animal sounds
    re.compile(r"\b(?:gâu\s*gâu|meo\s*meo|ò\s*o|cục\s*cục|cuc\s*các|quác\s*quác|éc\s*éc|be\s*be|ee\s*ee|gù\s*gù|chip\s*chip|sủa|gầm|gừ)\b", re.IGNORECASE),
    # Water/nature sounds
    re.compile(r"\b(?:rào\s*rào|xào\s*xào|xột\s*xoạt|soàn\s*soạt|lục\s*đục|tí\s*tách|rops\s*rọp|lộp\s*bộp|ràm\s*rạm)\b", re.IGNORECASE),
]

_SOUND_EN_PATTERNS: list[re.Pattern] = [
    re.compile(r"\b(?:sob(?:bing)?|cry(?:ing)?|weep(?:ing)?|wail(?:ing)?|bawl(?:ing)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:scream(?:ing)?|shout(?:ing)?|yell(?:ing)?|shriek(?:ing)?|howl(?:ing)?|roar(?:ing)?|bellow(?:ing)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:moan(?:ing)?|groan(?:ing)?|whimper(?:ing)?|sniffle(?:ing)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:laugh(?:ing)?|giggle(?:ing)?|chuckle(?:ing)?|snicker(?:ing)?|cackle(?:ing)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:whisper(?:ing)?|murmur(?:ing)?|mutter(?:ing)?|hum(?:ming)?)\b", re.IGNORECASE),
    re.compile(r"\b(?:bang|crash|thud|splat|clank|clang|crack|snap|pop|splash|drip|splash)\b", re.IGNORECASE),
]

# --- Junk patterns ---

_JUNK_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"[@]{2,}"), "@@"),
    (re.compile(r"[#]{2,}"), "##"),
    (re.compile(r"[*]{2,}"), "**"),
    (re.compile(r"[~]{3,}"), "~~~"),
    (re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]+"), "CJK"),
]

# --- Abbreviations / acronyms (TTS đọc sai hoặc đánh vần từng chữ) ---

# Viết tắt phổ biến kèm cách đọc mong muốn. TTS gặp "TP.HCM" sẽ đánh vần từng chữ
# cái hoặc đọc dấu chấm thành "chấm", nên đây là nhóm lỗi phát âm rõ rệt nhất.
_ABBREVIATION_EXPANSIONS: dict[str, str] = {
    "tp.hcm": "Thành phố Hồ Chí Minh",
    "tphcm": "Thành phố Hồ Chí Minh",
    "tp.": "Thành phố",
    "q.": "Quận",
    "p.": "Phường",
    "tt.": "Thị trấn",
    "h.": "Huyện",
    "ubnd": "Ủy ban nhân dân",
    "hđnd": "Hội đồng nhân dân",
    "thpt": "Trung học phổ thông",
    "thcs": "Trung học cơ sở",
    "đh": "Đại học",
    "cđ": "Cao đẳng",
    "tnhh": "trách nhiệm hữu hạn",
    "cty": "Công ty",
    "clb": "Câu lạc bộ",
    "hlv": "huấn luyện viên",
    "vđv": "vận động viên",
    "gs.": "Giáo sư",
    "pgs.": "Phó giáo sư",
    "ts.": "Tiến sĩ",
    "ths.": "Thạc sĩ",
    "bs.": "Bác sĩ",
    "ks.": "Kỹ sư",
    "nxb": "Nhà xuất bản",
    "v.v.": "vân vân",
    "vd:": "ví dụ:",
    "vd.": "ví dụ.",
    "tr.": "trang",
    "etc.": "et cetera",
    "i.e.": "tức là",
    "e.g.": "ví dụ",
    "vs.": "đấu với",
    "mr.": "Ông",
    "mrs.": "Bà",
    "dr.": "Bác sĩ",
}

# Khớp đúng các viết tắt trên (dài trước ngắn để "tp.hcm" không bị "tp." nuốt mất).
_ABBREVIATION_RE = re.compile(
    r"(?<![\w.])(?:"
    + "|".join(
        re.escape(key)
        for key in sorted(_ABBREVIATION_EXPANSIONS, key=len, reverse=True)
    )
    + r")(?![\w])",
    re.IGNORECASE,
)

# Từ viết hoa toàn bộ 2–6 chữ cái không dấu, không nằm trong danh sách trên: gần như
# luôn là acronym và TTS sẽ đọc như một từ thay vì đánh vần (hoặc ngược lại).
_ACRONYM_RE = re.compile(r"(?<![\w.])[A-Z]{2,6}(?![\w])")

# Viết tắt kiểu chữ-chấm-chữ ("N.A.S.A", "T.P") — luôn đọc sai.
_DOTTED_ACRONYM_RE = re.compile(r"(?<![\w.])(?:[A-ZĐ]\.){2,}[A-ZĐ]?(?![\w])")

# Không cảnh báo với các "acronym" thực ra là chữ số La Mã, đã có bộ chuẩn hoá riêng.
_ROMAN_NUMERAL_RE = re.compile(r"^[IVXLCDM]+$")


def _check_abbreviations(text: str) -> list[dict]:
    """Phát hiện viết tắt/acronym — nhóm khiến TTS đọc sai nhiều nhất sau ký tự rác.

    Các mẫu chồng lên nhau ("GS." khớp cả danh sách viết tắt lẫn mẫu acronym chung,
    "TP.HCM" chứa "TP"), nên phải khử theo *vùng giao nhau* chứ không theo vị trí khớp
    chính xác — nếu không cùng một chỗ sẽ bị đếm hai lần.
    """
    warnings: list[dict] = []
    occupied: list[tuple[int, int]] = []

    def _claim(start: int, end: int) -> bool:
        if any(start < taken_end and taken_start < end for taken_start, taken_end in occupied):
            return False
        occupied.append((start, end))
        return True

    # Danh sách viết tắt đã biết đi trước: chúng mang được cách đọc gợi ý.
    for m in _ABBREVIATION_RE.finditer(text):
        if not _claim(m.start(), m.end()):
            continue
        warnings.append({
            "kind": "abbreviation",
            "position": m.start(),
            "length": len(m.group()),
            "original": m.group(),
            "suggestion": _ABBREVIATION_EXPANSIONS.get(m.group().lower(), ""),
        })

    # Rồi mới tới acronym chung — dạng có dấu chấm trước vì nó dài hơn.
    for pattern in (_DOTTED_ACRONYM_RE, _ACRONYM_RE):
        for m in pattern.finditer(text):
            word = m.group()
            if _ROMAN_NUMERAL_RE.match(word.replace(".", "")):
                continue
            if not _claim(m.start(), m.end()):
                continue
            warnings.append({
                "kind": "abbreviation",
                "position": m.start(),
                "length": len(word),
                "original": word,
                "suggestion": "",
            })

    return warnings


# --- Vietnamese spell check ---

_VIETNAMESE_VOWELS = "aeiouàáảãạăắằẳẵặâấầẩẫậđéèẻẽẹêếềểễệíìỉĩịóòỏõọôốồổỗộơớờởỡợúùủũụưứừửữựýỳỷỹỵ"
_VIETNAMESE_VOWELS_UPPER = _VIETNAMESE_VOWELS.upper()
_VIETNAMESE_CONSONANTS = "bcdđghklmnpqrstvxỹỵỷý"
_VIET_DIACRITICS_MAP = {
    'a': 'àáảãạ', 'ă': 'ắằẳẵặ', 'â': 'ấầẩẫậ',
    'e': 'èẻẽẹé', 'ê': 'ếềểễệ',
    'i': 'ìỉĩịí', 'o': 'òóỏõọ', 'ô': 'ốồổỗộ', 'ơ': 'ớờởỡợ',
    'u': 'ùúủũụ', 'ư': 'ứừửữự', 'y': 'ỳýỷỹỵ',
}
_SUSPICIOUS_COMBOS = [
    re.compile(r"([aeiou])\1{2,}", re.IGNORECASE),
    re.compile(r"[qx][bcdfghjklmnpqrstvwxz]", re.IGNORECASE),
    re.compile(r"[bcdfghjklmnpqrstvwxz]{4,}", re.IGNORECASE),
]

# --- English word list (top common, ponytail: bundled subset) ---

_COMMON_EN: set[str] = set()
_EN_WORDS_MINIMAL = (
    "the be to of and a in that have i it for not on with he as you do at "
    "this but his by from they we say her she or an will my one all would there "
    "their what so up out if about who get which go me when make can like time "
    "no just him know take people into year your good some could them see other "
    "than then now look only come its over think also back after use two how our "
    "work first well way even new want because any these give day most us "
    "is are was were been has had do does did shall should may might must can could "
    "would will the a an and but or nor for yet so at by from in into of on to with "
    "above across after against along among around before below beneath beside between "
    "beyond during except inside near off onto outside through throughout toward under "
    "until up upon within without"
)
_COMMON_EN.update(_EN_WORDS_MINIMAL.split())


def _is_likely_vietnamese_word(word: str) -> bool:
    has_diacritic = any(c in _VIETNAMESE_VOWELS for c in word.lower())
    return has_diacritic and len(word) >= 2


def _check_vietnamese_spelling(text: str) -> list[dict]:
    warnings = []
    for m in re.finditer(r"[a-zA-ZÀ-ỹ]+(?:\.[a-zA-ZÀ-ỹ]+)*", text):
        word = m.group()
        if len(word) < 3:
            continue
        for pattern in _SUSPICIOUS_COMBOS:
            if pattern.search(word):
                warnings.append({
                    "kind": "spell_vi",
                    "position": m.start(),
                    "length": len(word),
                    "original": word,
                    "suggestion": "",
                })
                break
    return warnings


def _check_english_spelling(text: str) -> list[dict]:
    warnings = []
    for m in re.finditer(r"\b[a-zA-Z]{3,}\b", text):
        word = m.group().lower()
        if word not in _COMMON_EN and not _is_likely_vietnamese_word(m.group()):
            pass
    return warnings


def _check_junk(text: str) -> list[dict]:
    warnings = []
    for pattern, label in _JUNK_PATTERNS:
        for m in pattern.finditer(text):
            warnings.append({
                "kind": "junk",
                "position": m.start(),
                "length": len(m.group()),
                "original": m.group(),
                "suggestion": "",
            })
    return warnings


def _check_effect_markers(text: str) -> list[dict]:
    warnings = []
    for pattern in _EFFECT_PATTERNS:
        for m in pattern.finditer(text):
            warnings.append({
                "kind": "effect_marker",
                "position": m.start(),
                "length": len(m.group()),
                "original": m.group(),
                "suggestion": m.group(),
            })
    return warnings


def _check_sound_descriptions(text: str) -> list[dict]:
    """Detect words/phrases that describe sounds inline (not in brackets).
    These may need TTS tuning or replacement with sound effect files."""
    warnings = []
    seen: set[tuple[int, int]] = set()
    for pattern in _SOUND_VI_PATTERNS + _SOUND_EN_PATTERNS:
        for m in pattern.finditer(text):
            key = (m.start(), m.end())
            if key not in seen:
                seen.add(key)
                warnings.append({
                    "kind": "sound_desc",
                    "position": m.start(),
                    "length": len(m.group()),
                    "original": m.group(),
                    "suggestion": "",
                })
    return warnings


def analyze_text(text: str) -> list[dict]:
    """Run all checks and return list of warning dicts."""
    if not text:
        return []
    warnings: list[dict] = []
    warnings.extend(_check_junk(text))
    warnings.extend(_check_vietnamese_spelling(text))
    warnings.extend(_check_english_spelling(text))
    warnings.extend(_check_abbreviations(text))
    warnings.extend(_check_effect_markers(text))
    warnings.extend(_check_sound_descriptions(text))
    warnings.sort(key=lambda w: w["position"])
    return warnings


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
