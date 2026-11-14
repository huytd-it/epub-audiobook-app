"""Tách lượt thoại thành chunk riêng + nhãn giọng đọc (kind).

Lời thoại trong ngoặc kép hoặc dòng "- ..." phải đứng chunk riêng giống mục
"[...]" để khoảng lặng chunk_pause_ms chen giữa các lượt, và mang kind
"dialogue" để bước tổng hợp chọn giọng hội thoại.
"""
from app.chunker import (split_dialogue_turns, split_into_tts_chunk_items,
                         split_into_tts_chunks)

SAMPLE = (
    "Đến hoa khôi của trường như Ôn Thư mà trước đây hắn còn chẳng nhớ nổi mặt mũi ra sao, "
    "thì hơi đâu quan tâm xem nhân vật phong vân nam này là cái thá gì? "
    "Tần Tư Dương nhíu mày hỏi: \"Cậu là...?\" \"Tôi là Triệu Lập Văn lớp hai.\" "
    "\"Triệu Lập Văn?\" Tần Tư Dương nhíu mày, cái tên này hình như cũng hơi quen quen, "
    "nhưng không nhớ rõ lắm. \"Xin lỗi, tôi hình như không quen cậu.\""
)


def test_each_dialogue_turn_is_its_own_chunk():
    items = split_into_tts_chunk_items(SAMPLE, 400)
    assert [item["kind"] for item in items] == [
        "narration", "dialogue", "dialogue", "dialogue", "narration", "dialogue",
    ]
    assert items[0]["text"].endswith("Tần Tư Dương nhíu mày hỏi:")
    assert items[1]["text"] == "\"Cậu là...?\""
    assert items[2]["text"] == "\"Tôi là Triệu Lập Văn lớp hai.\""
    assert items[3]["text"] == "\"Triệu Lập Văn?\""
    assert items[5]["text"] == "\"Xin lỗi, tôi hình như không quen cậu.\""


def test_split_dialogue_turns_is_idempotent():
    once = split_dialogue_turns(SAMPLE)
    assert split_dialogue_turns(once) == once


def test_dash_dialogue_lines_are_dialogue_chunks():
    items = split_into_tts_chunk_items("Anh ta dừng lại.\n- Cậu là ai?\n- Tôi là khách.", 400)
    assert [(item["kind"], item["text"]) for item in items] == [
        ("narration", "Anh ta dừng lại."),
        ("dialogue", "- Cậu là ai?"),
        ("dialogue", "- Tôi là khách."),
    ]


def test_curly_quotes_and_guillemets():
    for opener, closer in (("\u201c", "\u201d"), ("\u00ab", "\u00bb")):
        text = f"Ông ta nói: {opener}Đi đi.{closer} Rồi quay lưng."
        items = split_into_tts_chunk_items(text, 400)
        assert [item["kind"] for item in items] == ["narration", "dialogue", "narration"]
        assert items[1]["text"] == f"{opener}Đi đi.{closer}"


def test_punctuation_inside_dialogue_does_not_split_the_turn():
    items = split_into_tts_chunk_items("Cô ấy nói: \"Tôi không biết... Anh ấy đi rồi.\"", 400)
    assert [item["kind"] for item in items] == ["narration", "dialogue"]
    assert items[1]["text"] == "\"Tôi không biết... Anh ấy đi rồi.\""


def test_long_dialogue_hard_split_keeps_dialogue_kind():
    turn = "\"" + " ".join(["nội dung thoại rất dài"] * 40) + "?\""
    items = split_into_tts_chunk_items(turn, 100)
    assert len(items) > 1
    assert all(item["kind"] == "dialogue" for item in items)
    assert all(len(item["text"]) <= 100 for item in items)


def test_long_dialogue_splits_at_sentence_end_not_mid_clause():
    """Lượt thoại dài hơn max_chars bị chia tại dấu chấm (ranh giới câu) trước,
    không cắt ngang mệnh đề tại dấu phẩy như cách cũ (cắt tại khoảng trắng)."""
    turn = '"' + ("Một câu dài ở đây. Rất nhiều chữ tiếp theo sau dấu phẩy, "
                  "và còn thêm vài chữ nữa cho dài. ") * 3 + '"'
    chunks = split_into_tts_chunks(turn, max_chars=60)

    assert all(len(chunk) <= 60 for chunk in chunks)
    # Cách cũ cắt mảnh đầu ở 60 ký tự, rơi vào giữa câu (sau dấu phẩy). Mảnh đầu
    # giờ phải kết thúc ngay tại dấu chấm của câu đầu.
    assert chunks[0].endswith(".")
    assert chunks[0] == '"Một câu dài ở đây.'


def test_long_dialogue_does_not_cut_inside_protected_span():
    """Dấu chấm/phẩy trong số, viết tắt địa danh không bao giờ là điểm cắt, dù
    lượt thoại bị chia cứng vì quá max_chars."""
    turn = '"' + ("Giá là 1.500.000 đồng tại TP.HCM và Q.1, mua luôn 1,500,000 nữa. ") * 8 + '"'
    chunks = split_into_tts_chunks(turn, max_chars=60)

    assert all(len(chunk) <= 60 for chunk in chunks)
    for token in ("1.500.000", "TP.HCM", "Q.1", "1,500,000"):
        assert any(token in chunk for chunk in chunks), token
    # Không mảnh nào dừng giữa một con số (…1. | …500. | …000.).
    assert not any(chunk.rstrip('"').endswith(("1.", "500.", "000.")) for chunk in chunks)



def test_dash_bullets_inside_bracket_block_stay_narration():
    items = split_into_tts_chunk_items("[Kỹ năng:\n- Thể Oán Linh (cấp 3)\n- Trảm Hồn]", 400)
    assert all(item["kind"] == "narration" for item in items)
    assert "".join(item["text"] for item in items).count("-") == 2


def test_plain_string_api_is_unchanged():
    chunks = split_into_tts_chunks(SAMPLE, 400)
    assert chunks == [item["text"] for item in split_into_tts_chunk_items(SAMPLE, 400)]
    assert all(isinstance(chunk, str) for chunk in chunks)
