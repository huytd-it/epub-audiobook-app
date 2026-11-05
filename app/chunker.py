"""Two independent chunking levels: chapters->patches, and patch text->TTS-sized chunks."""
from __future__ import annotations

import re

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+")
# Xuống dòng đơn cũng là ranh giới câu: EPUB join các khối bằng "\n" đơn
# (epub_parser dùng separator="\n"), nên khối stat nhiều dòng kết thúc bằng
# "]" sẽ dính thành một câu dài không dấu kết câu nếu chỉ tách ở ".!?…".
_SINGLE_NEWLINE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+|\s*\n\s*")

# Dấu câu đọc lên đã có nhịp nghỉ — dòng kết thúc bằng chúng thì không cần
# thêm phẩy trước xuống dòng. Ngoặc đóng của lời thoại nằm ở đây vì câu bên
# trong nó đã mang dấu câu ("Cậu là...?"), thêm phẩy thành "?",.
_LINE_END_PAUSE_CHARS = frozenset(".!?…,:;\"”»")

# Ngoặc kép dùng cho lời thoại: sách dịch dùng cả ngoặc thẳng (") lẫn cong
# (“ ”) và guillemet (« »). Mọi dạng đều là ranh giới lượt nói.
_DIALOGUE_OPENERS = "\"\u201c\u00ab"
_DIALOGUE_CLOSERS = "\"\u201d\u00bb"

# Hai lượt thoại đứng cạnh nhau ("..." "...") khiến TTS đọc liền một hơi, mất
# ranh giới người nói: tách dòng để bước tách câu bên dưới cắt ra.
_ADJACENT_TURNS_RE = re.compile(
    rf"(?<=[{re.escape(_DIALOGUE_CLOSERS)}])[ \t]+(?=[{re.escape(_DIALOGUE_OPENERS)}])"
)

# Đóng thoại ở cuối câu rồi tới chữ khác: câu kể nối tiếp phải đứng riêng.
_AFTER_SENTENCE_DIALOGUE_RE = re.compile(
    rf"(?<=[.!?\u2026][{re.escape(_DIALOGUE_CLOSERS)}])[ \t]+(?=\S)"
)

# Mở thoại ngay sau dấu câu dẫn ("hỏi: \"...\"") hoặc sau câu kết thúc
# ("... lắm. \"Xin lỗi"): câu dẫn và lời thoại thành hai piece riêng, mỗi
# piece một chunk. Sau ":" / ";" không cần khoảng trắng ("hỏi:\"...\""), còn
# sau dấu kết câu bắt buộc có — nếu không, dấu đóng thoại ("Cậu là...?")
# cũng khớp và bị tách thành chunk chỉ có một dấu ngoặc.
_BEFORE_DIALOGUE_RE = re.compile(
    rf"(?<=[:;])[ \t]*(?=[{re.escape(_DIALOGUE_OPENERS)}])"
    rf"|(?<=[,.!?\u2026])[ \t]+(?=[{re.escape(_DIALOGUE_OPENERS)}])"
)

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


def split_dialogue_turns(text: str) -> str:
    """Tách mỗi lượt thoại ra một dòng riêng.

    Chỉ *thêm xuống dòng*, không đổi mặt chữ: xuống dòng đơn vốn đã là ranh
    giới câu của chunker (`_SINGLE_NEWLINE_BOUNDARY_RE`), nên sau bước này
    mỗi lời thoại thành một piece độc lập và được đánh dấu standalone — đúng
    cơ chế đang dùng cho mục "[...]". Idempotent: chạy lần hai không còn
    khoảng trắng để thay.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not any(ch in text for ch in _DIALOGUE_OPENERS):
        return text
    text = _ADJACENT_TURNS_RE.sub("\n", text)
    text = _AFTER_SENTENCE_DIALOGUE_RE.sub("\n", text)
    return _BEFORE_DIALOGUE_RE.sub("\n", text)


def mask_dialogue_spans(text: str) -> str:
    """Che dấu câu *bên trong* lời thoại, giữ nguyên độ dài chuỗi.

    Câu "Tôi không biết... Anh ấy đi rồi." phải là một câu: nếu bước tách câu
    thấy dấu "..." ở giữa, nó cắt đôi lượt nói và mảnh sau mất ngoặc mở (nên
    bị xếp nhầm thành tường thuật). Mỗi dòng xử lý riêng vì sau
    split_dialogue_turns một lượt thoại luôn nằm trọn trên một dòng — ngoặc
    thiếu dấu đóng chỉ che tới hết dòng, không nuốt phần còn lại của đoạn.
    """
    if not any(ch in text for ch in _DIALOGUE_OPENERS):
        return text
    masked = list(text)
    offset = 0
    for line in text.split("\n"):
        start, end = offset, offset + len(line)
        offset = end + 1
        i = start
        while i < end:
            if text[i] not in _DIALOGUE_OPENERS:
                i += 1
                continue
            closer = next((j for j in range(i + 1, end) if text[j] in _DIALOGUE_CLOSERS), -1)
            stop = closer if closer != -1 else end
            for k in range(i, stop):
                if masked[k] in ".!?\u2026\n":
                    masked[k] = "x"
            i = stop + 1
    return "".join(masked)


def _has_dialogue_marker(text: str) -> bool:
    """Đoạn có lời thoại (ngoặc kép) hoặc dòng thoại kiểu gạch ngang đầu dòng."""
    if any(ch in text for ch in _DIALOGUE_OPENERS):
        return True
    return any(line.lstrip()[:1] in {"-", "\u2013", "\u2014"} and line.lstrip()[1:2].isspace()
               for line in text.split("\n") if line.strip())


def _split_paragraph_into_sentences(paragraph: str) -> list[str]:
    masked = mask_dialogue_spans(mask_protected_spans(paragraph))
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


def _is_dialogue_item(piece: str, *, in_bracket_block: bool = False) -> bool:
    """Piece là một lượt thoại: mở đầu bằng ngoặc kép, hoặc bằng gạch ngang
    đầu dòng kiểu "- Cậu là...?".

    Dòng gạch ngang bên trong khối "[...]" (bảng stat, danh sách kỹ năng) là
    mục liệt kê chứ không phải thoại, nên bị loại bằng ``in_bracket_block``.
    """
    stripped = piece.lstrip()
    if not stripped:
        return False
    if stripped[0] in _DIALOGUE_OPENERS:
        return True
    if in_bracket_block:
        return False
    return stripped[0] in {"-", "\u2013", "\u2014"} and stripped[1:2].isspace()


def _is_bracket_item(piece: str) -> bool:
    """Câu/đoạn mở đầu bằng "[" (lựa chọn game, dòng stat, câu dẫn trong ngoặc).

    Các mục này luôn đứng chunk riêng để khoảng lặng chunk_pause_ms chen giữa
    các mục — nhịp nghỉ do app chèn khi ghép audio, không phụ thuộc prosody
    của engine (ZeroTTS đọc lướt mọi dấu câu ở đoạn liệt kê trên mọi giọng
    đã thử: maichi, kimoanh, huuduc).
    """
    return piece.lstrip().startswith("[")


def _is_standalone_item(piece: str, *, in_bracket_block: bool = False) -> bool:
    return _is_bracket_item(piece) or _is_dialogue_item(piece, in_bracket_block=in_bracket_block)


def _kind_for(piece: str, *, in_bracket_block: bool = False) -> str:
    """Nhãn giọng đọc của một chunk: "dialogue" cho lời thoại, còn lại "narration"."""
    return "dialogue" if _is_dialogue_item(piece, in_bracket_block=in_bracket_block) else "narration"


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


def split_into_tts_chunk_items(text: str, max_chars: int = 400) -> list[dict]:
    """Như :func:`split_into_tts_chunks` nhưng trả kèm nhãn giọng đọc.

    Mỗi item là ``{"text", "kind"}`` với ``kind`` = "dialogue" (lời thoại trong
    ngoặc kép / dòng gạch ngang) hoặc "narration" (mọi thứ còn lại). Người gọi
    cần một giọng khác cho hội thoại dùng nhãn này để chọn engine theo chunk.
    """
    text = split_dialogue_turns(text)
    text = _add_sentence_break_between_brackets(text)
    text = _add_pause_comma_before_linebreaks(text)
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]

    pieces: list[tuple[str, bool, str]] = []
    for paragraph in paragraphs:
        # Mục ngoặc vuông và lời thoại có thể nằm sau câu dẫn, không chỉ ở đầu
        # đoạn. Cho đoạn đó đi qua bước tách câu để giữ cờ standalone của từng
        # mục. Khối "[...]" nhiều dòng là bảng stat: dấu "-" bên trong nó là
        # gạch đầu dòng, không phải lượt thoại.
        in_bracket_block = paragraph.lstrip().startswith("[")
        if (len(paragraph) <= max_chars and "[" not in paragraph
                and not _has_dialogue_marker(paragraph)):
            pieces.append((paragraph, False, "narration"))
            continue
        for sentence in _split_paragraph_into_sentences(paragraph):
            standalone = _is_standalone_item(sentence, in_bracket_block=in_bracket_block)
            kind = _kind_for(sentence, in_bracket_block=in_bracket_block)
            if len(sentence) <= max_chars:
                pieces.append((sentence, standalone, kind))
            else:
                # Mảnh cắt cứng từ một mục "[" (hoặc một lượt thoại dài) vẫn
                # đứng riêng — mảnh sau mất dấu mở đầu nên phải giữ cờ và nhãn
                # theo thay vì nhìn mặt chữ.
                pieces.extend((part, standalone, kind) for part in _hard_split(sentence, max_chars))

    chunks: list[tuple[str, str]] = []
    buffer = ""
    for piece, standalone, kind in pieces:
        if standalone:
            if buffer:
                chunks.append((buffer, "narration"))
                buffer = ""
            chunks.append((piece, kind))
            continue
        if not buffer:
            buffer = piece
        elif len(buffer) + 1 + len(piece) <= max_chars:
            buffer = f"{buffer} {piece}"
        else:
            chunks.append((buffer, "narration"))
            buffer = piece
    if buffer:
        chunks.append((buffer, "narration"))

    return [{"text": chunk, "kind": kind} for chunk, kind in chunks]


def split_into_tts_chunks(text: str, max_chars: int = 400) -> list[str]:
    """Greedily pack paragraphs/sentences into chunks no longer than max_chars,
    never splitting mid-sentence.

    Xuống dòng đơn cũng là điểm tách (khối stat nhiều dòng "[...]" không có
    dấu kết câu) và dòng chưa có nhịp nghỉ được thêm phẩy để TTS ngắt hơi.
    Các mục "[...]" kề nhau trên cùng một dòng được chèn dấu chấm ngắt câu.
    Mọi câu/đoạn mở đầu bằng "[" hoặc một lượt thoại ("...", "- ...") luôn
    đứng chunk riêng để khoảng lặng chunk_pause_ms chen giữa các mục khi ghép
    audio.
    """
    return [item["text"] for item in split_into_tts_chunk_items(text, max_chars)]
