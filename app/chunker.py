"""Two independent chunking levels: chapters->patches, and patch text->TTS-sized chunks."""
from __future__ import annotations

import re

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+")

# Dấu chấm bên trong các mẫu dưới đây KHÔNG phải ranh giới câu. Nếu tách ở đó,
# "Ông làm việc tại TP.HCM." vỡ thành hai chunk và TTS đọc "tê pê" rồi ngắt hơi
# giữa tên riêng (docs/toi_uu_tts.md mục 8.3).
_PROTECTED_PATTERNS: list[re.Pattern] = [
    # Số thập phân và số có dấu phân cách nghìn: 1.5 — 1,5 — 1.500.000
    re.compile(r"\d+(?:[.,]\d+)+"),
    # Ngày tháng: 1/3/2024, 01.03.2024, 1-3-2024
    re.compile(r"\b\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}\b"),
    # Học hàm/học vị ghép: GS.TS, PGS.TS, ThS.BS — và dạng đơn "GS. Nam"
    re.compile(r"(?<![\w.])(?:PGS|GS|ThS|ThS|TS|BS|KS|NCS)\.(?:TS|BS|ThS|KS)?"),
    # Địa danh viết tắt: TP.HCM, Q.1, P.5, TT.Củ Chi, H.Bình Chánh
    re.compile(r"(?<![\w.])(?:TP|TT|Q|P|H|X)\.\s?[0-9A-ZĐÀ-Ỹ][\wÀ-ỹ]*"),
    # Acronym chấm giữa chữ: U.S.A, N.A.S.A
    re.compile(r"(?<![\w.])(?:[A-ZĐ]\.){2,}[A-ZĐ]?"),
    # Viết tắt thông dụng không kết thúc câu
    re.compile(r"(?<![\w.])(?:v\.v|i\.e|e\.g|etc|Mr|Mrs|Ms|Dr|St|No|tr|vd)\.", re.IGNORECASE),
]


def mask_protected_spans(text: str) -> str:
    """Trả về bản sao *cùng độ dài* với dấu câu nằm trong mẫu bảo vệ bị che.

    Che thay vì thay thế bằng placeholder để mọi offset khớp 1-1 với chuỗi gốc:
    caller chạy regex trên bản che rồi cắt/chèn trên chuỗi gốc.
    """
    if "." not in text:
        return text
    masked = list(text)
    for pattern in _PROTECTED_PATTERNS:
        for m in pattern.finditer(text):
            for i in range(m.start(), m.end()):
                if masked[i] in ".!?…":
                    masked[i] = "x"
    return "".join(masked)


def group_into_patches(chapter_count: int, patch_size: int = 10) -> list[tuple[int, int]]:
    """Return a list of (chapter_start, chapter_end) inclusive ranges, sequential, last one
    may be smaller than patch_size."""
    if chapter_count <= 0:
        return []
    ranges = []
    for start in range(0, chapter_count, patch_size):
        end = min(start + patch_size - 1, chapter_count - 1)
        ranges.append((start, end))
    return ranges


def _split_paragraph_into_sentences(paragraph: str) -> list[str]:
    masked = mask_protected_spans(paragraph)
    sentences: list[str] = []
    last = 0
    for m in _SENTENCE_BOUNDARY_RE.finditer(masked):
        sentences.append(paragraph[last:m.start()])
        last = m.end()
    sentences.append(paragraph[last:])
    return [s.strip() for s in sentences if s.strip()]


def _hard_split(piece: str, max_chars: int) -> list[str]:
    """Last-resort split at word boundaries for a sentence longer than max_chars.

    Without this a paragraph carrying no sentence-ending punctuation would leave the
    chunker as one oversized chunk, and TTS backends reject it.
    """
    words = piece.split(" ")
    parts: list[str] = []
    buffer = ""
    for word in words:
        candidate = f"{buffer} {word}".strip()
        if len(candidate) <= max_chars:
            buffer = candidate
            continue
        if buffer:
            parts.append(buffer)
        # A single word longer than the limit still has to be cut somewhere.
        while len(word) > max_chars:
            parts.append(word[:max_chars])
            word = word[max_chars:]
        buffer = word
    if buffer:
        parts.append(buffer)
    return parts


def split_into_tts_chunks(text: str, max_chars: int = 400) -> list[str]:
    """Greedily pack paragraphs/sentences into chunks no longer than max_chars,
    never splitting mid-sentence."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    pieces: list[str] = []
    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
            continue
        for sentence in _split_paragraph_into_sentences(paragraph):
            if len(sentence) <= max_chars:
                pieces.append(sentence)
            else:
                pieces.extend(_hard_split(sentence, max_chars))

    chunks: list[str] = []
    buffer = ""
    for piece in pieces:
        if not buffer:
            buffer = piece
        elif len(buffer) + 1 + len(piece) <= max_chars:
            buffer = f"{buffer} {piece}"
        else:
            chunks.append(buffer)
            buffer = piece
    if buffer:
        chunks.append(buffer)

    return chunks
