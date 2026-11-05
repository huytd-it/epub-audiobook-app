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


def test_dash_bullets_inside_bracket_block_stay_narration():
    items = split_into_tts_chunk_items("[Kỹ năng:\n- Thể Oán Linh (cấp 3)\n- Trảm Hồn]", 400)
    assert all(item["kind"] == "narration" for item in items)
    assert "".join(item["text"] for item in items).count("-") == 2


def test_plain_string_api_is_unchanged():
    chunks = split_into_tts_chunks(SAMPLE, 400)
    assert chunks == [item["text"] for item in split_into_tts_chunk_items(SAMPLE, 400)]
    assert all(isinstance(chunk, str) for chunk in chunks)
