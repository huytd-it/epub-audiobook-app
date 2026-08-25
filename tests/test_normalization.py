"""Unit tests for app/normalization.py."""
import pytest

from app.normalization import (
    NormalizationOptions,
    clean_junk_tokens,
    ensure_sentence_punctuation,
    normalize_file_extensions,
    normalize_numbers,
    normalize_text,
    remove_cjk,
    remove_dots_in_vietnamese_words,
)
from app.text_analysis import expand_abbreviations


def test_small_integer_to_words():
    assert normalize_numbers("1000") == "một nghìn"
    assert normalize_numbers("1060") == "một nghìn không trăm sáu mươi"
    assert normalize_numbers("9999") == "chín nghìn chín trăm chín mươi chín"


def test_long_integer_digit_by_digit():
    assert normalize_numbers("038920842") == "ba mươi tám triệu chín trăm hai mươi nghìn tám trăm bốn mươi hai"


def test_currency_vnd():
    assert normalize_numbers("100.000đ") == "một trăm nghìn đồng"
    assert normalize_numbers("1.000.000đ") == "một triệu đồng"
    assert normalize_numbers("VND 50000") == "năm mươi nghìn đồng"


def test_currency_usd():
    assert normalize_numbers("$50") == "năm mươi đô la"
    assert normalize_numbers("100 USD") == "một trăm đô la"


def test_date():
    assert normalize_numbers("01/01/2024") == "ngày một tháng một năm hai nghìn không trăm hai mươi tư"
    assert normalize_numbers("1/1/2024") == "ngày một tháng một năm hai nghìn không trăm hai mươi tư"


def test_date_does_not_duplicate_ngay_prefix():
    # vietnormalizer.convert_date chèn thêm "ngày" dù chữ "ngày" đã đứng trước.
    assert normalize_numbers("ngày 1/3/2024") == "ngày một tháng ba năm hai nghìn không trăm hai mươi tư"
    assert normalize_numbers("Ngày 01/01/2024") == "Ngày một tháng một năm hai nghìn không trăm hai mươi tư"
    # Phép lặp từ chủ ý không theo sau bởi cụm ngày thì giữ nguyên.
    assert normalize_numbers("Ngày ngày em chờ") == "Ngày ngày em chờ"


def test_time():
    assert normalize_numbers("14:30") == "mười bốn giờ ba mươi phút"
    assert normalize_numbers("9:00") == "chín giờ không phút"


def test_percent():
    assert normalize_numbers("50%") == "năm mươi phần trăm"
    assert normalize_numbers("12,5%") == "mười hai phẩy năm phần trăm"


def test_decimal():
    assert normalize_numbers("1,5") == "một phẩy năm"
    assert normalize_numbers("3,14") == "ba phẩy mười bốn"
    assert normalize_numbers("100,5") == "một trăm phẩy năm"
    assert normalize_numbers("1000,001") == "một nghìn phẩy một"


def test_dot_decimal():
    assert normalize_numbers("125,3") == "một trăm hai mươi lăm phẩy ba"


def test_plain_integer_longer_than_four_digits():
    assert normalize_numbers("50000") == "năm mươi nghìn"
    assert normalize_numbers("100000") == "một trăm nghìn"


def test_vietnormalizer_measurement_units_and_ordinals():
    assert normalize_numbers("120km/h") == "một trăm hai mươi ki-lô-mét trên giờ"
    assert normalize_numbers("thứ 2") == "thứ hai"


def test_vietnormalizer_dictionary_and_transliteration_options():
    base = dict(numbers=False, junk=False, spellcheck=False, file_extensions=False, punctuation=False)
    assert normalize_text("TV database qwertyz", NormalizationOptions(**base)) == "TV database qwertyz"
    assert normalize_text("TV database qwertyz", NormalizationOptions(**base, dictionary=True)) == "ti vi đa-ta-bê qwertyz"
    assert "qwertyz" not in normalize_text(
        "TV database qwertyz", NormalizationOptions(**base, transliteration=True)
    )


def test_clean_junk_tokens_default():
    assert clean_junk_tokens("OO@@") == ""
    assert clean_junk_tokens("## hello ##") == " hello "


def test_clean_junk_tokens_custom():
    assert clean_junk_tokens("fooBARbaz", ["BAR"]) == "foobaz"


def test_remove_cjk_basic():
    assert remove_cjk("Chương 日") == "Chương "
    assert remove_cjk("你好Hello") == "Hello"
    assert remove_cjk("Việt Nam") == "Việt Nam"


def test_remove_cjk_multiple_ranges():
    # CJK Unified Ideographs (4E00-9FFF), Extension A (3400-4DBF), Compatibility (F900-FAFF)
    assert remove_cjk("中文") == ""
    assert remove_cjk("A文B") == "AB"


def test_remove_cjk_edge_cases():
    assert remove_cjk("") == ""
    assert remove_cjk("Hello世界!") == "Hello!"
    assert remove_cjk("Chương 100") == "Chương 100"


def test_remove_cjk_in_pipeline():
    result = normalize_text("Hello你好 World", NormalizationOptions(numbers=False, junk=False, spellcheck=False, punctuation=False))
    assert result == "Hello World"


def test_remove_dots_in_vietnamese_words():
    assert remove_dots_in_vietnamese_words("ch.ế.t") == "chết"
    assert remove_dots_in_vietnamese_words("t.h.ế") == "thế"


def test_remove_dots_does_not_touch_english_or_urls():
    assert remove_dots_in_vietnamese_words("example.com") == "example.com"
    assert remove_dots_in_vietnamese_words("v.v.") == "v.v."
    assert remove_dots_in_vietnamese_words("U.S.A") == "U.S.A"


def test_remove_dots_in_vietnamese_without_diacritics():
    assert remove_dots_in_vietnamese_words("t.h.e") == "the"
    assert remove_dots_in_vietnamese_words("c.o.n") == "con"
    assert remove_dots_in_vietnamese_words("n.g.u.y.e.n") == "nguyen"
    assert remove_dots_in_vietnamese_words("t.h.u v.i.e.n") == "thu vien"


def test_normalize_text_full_pipeline():
    text = "OO@@ ch.ế.t 1000 lần, còn 038920842 là số điện thoại."
    result = normalize_text(text, NormalizationOptions())
    assert "OO@@" not in result
    assert "chết" in result
    assert "một nghìn" in result
    assert "ba mươi tám triệu chín trăm hai mươi nghìn tám trăm bốn mươi hai" in result


def test_normalize_text_respects_toggles():
    text = "OO@@ ch.ế.t 1000"
    opts = NormalizationOptions(numbers=False, junk=False, spellcheck=False, file_extensions=False, punctuation=False)
    assert normalize_text(text, opts) == text

    opts = NormalizationOptions(numbers=True, junk=False, spellcheck=False, file_extensions=False, punctuation=False)
    assert normalize_text(text, opts) == "OO@@ ch.ế.t một nghìn"

    opts = NormalizationOptions(numbers=False, junk=True, spellcheck=False, file_extensions=False, punctuation=False)
    assert normalize_text(text, opts) == " ch.ế.t 1000"

    opts = NormalizationOptions(numbers=False, junk=False, spellcheck=True, file_extensions=False, punctuation=False)
    assert normalize_text(text, opts) == "OO@@ chết 1000"


def test_normalize_file_extensions_wav():
    assert normalize_file_extensions("file.wav") == "file chấm wav"
    assert normalize_file_extensions("audio.wav") == "audio chấm wav"


def test_normalize_file_extensions_mp3():
    result = normalize_file_extensions("song.mp3")
    assert "chấm m p ba" in result
    assert "song" in result


def test_normalize_file_extensions_jpg():
    assert normalize_file_extensions("photo.jpg") == "photo chấm jpg"


def test_normalize_file_extensions_standalone():
    assert normalize_file_extensions("file .wav") == "file chấm wav"


def test_normalize_file_extensions_no_false_positive():
    assert normalize_file_extensions("v.v.") == "v.v."
    assert normalize_file_extensions("3.14") == "3.14"
    assert normalize_file_extensions("U.S.A") == "U.S.A"


def test_normalize_file_extensions_multiple():
    text = "Convert song.mp3 to audio.wav"
    result = normalize_file_extensions(text)
    assert "chấm m p ba" in result
    assert "chấm wav" in result


def test_ensure_sentence_punctuation_adds_period():
    result = ensure_sentence_punctuation("Xin chào\nTôi là Nam")
    assert result == "Xin chào.\nTôi là Nam."


def test_ensure_sentence_punctuation_skips_existing():
    result = ensure_sentence_punctuation("Xin chào.\nTôi là Nam?")
    assert result == "Xin chào.\nTôi là Nam?"


def test_ensure_sentence_punctuation_handles_comma():
    result = ensure_sentence_punctuation("Cha mẹ,")
    assert result == "Cha mẹ,"


def test_ensure_sentence_punctuation_empty_lines():
    result = ensure_sentence_punctuation("Hello\n\nWorld")
    assert result == "Hello.\n\nWorld."


def test_punctuation_with_file_extensions_pipeline():
    text = "Tên gọi sự kiện: Thiên Quốc Ca Thanh\nNgười ủy thác: Hạo Quân Thành\nGiới tính: Nam\nTuổi: hai mươi lăm\nNghề nghiệp: Lập trình viên\nQuan hệ gia đình: Cha mẹ,"
    result = normalize_text(text, NormalizationOptions(numbers=False, junk=False, spellcheck=False))
    lines = result.split("\n")
    assert all(line.endswith((".", ",", ":", "?", "!")) or not line.strip() for line in lines)
    assert "Cha mẹ," in result


def test_file_ext_in_pipeline():
    result = normalize_text("file.wav và file.mp3", NormalizationOptions(numbers=False, junk=False, spellcheck=False, punctuation=False))
    assert "chấm wav" in result
    assert "chấm m p ba" in result


def test_standalone_file_ext_in_pipeline():
    result = normalize_text("đuôi .wav", NormalizationOptions(numbers=False, junk=False, spellcheck=False, punctuation=False))
    assert "chấm wav" in result


# --- Viết tắt (docs/toi_uu_tts.md mục 5.3, 8.2) ---


def test_expand_common_abbreviations():
    assert expand_abbreviations("UBND thành phố xử lý vụ việc.").startswith("Ủy ban nhân dân")
    assert "Thành phố Hồ Chí Minh" in expand_abbreviations("Ông sống ở TP.HCM.")
    assert "Nhà xuất bản" in expand_abbreviations("Sách do NXB Trẻ phát hành.")


def test_expand_inserts_missing_space():
    assert expand_abbreviations("Nhà ở Q.1 rất đắt.") == "Nhà ở Quận 1 rất đắt."


def test_short_abbreviation_needs_numeric_context():
    # "P." ở đây là chữ cái đầu của tên người, không phải "Phường".
    assert expand_abbreviations("Ông P. Hùng đến muộn.") == "Ông P. Hùng đến muộn."
    assert expand_abbreviations("Nhà ở P.5 quận Bình Thạnh.") == "Nhà ở Phường 5 quận Bình Thạnh."


def test_unknown_acronym_is_left_alone():
    assert expand_abbreviations("Anh ấy sống ở U.S.A nhiều năm.") == "Anh ấy sống ở U.S.A nhiều năm."


def test_abbreviations_toggle_in_pipeline():
    text = "UBND xã ra thông báo mới."
    base = dict(numbers=False, junk=False, spellcheck=False, file_extensions=False, punctuation=False, breaks=False)
    assert "Ủy ban nhân dân" in normalize_text(text, NormalizationOptions(**base, abbreviations=True))
    assert normalize_text(text, NormalizationOptions(**base, abbreviations=False)) == text


def test_abbreviation_runs_before_number_normalization():
    result = normalize_text(
        "Nhà ở Q.1.",
        NormalizationOptions(junk=False, spellcheck=False, file_extensions=False, punctuation=False, breaks=False),
    )
    assert result == "Nhà ở Quận một."


# --- Cue ngắt nghỉ (docs/toi_uu_tts.md mục 3, 6) ---


def test_breaks_toggle_in_pipeline():
    text = "Ngày mai chúng ta sẽ họp bàn kế hoạch."
    base = dict(numbers=False, junk=False, spellcheck=False, file_extensions=False,
                punctuation=False, abbreviations=False)
    assert normalize_text(text, NormalizationOptions(**base, breaks=True)) == "Ngày mai, chúng ta sẽ họp bàn kế hoạch."
    assert normalize_text(text, NormalizationOptions(**base, breaks=False)) == text


def test_all_new_flags_off_is_a_no_op():
    text = "UBND TP.HCM họp ngày mai để bàn kế hoạch."
    opts = NormalizationOptions(numbers=False, junk=False, spellcheck=False, file_extensions=False,
                                punctuation=False, abbreviations=False, breaks=False)
    assert normalize_text(text, opts) == text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
