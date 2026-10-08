"""Two independent chunking levels: chapters->patches, and patch text->TTS-sized chunks."""
from __future__ import annotations

import re

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+")
# Xuống dòng đơn cũng là ranh giới câu: EPUB join các khối bằng "\n" đơn
# (epub_parser dùng separator="\n"), nên khối stat nhiều dòng kết thúc bằng
# "]" sẽ dính thành một câu dài không dấu kết câu nếu chỉ tách ở ".!?…".
_SINGLE_NEWLINE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+|\s*\n\s*")

# Dấu câu đọc lên đã có nhịp nghỉ — dòng kết thúc bằng chúng thì không cần
# thêm phẩy trước xuống dòng.
_LINE_END_PAUSE_CHARS = frozenset(".!?…,:;")

# Các mục "[...]" kề nhau, cách nhau bằng khoảng trắng ("[Trò chơi
# mới] [Tiếp tục trò chơi] [Thoát]") khiến TTS đọc liền không nghỉ. Chấm tạo
# ranh giới câu — nhịp nghỉ chắc chắn nghe được trên mọi engine (phẩy đã thử
# nhưng ZeroTTS đọc lướt, không khác biệt). Chỗ đã có dấu câu thì giữ nguyên.
_BRACKET_PAIR_RE = re.compile(r"(?<![.!?…,:;])\]\s*\[")

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
    for m in _SINGLE_NEWLINE_BOUNDARY_RE.finditer(masked):
        sentences.append(paragraph[last:m.start()])
        last = m.end()
    sentences.append(paragraph[last:])
    return [s.strip() for s in sentences if s.strip()]


def _add_pause_comma_before_linebreaks(text: str) -> str:
    """Thêm dấu phẩy vào cuối dòng chưa có nhịp nghỉ trước xuống dòng đơn.

    Khối stat kiểu "[Kỹ năng:\\n- Thể Oán Linh (...)\\n- ...]" không có dấu
    kết câu nên TTS đọc liền một hơi không nghỉ. Phẩy cho nhịp nghỉ ngắn
    (phù hợp liệt kê) và chạy lại idempotent — dòng đã kết thúc bằng dấu
    câu thì giữ nguyên. Ngắt đoạn ("\\n\\n", dòng trống) không đụng tới.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    for i in range(len(lines) - 1):
        if not lines[i].strip() or not lines[i + 1].strip():
            continue  # ngắt đoạn hoặc dòng trống: giữ nguyên
        stripped = lines[i].rstrip()
        if stripped and stripped[-1] not in _LINE_END_PAUSE_CHARS:
            lines[i] = f"{stripped},"
    return "\n".join(lines)


def _add_sentence_break_between_brackets(text: str) -> str:
    """Thêm dấu chấm giữa các mục "[...]" kề nhau trên cùng một dòng.

    "[A] [B]" → "[A]. [B]" để TTS ngắt câu khi đọc liệt kê (lựa chọn game,
    bảng stat viết gọn một dòng). Chạy lại idempotent — đã có dấu câu ở
    giữa (kể cả trước dấu "]" như "rồi.] [Sau") thì regex không khớp nữa.
    """
    return _BRACKET_PAIR_RE.sub("]. [", text)


def _is_bracket_item(piece: str) -> bool:
    """Câu/đoạn mở đầu bằng "[" (lựa chọn game, dòng stat, câu dẫn trong ngoặc).

    Các mục này luôn đứng chunk riêng để khoảng lặng chunk_pause_ms chen giữa
    các mục — nhịp nghỉ do app chèn khi ghép audio, không phụ thuộc prosody
    của engine (ZeroTTS đọc lướt mọi dấu câu ở đoạn liệt kê trên mọi giọng
    đã thử: maichi, kimoanh, huuduc).
    """
    return piece.lstrip().startswith("[")


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
    never splitting mid-sentence.

    Xuống dòng đơn cũng là điểm tách (khối stat nhiều dòng "[...]" không có
    dấu kết câu) và dòng chưa có nhịp nghỉ được thêm phẩy để TTS ngắt hơi.
    Các mục "[...]" kề nhau trên cùng một dòng được chèn dấu chấm ngắt câu.
    Mọi câu/đoạn mở đầu bằng "[" luôn đứng chunk riêng để khoảng lặng
    chunk_pause_ms chen giữa các mục khi ghép audio.
    """
    text = _add_sentence_break_between_brackets(text)
    text = _add_pause_comma_before_linebreaks(text)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    pieces: list[tuple[str, bool]] = []
    for paragraph in paragraphs:
        # Mục ngoặc vuông có thể nằm sau câu dẫn, không chỉ ở đầu đoạn.
        # Cho đoạn đó đi qua bước tách câu để giữ cờ standalone của từng mục.
        if len(paragraph) <= max_chars and "[" not in paragraph:
            pieces.append((paragraph, False))
            continue
        for sentence in _split_paragraph_into_sentences(paragraph):
            standalone = _is_bracket_item(sentence)
            if len(sentence) <= max_chars:
                pieces.append((sentence, standalone))
            else:
                # Mảnh cắt cứng từ một mục "[" vẫn đứng riêng — mảnh sau mất
                # dấu "[" mở đầu nên phải giữ cờ theo thay vì nhìn mặt chữ.
                pieces.extend((part, standalone) for part in _hard_split(sentence, max_chars))

    chunks: list[str] = []
    buffer = ""
    for piece, standalone in pieces:
        if standalone:
            if buffer:
                chunks.append(buffer)
                buffer = ""
            chunks.append(piece)
            continue
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
