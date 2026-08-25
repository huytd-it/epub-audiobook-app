"""Tách câu không được cắt bên trong viết tắt, ngày tháng, số thập phân.

docs/toi_uu_tts.md mục 8.3: dấu chấm trong "TP.HCM" hay "GS.TS" không phải ranh
giới câu; cắt ở đó làm TTS ngắt hơi giữa tên riêng.
"""
import pytest

from app.chunker import _split_paragraph_into_sentences, mask_protected_spans, split_into_tts_chunks


def test_no_split_inside_place_abbreviation():
    text = "Ông làm việc tại TP.HCM. Sau đó ông về Q.1."
    parts = _split_paragraph_into_sentences(text)
    assert parts == ["Ông làm việc tại TP.HCM.", "Sau đó ông về Q.1."]


def test_no_split_on_titles_and_decimals():
    text = "GS.TS Nguyễn Văn A trình bày 1.5 điểm. Xong."
    assert _split_paragraph_into_sentences(text) == [
        "GS.TS Nguyễn Văn A trình bày 1.5 điểm.",
        "Xong.",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Hạn nộp là 1/3/2024 theo thông báo.",
        "Giá bán 1.500.000 đồng một chiếc.",
        "Anh ấy sống ở U.S.A nhiều năm rồi.",
        "Xem thêm tr. 45 của tài liệu.",
        "Có cam, quýt, bưởi v.v. đều bán được.",
    ],
)
def test_protected_patterns_stay_in_one_sentence(text):
    assert _split_paragraph_into_sentences(text) == [text]


def test_real_sentence_boundaries_still_split():
    text = "Trời mưa. Đường ngập! Ai cũng về muộn? Thế thôi…"
    assert len(_split_paragraph_into_sentences(text)) == 4


def test_mask_keeps_length_and_only_hides_terminators():
    text = "Tại TP.HCM. Xong."
    masked = mask_protected_spans(text)
    assert len(masked) == len(text)
    assert masked == "Tại TPxHCM. Xong."


def test_chunking_keeps_abbreviation_intact_when_paragraph_is_long():
    paragraph = ("Ông làm việc tại TP.HCM và đi lại rất nhiều nơi trong cả nước. " * 4).strip()
    chunks = split_into_tts_chunks(paragraph, max_chars=80)
    assert all(len(c) <= 80 for c in chunks)
    assert not any(c.endswith("TP.") for c in chunks)
