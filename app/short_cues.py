"""Cue cho short dọc: tách câu + ước lượng mốc thời gian theo audio thật.

Remotion không đọc .ass, nên short đi đường Remotion cần cue JSON. Ở đây chia
câu theo văn bản rồi chia tỉ lệ theo số ký tự trên TTS duration — không phải
forced-alignment, nhưng đủ khớp để highlight từ chạy cùng giọng đọc. Giữ
chung `subtitle_gen.DEFAULT_MAX_CHARS_PER_CUE` để không lệch style sub ngang.
"""
from __future__ import annotations

import re

DEFAULT_MAX_CHARS_PER_CUE = 42

# Dấu kết câu tiếng Việt + tiếng Anh. Cắt sau dấu, giữ dấu ở cuối cue.
_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
# Tiếng Việt viết không chấm câu giữa dòng; tách theo câu bằng những dấu này.
_SOFT_BREAK = re.compile(r"[,;:–—]\s+")


def split_sentences(text: str) -> list[str]:
    """Tách câu, giữ nguyên dấu. Rỗng -> [] (không bịa nội dung)."""
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return []
    parts: list[str] = []
    for block in _SENTENCE_END.split(cleaned):
        block = block.strip()
        if block:
            parts.append(block)
    return parts


def _greedy_wrap(sentences: list[str], max_chars: int) -> list[str]:
    """Ghép câu thành cue ngắn đủ hiển thị; câu quá dài bị cắt theo dấu mềm."""
    cues: list[str] = []
    for sentence in sentences:
        if len(sentence) <= max_chars:
            cues.append(sentence)
            continue
        buffer = ""
        for piece in _SOFT_BREAK.split(sentence):
            piece = piece.strip()
            if not piece:
                continue
            if not buffer:
                buffer = piece
            elif len(buffer) + len(piece) + 1 <= max_chars:
                buffer = f"{buffer} {piece}"
            else:
                cues.append(buffer)
                buffer = piece
        if buffer:
            cues.append(buffer)
    return cues


def build_vertical_cues(text: str, duration_seconds: float, *,
                        max_chars_per_cue: int = DEFAULT_MAX_CHARS_PER_CUE) -> list[dict]:
    """Cue JSON cho Remotion: [{text, start, end, words}].

    Thời gian chia theo tỉ lệ số ký tự trong tổng thời lượng audio; cue cuối
    chốt đúng `duration_seconds` để không hụt đuôi. `words` cho phép lớp đồ hoạ
    tự highlight từng từ.
    """
    if duration_seconds <= 0:
        raise ValueError("duration_seconds must be positive")
    cues = _greedy_wrap(split_sentences(text), max_chars_per_cue)
    if not cues:
        return []
    total_chars = sum(len(cue) for cue in cues) or 1
    out: list[dict] = []
    cursor = 0.0
    for index, cue in enumerate(cues):
        share = duration_seconds * (len(cue) / total_chars)
        end = duration_seconds if index == len(cues) - 1 else cursor + share
        out.append({
            "text": cue,
            "start": round(cursor, 3),
            "end": round(end, 3),
            "words": [w for w in cue.split() if w],
        })
        cursor = end
    return out