"""Dự đoán chỗ ngắt nghỉ trong lòng câu cho TTS tiếng Việt.

Nền tảng: `docs/toi_uu_tts.md` mục 3 (Cách 1 — rule-based) và mục 6 (mức B0–B3).

Mọi engine đang dùng (VoxCPM2, OmniVoice, Confucius, F5-ViVoice, VieNeu, ZeroTTS,
Edge, gTTS) đều nhận **plain text**, không có SSML, nên "break tag" duy nhất mà
chúng hiểu là *dấu câu*. Vì vậy predictor ở đây chỉ chèn `,` `;` `...` — không sinh
thẻ XML, không đổi mặt chữ, không xóa hay đảo từ.

Ràng buộc an toàn (giữ cho pass này luôn có thể chạy lại):
  * không chèn cạnh dấu câu đã có → chạy lần hai không đổi gì nữa (idempotent);
  * không chèn khi còn dưới 3 tiếng tới cuối câu;
  * tối thiểu ~8 âm tiết giữa hai cue liên tiếp.
"""
from __future__ import annotations

import re
from typing import Protocol

from app.chunker import mask_protected_spans

# Mức ngắt theo docs/toi_uu_tts.md mục 6. B1 ~ 80–150ms, B2 ~ 150–250ms,
# B3 ~ 300–500ms — với plain-text TTS, độ dài nghỉ do chính dấu câu quyết định.
B0_NONE = 0
B1_SHORT = 1
B2_MEDIUM = 2
B3_LONG = 3

BREAK_CUES: dict[int, str] = {B1_SHORT: ",", B2_MEDIUM: ";", B3_LONG: "..."}

# Câu ngắn hơn ngần này đọc một hơi vẫn tự nhiên.
_MIN_TOKENS_FOR_BREAK = 6
# Sau điểm ngắt phải còn đủ chữ, nếu không câu kết thúc lửng.
_MIN_TOKENS_AFTER_BREAK = 3
# Hai bên một liên từ đều phải đủ dài mới đáng ngắt.
_MIN_SYLLABLES_EACH_SIDE = 4
# Trần tần suất: tránh rắc dấu phẩy khắp câu.
_MIN_SYLLABLES_BETWEEN_CUES = 8

_TRAILING_PUNCTUATION = ",.;:!?…-–—)]}\"'»"
_LEADING_PUNCTUATION = ",.;:!?…-–—)]}\"'»"

# Trạng ngữ đứng đầu câu: người Việt đọc luôn nghỉ một nhịp sau chúng
# (docs/toi_uu_tts.md mục 4.3).
_FRONTED_ADVERBIALS: frozenset[str] = frozenset({
    "hôm nay", "hôm qua", "hôm sau", "hôm trước", "ngày mai", "ngày kia",
    "sáng nay", "sáng sớm", "trưa nay", "chiều nay", "tối nay", "đêm qua",
    "năm nay", "năm ngoái", "hiện nay", "ngày nay", "bây giờ", "bấy giờ",
    "lúc đó", "lúc này", "lúc ấy", "khi đó", "khi ấy", "dạo đó", "dạo ấy",
    "sau đó", "trước đó", "thế rồi", "rốt cuộc", "cuối cùng", "đầu tiên",
    "tiếp theo", "sau cùng", "lát sau", "một lúc sau", "không lâu sau",
    "mấy hôm sau", "ít lâu sau", "từ đó", "kể từ đó",
    "thực ra", "thật ra", "nói chung", "nhìn chung", "nói cách khác",
    "tuy nhiên", "tuy vậy", "tuy thế", "thế nhưng", "vì vậy", "vì thế",
    "do đó", "do vậy", "ngoài ra", "bên cạnh đó", "trong khi đó",
    "bỗng nhiên", "đột nhiên", "thình lình", "quả nhiên", "dĩ nhiên",
    "tất nhiên", "đương nhiên", "may thay", "tiếc thay",
})
_MAX_ADVERBIAL_TOKENS = max(len(p.split()) for p in _FRONTED_ADVERBIALS)

# Liên từ nối *mệnh đề*. Cố tình bỏ "và", "hay", "hoặc", "mà", "thì": chúng phần
# lớn nối danh từ/định ngữ, chèn phẩy vào đó là sai ngữ pháp chứ không phải ngắt hơi.
_CLAUSE_CONJUNCTIONS: frozenset[str] = frozenset({
    "nhưng", "song", "còn", "nên", "rồi", "nếu", "vì", "do",
    "tuy nhiên", "tuy vậy", "tuy thế", "thế nhưng", "thế mà", "vậy mà",
    "vì vậy", "vì thế", "do đó", "do vậy", "cho nên", "bởi vì", "bởi thế",
    "trong khi", "sau khi", "trước khi", "trong khi đó", "ngoài ra",
})
_MAX_CONJUNCTION_TOKENS = max(len(p.split()) for p in _CLAUSE_CONJUNCTIONS)

# Một "câu" để dự đoán ngắt nghỉ: khúc văn bản giữa hai dấu kết câu hoặc xuống dòng.
_SENTENCE_SPAN_RE = re.compile(r"[^.!?…\n]+")
_TOKEN_RE = re.compile(r"\S+")
_WORD_CHAR_RE = re.compile(r"[^\W_]", re.UNICODE)


class BreakPredictor(Protocol):
    """Gán nhãn B0–B3 cho từng token; nhãn nằm *sau* token cùng chỉ số."""

    def predict(self, tokens: list[str]) -> list[int]:
        ...


def _is_word(token: str) -> bool:
    return bool(_WORD_CHAR_RE.search(token))


def _bare(token: str) -> str:
    return token.strip(_TRAILING_PUNCTUATION + "(“”‘’«").lower()


def _phrase_at(words: list[str], start: int, length: int) -> str:
    return " ".join(words[start:start + length])


class RuleBasedBreakPredictor:
    """Heuristic thuần, không phụ thuộc thư viện ngoài, chạy được trên CPU yếu.

    Hai luật, đúng theo docs/toi_uu_tts.md mục 4.3:
      1. Nghỉ sau trạng ngữ mở đầu câu.
      2. Nghỉ trước liên từ nối mệnh đề khi cả hai vế đều đủ dài.
    """

    def predict(self, tokens: list[str]) -> list[int]:
        labels = [B0_NONE] * len(tokens)
        if len(tokens) < _MIN_TOKENS_FOR_BREAK:
            return labels
        words = [_bare(t) for t in tokens]
        last_index = len(tokens) - 1

        def _can_break_after(i: int) -> bool:
            if i < 0 or i > last_index - _MIN_TOKENS_AFTER_BREAK:
                return False
            token = tokens[i]
            # Đã có dấu câu ở đây rồi: nhịp nghỉ có sẵn, thêm nữa là thừa.
            return bool(token) and token[-1] not in _TRAILING_PUNCTUATION

        # 1. Trạng ngữ mở đầu câu.
        for length in range(min(_MAX_ADVERBIAL_TOKENS, last_index), 0, -1):
            if _phrase_at(words, 0, length) in _FRONTED_ADVERBIALS and _can_break_after(length - 1):
                labels[length - 1] = B1_SHORT
                break

        # 2. Trước liên từ nối mệnh đề.
        for i in range(1, last_index):
            for length in range(min(_MAX_CONJUNCTION_TOKENS, last_index - i), 0, -1):
                if _phrase_at(words, i, length) not in _CLAUSE_CONJUNCTIONS:
                    continue
                before = sum(1 for t in tokens[:i] if _is_word(t))
                after = sum(1 for t in tokens[i + length:] if _is_word(t))
                if before < _MIN_SYLLABLES_EACH_SIDE or after < _MIN_SYLLABLES_EACH_SIDE:
                    break
                if _can_break_after(i - 1) and not labels[i - 1]:
                    labels[i - 1] = B1_SHORT
                break

        return _thin_out(labels, tokens)


def _thin_out(labels: list[int], tokens: list[str]) -> list[int]:
    """Bỏ bớt cue nằm quá sát nhau (trần ~1 cue / 8 âm tiết)."""
    result = list(labels)
    syllables_since_cue = 0
    seen_cue = False
    for i, token in enumerate(tokens):
        if _is_word(token):
            syllables_since_cue += 1
        if not result[i]:
            continue
        if seen_cue and syllables_since_cue < _MIN_SYLLABLES_BETWEEN_CUES:
            result[i] = B0_NONE
            continue
        seen_cue = True
        syllables_since_cue = 0
    return result


_DEFAULT_PREDICTOR = RuleBasedBreakPredictor()


def get_predictor(word_segmentation: bool = False) -> BreakPredictor:
    """Chọn predictor. Milestone B cắm UndertheseaBreakPredictor vào đây."""
    if word_segmentation:
        try:
            from app.linguistic_breaks import get_linguistic_predictor
        except ImportError:
            return _DEFAULT_PREDICTOR
        return get_linguistic_predictor() or _DEFAULT_PREDICTOR
    return _DEFAULT_PREDICTOR


def insert_break_cues(text: str, predictor: BreakPredictor | None = None) -> str:
    """Chèn dấu câu làm cue ngắt nghỉ vào giữa câu, giữ nguyên mọi thứ còn lại.

    Chạy trên bản *mask* của văn bản (dấu chấm trong "TP.HCM", "1.5" bị che) nên
    ranh giới câu không bị hiểu sai, còn offset vẫn khớp 1-1 với chuỗi gốc.
    """
    if not text or not text.strip():
        return text
    predictor = predictor or _DEFAULT_PREDICTOR
    masked = mask_protected_spans(text)

    insertions: list[tuple[int, str]] = []
    for span in _SENTENCE_SPAN_RE.finditer(masked):
        matches = list(_TOKEN_RE.finditer(masked, span.start(), span.end()))
        if len(matches) < _MIN_TOKENS_FOR_BREAK:
            continue
        tokens = [text[m.start():m.end()] for m in matches]
        labels = predictor.predict(tokens)
        for m, label in zip(matches, labels):
            if not label:
                continue
            cue = BREAK_CUES.get(label)
            if not cue:
                continue
            following = text[m.end():m.end() + 1]
            if following and not following.isspace() and following in _LEADING_PUNCTUATION:
                continue
            insertions.append((m.end(), cue))

    if not insertions:
        return text
    out = text
    for offset, cue in sorted(insertions, reverse=True):
        out = out[:offset] + cue + out[offset:]
    return out
