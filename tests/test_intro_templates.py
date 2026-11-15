from app.intro_templates import collect_warnings, estimated_seconds, render_template, validate_template
from app.production_defaults import DEFAULT_INTRO_CONFIG, validate_intro_config


def test_render_substitutes_known_vars():
    assert render_template("Hi {book_title} - {channel_name}", {"book_title": "Sách A", "channel_name": "Kênh B"}) == "Hi Sách A - Kênh B"


def test_render_empty_var_becomes_empty_string():
    assert render_template("Hi {channel_name}!", {"channel_name": ""}) == "Hi !"
    assert render_template("Hi {author}", {}) == "Hi "


def test_validate_template_rejects_unsupported_var():
    errs = validate_template("Hi {unknown}")
    assert any("không hỗ trợ" in e for e in errs)


def test_validate_template_length():
    errs = validate_template("x" * 301)
    assert errs


def test_collect_warnings_missing_var():
    w = collect_warnings("Hi {channel_name}", {"channel_name": ""})
    assert any("channel_name" in x for x in w)


def test_collect_warnings_long_duration():
    w = collect_warnings("x" * 200, {})
    assert any("8 giây" in x for x in w)


def test_estimated_seconds():
    assert estimated_seconds("a" * 30) == 2.0


def test_validate_intro_config_clamps():
    cfg = validate_intro_config({**DEFAULT_INTRO_CONFIG, "cta_max_per_patch": 99})
    assert cfg["cta_max_per_patch"] == 5
    cfg2 = validate_intro_config({**DEFAULT_INTRO_CONFIG, "cta_max_per_patch": -5})
    assert cfg2["cta_max_per_patch"] == 0


def test_validate_intro_config_rejects_bad_var():
    try:
        validate_intro_config({**DEFAULT_INTRO_CONFIG, "intro_template": "hi {bad}"})
        assert False, "should raise"
    except ValueError as e:
        assert "không hỗ trợ" in str(e)
