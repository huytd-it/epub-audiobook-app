"""Rule-based break predictor: chèn cue ngắt nghỉ giữa câu cho TTS plain-text."""
import pytest

from app.breaks import (B1_SHORT, RuleBasedBreakPredictor, get_predictor,
                        insert_break_cues)


def test_fronted_adverbial_gets_comma():
    assert insert_break_cues("Ngày mai chúng ta sẽ họp.") == "Ngày mai, chúng ta sẽ họp."


def test_conjunction_gets_pause():
    out = insert_break_cues("Trời mưa rất to nhưng chúng tôi vẫn đi học đầy đủ.")
    assert out == "Trời mưa rất to, nhưng chúng tôi vẫn đi học đầy đủ."


def test_no_double_punctuation():
    assert insert_break_cues("Đã có dấu phẩy, ở đây.") == "Đã có dấu phẩy, ở đây."


def test_existing_comma_before_conjunction_is_left_alone():
    text = "Trời mưa rất to, nhưng chúng tôi vẫn đi học đầy đủ."
    assert insert_break_cues(text) == text


def test_idempotent():
    once = insert_break_cues("Ngày mai chúng ta sẽ họp bàn về kế hoạch sản xuất.")
    assert insert_break_cues(once) == once


def test_short_sentence_untouched():
    assert insert_break_cues("Ngày mai họp.") == "Ngày mai họp."


def test_no_cue_right_before_end_of_sentence():
    # "nên" đứng sát cuối: ngắt ở đó làm câu cụt.
    text = "Anh ấy đã cố gắng rất nhiều lần nên thôi."
    assert insert_break_cues(text) == text


def test_conjunction_needs_a_long_enough_first_clause():
    text = "Trời mưa nhưng chúng tôi vẫn đi học đầy đủ."
    assert insert_break_cues(text) == text


def test_cues_are_not_packed_too_densely():
    text = "Tuy nhiên trời mưa rất to nên chúng tôi phải nghỉ."
    out = insert_break_cues(text)
    assert out.count(",") <= 1


def test_paragraph_structure_is_preserved():
    text = "Ngày mai chúng ta sẽ họp.\n\nHôm qua trời mưa rất to ở ngoài kia."
    out = insert_break_cues(text)
    assert out.count("\n\n") == 1
    assert out.startswith("Ngày mai, chúng ta")


def test_protected_abbreviation_is_not_treated_as_sentence_end():
    text = "Ngày mai chúng ta sẽ họp tại TP.HCM và bàn thêm."
    out = insert_break_cues(text)
    assert "TP.HCM" in out
    assert out.startswith("Ngày mai, ")


@pytest.mark.parametrize("text", ["", "   ", "\n\n", "..."])
def test_degenerate_input(text):
    assert insert_break_cues(text) == text


def test_predictor_labels_are_aligned_with_tokens():
    tokens = "Ngày mai chúng ta sẽ họp".split()
    labels = RuleBasedBreakPredictor().predict(tokens)
    assert len(labels) == len(tokens)
    assert labels == [0, B1_SHORT, 0, 0, 0, 0]


def test_factory_defaults_to_rule_based():
    assert isinstance(get_predictor(), RuleBasedBreakPredictor)


def test_factory_falls_back_without_optional_package():
    assert get_predictor(word_segmentation=True) is not None
