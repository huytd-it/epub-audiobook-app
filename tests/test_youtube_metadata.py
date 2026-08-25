import json
from types import SimpleNamespace
import pytest
import soundfile as sf
import numpy as np

from app import db
from app.youtube_metadata import (get_book_youtube_config, get_patch_youtube_override,
                                  resolve_patch_youtube_metadata, save_book_youtube_config,
                                  save_patch_youtube_override, validate_book_youtube_config,
                                  load_timeline, render_description_extra, PLAYLIST_LINK_LABEL)


def test_load_timeline_returns_valid_sidecar(tmp_path):
    audio = tmp_path / "result.wav"
    sf.write(audio, np.zeros(40), 1)
    timeline = {"version": 1, "sample_rate": 1, "total_frames": 40,
                "chapters": [{"chapter_index": 1, "start_frame": 0, "start_seconds": 0.0, "title": "One"},
                             {"chapter_index": 2, "start_frame": 10, "start_seconds": 10.0, "title": "Two"},
                             {"chapter_index": 3, "start_frame": 20, "start_seconds": 20.0, "title": "Three"}]}
    audio.with_suffix(".timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    assert load_timeline(audio) == timeline


@pytest.mark.parametrize("chapter_change", [
    lambda chapters: chapters[1].update(start_frame=5),
    lambda chapters: chapters[1].pop("chapter_index"),
])
def test_load_timeline_rejects_invalid_chapter_order_and_schema(tmp_path, chapter_change):
    audio = tmp_path / "invalid.wav"
    sf.write(audio, np.zeros(20), 10)
    chapters = [
        {"chapter_index": 1, "title": "One", "start_frame": 0, "start_seconds": 0},
        {"chapter_index": 2, "title": "Two", "start_frame": 10, "start_seconds": 1},
    ]
    chapter_change(chapters)
    audio.with_suffix(".timeline.json").write_text(json.dumps({
        "version": 1, "sample_rate": 10, "total_frames": 20, "chapters": chapters,
    }), encoding="utf-8")
    assert load_timeline(audio) is None


@pytest.mark.parametrize("count", [1, 2])
def test_load_timeline_accepts_valid_short_chapter_lists(tmp_path, count):
    audio = tmp_path / "short-structural.wav"
    sf.write(audio, np.zeros(20), 10)
    chapters = [{"chapter_index": i + 1, "title": f"Chapter {i + 1}",
                 "start_frame": i * 10, "start_seconds": i}
                for i in range(count)]
    audio.with_suffix(".timeline.json").write_text(json.dumps({
        "version": 1, "sample_rate": 10, "total_frames": 20, "chapters": chapters,
    }), encoding="utf-8")
    assert load_timeline(audio) is not None


def test_load_timeline_accepts_gapped_source_indexes(tmp_path):
    audio = tmp_path / "gapped.wav"
    sf.write(audio, np.zeros(20), 10)
    timeline = {"version": 1, "sample_rate": 10, "total_frames": 20, "chapters": [
        {"chapter_index": 10, "title": "Ten", "start_frame": 0, "start_seconds": 0},
        {"chapter_index": 12, "title": "Twelve", "start_frame": 10, "start_seconds": 1},
    ]}
    audio.with_suffix(".timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    assert load_timeline(audio) == timeline


@pytest.mark.parametrize("indexes", [[10, 10], [12, 10]])
def test_load_timeline_rejects_duplicate_or_regressing_indexes(tmp_path, indexes):
    audio = tmp_path / "bad-indexes.wav"
    sf.write(audio, np.zeros(20), 10)
    timeline = {"version": 1, "sample_rate": 10, "total_frames": 20, "chapters": [
        {"chapter_index": indexes[0], "title": "One", "start_frame": 0, "start_seconds": 0},
        {"chapter_index": indexes[1], "title": "Two", "start_frame": 10, "start_seconds": 1},
    ]}
    audio.with_suffix(".timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    assert load_timeline(audio) is None


def test_valid_short_timeline_loads_but_is_not_added_to_description(tmp_path):
    audio = tmp_path / "short.wav"
    sf.write(audio, np.zeros(20), 10)
    timeline = {"version": 1, "sample_rate": 10, "total_frames": 20,
                 "chapters": [{"chapter_index": 1, "start_frame": 0, "start_seconds": 0.0, "title": "Only"}]}
    audio.with_suffix(".timeline.json").write_text(json.dumps(timeline), encoding="utf-8")
    from app.youtube_metadata import load_timeline, resolve_patch_youtube_metadata
    assert load_timeline(audio) == timeline
    assert "Only" not in resolve_patch_youtube_metadata(_book(), _patch(str(audio)), {})["description"]


def _book():
    return SimpleNamespace(title="Nha Tro", automation_config=json.dumps({"youtube": {
        "description": "book description",
        "genre_tags": "kinh di, huyen huyen",
    }}))


def _patch(audio_path=None):
    return SimpleNamespace(name="Mua", chapter_start=0, chapter_end=7, patch_index=3, audio_path=audio_path)


def _timeline_audio(tmp_path, *, frames=30 * 10, sample_rate=10, chapters=None):
    audio = tmp_path / "episode.wav"
    sf.write(audio, np.zeros(frames), sample_rate)
    sidecar = audio.with_suffix(".timeline.json")
    sidecar.write_text(json.dumps({"version": 1, "sample_rate": sample_rate,
                                   "total_frames": frames,
                                   "chapters": chapters or [
                                       {"chapter_index": 1, "start_frame": 0, "start_seconds": 0, "title": "Intro"},
                                       {"chapter_index": 2, "start_frame": 100, "start_seconds": 10, "title": "Chapter 1"},
                                       {"chapter_index": 3, "start_frame": 200, "start_seconds": 20, "title": "Chapter 2"},
                                   ]}))
    return audio


def _write_timeline(audio, **values):
    timeline = {"version": 1, "sample_rate": 10, "total_frames": 300,
                "chapters": [{"start_frame": 0, "title": "Intro"},
                              {"start_frame": 100, "title": "One"},
                              {"start_frame": 200, "title": "Two"}]}
    timeline.update(values)
    audio.with_suffix(".timeline.json").write_text(json.dumps(timeline))


def test_valid_timeline_is_appended_once_with_floor_and_hour_formatting(tmp_path):
    audio = _timeline_audio(tmp_path, frames=72000, sample_rate=10, chapters=[
        {"chapter_index": 1, "start_frame": 0, "start_seconds": 0, "title": "Intro"},
        {"chapter_index": 2, "start_frame": 100, "start_seconds": 10, "title": "Chapter 1"},
        {"chapter_index": 3, "start_frame": 36000, "start_seconds": 3600, "title": "Chapter 2"},
    ])
    book = _book()
    patch = _patch(str(audio))
    result = resolve_patch_youtube_metadata(book, patch, None)
    assert result["description"] == "book description\n\n00:00 Intro\n00:10 Chapter 1\n1:00:00 Chapter 2"
    assert result["description"].count("Intro") == 1


@pytest.mark.parametrize("chapters", [
    [{"start_frame": 0, "title": "a"}, {"start_frame": 100, "title": "b"}],
    [{"start_frame": 1, "title": "a"}, {"start_frame": 101, "title": "b"}, {"start_frame": 201, "title": "c"}],
    [{"start_frame": 0, "title": "a"}, {"start_frame": 99, "title": "b"}, {"start_frame": 200, "title": "c"}],
    [{"start_frame": 0, "title": "a"}, {"start_frame": 100, "title": "b"}, {"start_frame": 250, "title": "c"}],
    [{"start_frame": 0, "title": " "}, {"start_frame": 100, "title": "b"}, {"start_frame": 200, "title": "c"}],
    [{"start_frame": 0, "title": "a"}, {"start_frame": 100, "title": "b"}, {"start_frame": 100, "title": "c"}],
])
def test_invalid_timeline_preserves_description(tmp_path, chapters):
    audio = _timeline_audio(tmp_path, chapters=chapters)
    result = resolve_patch_youtube_metadata(_book(), _patch(str(audio)), None)
    assert result["description"] == "book description"


def test_missing_or_stale_timeline_preserves_description(tmp_path):
    audio = _timeline_audio(tmp_path)
    audio.with_suffix(".timeline.json").unlink()
    assert resolve_patch_youtube_metadata(_book(), _patch(str(audio)), None)["description"] == "book description"


def test_invalid_utf8_timeline_preserves_description(tmp_path):
    audio = _timeline_audio(tmp_path)
    audio.with_suffix(".timeline.json").write_bytes(b"{\xff")
    assert resolve_patch_youtube_metadata(_book(), _patch(str(audio)), None)["description"] == "book description"


def test_existing_timeline_after_prose_and_blank_line_is_unchanged(tmp_path):
    audio = _timeline_audio(tmp_path)
    _write_timeline(audio, chapters=[{"start_frame": 0, "title": "Intro"},
                                     {"start_frame": 100, "title": "One"},
                                     {"start_frame": 200, "title": "Two"}])
    block = "00:00 Intro\n00:10 One\n00:20 Two"
    book = _book()
    book.automation_config = json.dumps({"youtube": {"description": f"Prose\n\n{block}"}})
    description = resolve_patch_youtube_metadata(book, _patch(str(audio)), None)["description"]
    assert description == f"Prose\n\n{block}"
    assert description.count("00:00 Intro") == 1


@pytest.mark.parametrize("values", [
    {"sample_rate": 0}, {"sample_rate": 11}, {"total_frames": 299},
    {"sample_rate": True}, {"total_frames": False}, {"total_frames": 301},
    {"chapters": [{"start_frame": 0, "title": "a"}, {"start_frame": 100, "title": "b"}, {"start_frame": 301, "title": "c"}]},
])
def test_invalid_timeline_numbers_preserve_description(tmp_path, values):
    audio = _timeline_audio(tmp_path)
    _write_timeline(audio, **values)
    assert resolve_patch_youtube_metadata(_book(), _patch(str(audio)), None)["description"] == "book description"


@pytest.mark.parametrize("start_seconds", [None, True, "10", float("nan"), 10.000000002])
def test_mismatched_or_invalid_start_seconds_preserves_description(tmp_path, start_seconds):
    audio = _timeline_audio(tmp_path, chapters=[
        {"start_frame": 0, "start_seconds": 0, "title": "Intro"},
        {"start_frame": 100, "start_seconds": start_seconds, "title": "Chapter 1"},
        {"start_frame": 200, "start_seconds": 20, "title": "Chapter 2"},
    ])
    assert resolve_patch_youtube_metadata(_book(), _patch(str(audio)), None)["description"] == "book description"


def test_start_seconds_must_match_frame_position_tightly(tmp_path):
    audio = _timeline_audio(tmp_path, chapters=[
            {"chapter_index": 1, "start_frame": 0, "start_seconds": 0.0, "title": "Intro"},
            {"chapter_index": 2, "start_frame": 100, "start_seconds": 10.0, "title": "Chapter 1"},
            {"chapter_index": 3, "start_frame": 200, "start_seconds": 20.0, "title": "Chapter 2"},
    ])
    assert "00:10 Chapter 1" in resolve_patch_youtube_metadata(_book(), _patch(str(audio)), None)["description"]


def test_timeline_append_is_idempotent_for_exact_existing_block(tmp_path):
    audio = _timeline_audio(tmp_path)
    _write_timeline(audio, chapters=[{"start_frame": 0, "title": "Intro"},
                                     {"start_frame": 100, "title": "One"},
                                     {"start_frame": 200, "title": "Two"}])
    book = _book()
    book.automation_config = json.dumps({"youtube": {"description": "00:00 Intro\n00:10 One\n00:20 Two"}})
    result = resolve_patch_youtube_metadata(book, _patch(str(audio)), None)
    assert result["description"].count("00:00 Intro") == 1


@pytest.mark.parametrize("chapters,frames,valid", [
        ([{"chapter_index": 1, "start_frame": 0, "start_seconds": 0, "title": "a"}, {"chapter_index": 2, "start_frame": 100, "start_seconds": 10, "title": "b"}, {"chapter_index": 3, "start_frame": 200, "start_seconds": 20, "title": "c"}], 300, True),
    ([{"start_frame": 0, "start_seconds": 0, "title": "a"}, {"start_frame": 99, "start_seconds": 9.9, "title": "b"}, {"start_frame": 200, "start_seconds": 20, "title": "c"}], 300, False),
    ([{"start_frame": 0, "start_seconds": 0, "title": "a"}, {"start_frame": 100, "start_seconds": 10, "title": "b"}, {"start_frame": 200, "start_seconds": 20, "title": "c"}], 299, False),
])
def test_timeline_ten_second_boundaries(tmp_path, chapters, frames, valid):
    audio = _timeline_audio(tmp_path, frames=frames, chapters=chapters)
    result = resolve_patch_youtube_metadata(_book(), _patch(str(audio)), None)
    assert ("00:00 a" in result["description"]) is valid


def test_default_patch_title_and_tags():
    result = resolve_patch_youtube_metadata(_book(), _patch(), None)
    assert result["title"] == "Nha Tro - Tập 4 - Chương 1-8: Mua | kinh di, huyen huyen"
    assert result["tags"] == ["kinh di", "huyen huyen"]


def test_episode_number_comes_from_patch_index():
    assert "Tập 4" in resolve_patch_youtube_metadata(_book(), _patch(), None)["title"]


def test_legacy_ascii_default_template_is_upgraded():
    book = _book()
    book.automation_config = json.dumps({"youtube": {
        "title_template": "{book_title} - Tap {episode_number} - Chuong {chapter_start}-{chapter_end}: {patch_name} | {genre_tags}",
        "genre_tags": "kinh di",
    }})
    result = resolve_patch_youtube_metadata(book, _patch(), None)
    assert result["title"] == "Nha Tro - Tập 4 - Chương 1-8: Mua | kinh di"


def test_chapter_range_detected_from_patch_name():
    patch = SimpleNamespace(name="Chương 11: Thất thủ", chapter_start=12, chapter_end=21,
                            patch_index=1, audio_path=None)
    result = resolve_patch_youtube_metadata(_book(), patch, None)
    assert result["title"] == "Nha Tro - Tập 2 - Chương 11-20: Thất thủ | kinh di, huyen huyen"


def test_chapter_range_detected_from_timeline(tmp_path):
    audio = _timeline_audio(tmp_path, chapters=[
        {"chapter_index": 2, "start_frame": 0, "start_seconds": 0, "title": "Chương 5: Mở màn"},
        {"chapter_index": 3, "start_frame": 100, "start_seconds": 10, "title": "Chương 6. Giữa"},
        {"chapter_index": 4, "start_frame": 200, "start_seconds": 20, "title": "Chương 7 - Kết"},
    ])
    patch = SimpleNamespace(name="Chương 5: Mở màn", chapter_start=6, chapter_end=8,
                            patch_index=0, audio_path=str(audio))
    result = resolve_patch_youtube_metadata(_book(), patch, None)
    assert "Chương 5-7: Mở màn" in result["title"]


def test_single_chapter_patch_shows_one_number():
    patch = SimpleNamespace(name="Chương 9: Riêng", chapter_start=10, chapter_end=10,
                            patch_index=0, audio_path=None)
    result = resolve_patch_youtube_metadata(_book(), patch, None)
    assert "Chương 9: Riêng" in result["title"]
    assert "9-9" not in result["title"]


def test_fallback_chapter_range_is_1_based():
    """When no timeline/name/chapter_no gives real numbers, fallback adds 1."""
    patch = SimpleNamespace(name="Mua", chapter_start=0, chapter_end=0,
                            patch_index=0, audio_path=None)
    result = resolve_patch_youtube_metadata(_book(), patch, None)
    assert "Chương 1" in result["title"]
    assert "Chương 0" not in result["title"]


def test_fallback_chapter_range_multi_chapter():
    """Fallback 1-based range for multi-chapter patch."""
    patch = SimpleNamespace(name="Interlude", chapter_start=3, chapter_end=7,
                            patch_index=2, audio_path=None)
    result = resolve_patch_youtube_metadata(_book(), patch, None)
    assert "Chương 4-8" in result["title"]


def test_default_description_is_generated_when_config_empty():
    book = _book()
    book.automation_config = json.dumps({"youtube": {"genre_tags": "kinh di"}})
    result = resolve_patch_youtube_metadata(book, _patch(), None)
    assert "Nha Tro" in result["description"]
    assert "Tập 4 - Chương 1-8: Mua" in result["description"]


def _empty_config_book():
    return SimpleNamespace(title="Dị Độ Lữ Xá", automation_config=json.dumps(
        {"youtube": {"genre_tags": "Linh dị, Đô Thị"}}))


def test_default_description_lists_every_chapter_by_name():
    context = {"chapter_titles": ["Chương 1: Mưa", "Chương 2: Nắng", "Chương 3: Gió"]}
    description = resolve_patch_youtube_metadata(
        _empty_config_book(), _patch(), None, context=context)["description"]
    assert "Chương 1: Mưa" in description
    assert "Chương 2: Nắng" in description
    assert "Chương 3: Gió" in description


def test_chapter_list_uses_timeline_timestamps_when_available(tmp_path):
    audio = _timeline_audio(tmp_path, chapters=[
        {"chapter_index": 1, "start_frame": 0, "start_seconds": 0, "title": "Chương 1: Mưa"},
        {"chapter_index": 2, "start_frame": 100, "start_seconds": 10, "title": "Chương 2: Nắng"},
        {"chapter_index": 3, "start_frame": 200, "start_seconds": 20, "title": "Chương 3: Gió"},
    ])
    context = {"chapter_titles": ["Chương 1: Mưa", "Chương 2: Nắng", "Chương 3: Gió"]}
    description = resolve_patch_youtube_metadata(
        _empty_config_book(), _patch(str(audio)), None, context=context)["description"]
    assert "00:00 Chương 1: Mưa" in description
    assert "00:10 Chương 2: Nắng" in description
    assert description.count("Chương 2: Nắng") == 1


def test_default_description_includes_music_name_and_license():
    context = {"music": {"name": "Incredulity", "description": "Ambient",
                         "license": "CC BY 4.0 — Scott Buckley"}}
    description = resolve_patch_youtube_metadata(
        _empty_config_book(), _patch(), None, context=context)["description"]
    assert "Incredulity" in description
    assert "CC BY 4.0 — Scott Buckley" in description


def test_default_description_ends_with_hashtags_from_title_and_genres():
    description = resolve_patch_youtube_metadata(
        _empty_config_book(), _patch(), None)["description"]
    tags = description.strip().split("\n")[-1]
    assert tags.startswith("#")
    assert "#DịĐộLữXá" in tags
    assert "#LinhDị" in tags and "#ĐôThị" in tags


def test_hashtags_use_book_title_before_its_first_dash():
    book = SimpleNamespace(title="Hoàng Hà Phục Yêu Truyện - Tập 1 - Long Phi",
                           automation_config=json.dumps({"youtube": {}}))
    description = resolve_patch_youtube_metadata(book, _patch(), None)["description"]
    assert "#HoàngHàPhụcYêuTruyện" in description
    assert "#Tập1" not in description


def test_configured_description_is_not_replaced_by_generated_sections():
    context = {"chapter_titles": ["Chương 1: Mưa"], "music": {"name": "Incredulity", "license": "CC"}}
    description = resolve_patch_youtube_metadata(_book(), _patch(), None, context=context)["description"]
    assert description == "book description"


def test_generated_description_is_capped_at_youtube_limit():
    context = {"chapter_titles": [f"Chương {i}: " + "x" * 90 for i in range(200)]}
    description = resolve_patch_youtube_metadata(
        _empty_config_book(), _patch(), None, context=context)["description"]
    assert len(description) <= 5000


def test_default_description_still_appends_timeline(tmp_path):
    audio = _timeline_audio(tmp_path)
    book = _book()
    book.automation_config = json.dumps({"youtube": {}})
    result = resolve_patch_youtube_metadata(book, _patch(str(audio)), None)
    assert "Tập 4 - Chương 1-2: Mua" in result["description"]
    assert "00:00 Intro" in result["description"]


def test_optional_title_segments_are_omitted():
    patch = _patch()
    patch.name = ""
    book = _book()
    book.automation_config = json.dumps({"youtube": {"genre_tags": ""}})
    assert resolve_patch_youtube_metadata(book, patch, None)["title"] == "Nha Tro - Tập 4 - Chương 1-8"


def test_empty_patch_name_keeps_non_empty_genre_suffix():
    patch = _patch()
    patch.name = ""
    result = resolve_patch_youtube_metadata(_book(), patch, None)
    assert result["title"] == "Nha Tro - Tập 4 - Chương 1-8 | kinh di, huyen huyen"


def test_patch_override_wins_and_empty_field_inherits():
    result = resolve_patch_youtube_metadata(_book(), _patch(), {"title": "Custom", "description": ""})
    assert result["title"] == "Custom"
    assert result["description"] == "book description"


def test_patch_genre_override_drives_title_and_tags():
    result = resolve_patch_youtube_metadata(_book(), _patch(), {"genre_tags": " mystery, mystery, fantasy "})
    assert result["title"].endswith("| mystery, fantasy")
    assert result["tags"] == ["mystery", "fantasy"]


def test_list_tags_drive_title_and_returned_tags():
    result = resolve_patch_youtube_metadata(_book(), _patch(), {"tags": [" mystery ", "mystery", "fantasy"]})
    assert result["title"].endswith("| mystery, fantasy")
    assert result["tags"] == ["mystery", "fantasy"]


@pytest.mark.parametrize("empty_field", ["patch_name", "genre_tags"])
def test_custom_template_removes_empty_optional_fragment(empty_field):
    book = _book()
    book.automation_config = json.dumps({"youtube": {
        "title_template": "{book_title}: {patch_name} | {genre_tags}",
        "genre_tags": "genres" if empty_field == "patch_name" else "",
    }})
    patch = _patch()
    if empty_field == "patch_name":
        patch.name = ""
    else:
        patch.name = "Name"
    result = resolve_patch_youtube_metadata(book, patch, None)
    assert ":" not in result["title"] if empty_field == "patch_name" else "|" not in result["title"]


def test_explicit_title_always_includes_resolved_genre_suffix():
    result = resolve_patch_youtube_metadata(_book(), _patch(), {"title": " Custom | ", "genre_tags": " mystery "})
    assert result["title"] == "Custom |"


def test_description_template_renders_allowed_values_and_snapshot_playlist_shape():
    book = _book()
    book.automation_config = json.dumps({"youtube": {
        "description": "{book_title} episode {episode_number}",
        "genre_tags": "mystery, fantasy",
    }})
    result = resolve_patch_youtube_metadata(book, _patch(), None)
    assert result["description"] == "Nha Tro episode 4"
    assert set(result["youtube"]) >= {"mode", "playlist_id", "title_template"}


def test_playlist_override_inherits_book_destination():
    book = _book()
    book.automation_config = json.dumps({"youtube": {"playlist": {"mode": "create", "title_template": "{book_title}", "description_template": "desc"}}})
    result = resolve_patch_youtube_metadata(book, _patch(), {"playlist": {"mode": "existing", "playlist_id": "p1"}})
    assert result["youtube"]["playlist_id"] == "p1"
    with pytest.raises(ValueError):
        validate_book_youtube_config({"description": "{unknown}"})


def test_save_override_validates_values(tmp_path):
    conn = db.connect(str(tmp_path / "override.db"))
    db.init_schema(conn)
    with pytest.raises(ValueError):
        save_patch_youtube_override(conn, 1, {"title": 3})
    with pytest.raises(ValueError):
        save_patch_youtube_override(conn, 1, {"tags": ["ok", 3]})
    with pytest.raises(ValueError):
        save_patch_youtube_override(conn, 1, {"privacy_status": "invalid"})
    with pytest.raises(ValueError):
        save_patch_youtube_override(conn, 1, {"playlist": {"mode": "invalid"}})


@pytest.mark.parametrize("config", [{"description": 3}, {"genre_tags": 3}, {"privacy_status": 3}, {"title_template": 3}, {"playlist": "none"}])
def test_config_types_are_validated(config):
    with pytest.raises(ValueError):
        validate_book_youtube_config(config)


def test_invalid_template_syntax_is_rejected():
    with pytest.raises(ValueError):
        validate_book_youtube_config({"title_template": "{broken"})


@pytest.mark.parametrize("template", [
    "{patch_name}",
    "{patch_name}: {patch_name}",
    "x / {patch_name}",
    "{genre_tags}",
    "{genre_tags} | {genre_tags}",
    "x / {genre_tags}",
])
def test_optional_placeholders_require_exact_single_fragments(template):
    with pytest.raises(ValueError):
        validate_book_youtube_config({"title_template": template})


@pytest.mark.parametrize("template", [
    "{book_title}",
    "{book_title}: {patch_name}",
    "{book_title} | {genre_tags}",
    "{book_title}: {patch_name} | {genre_tags}",
])
def test_title_template_accepts_only_valid_suffix_forms(template):
    assert validate_book_youtube_config({"title_template": template})["title_template"] == template


@pytest.mark.parametrize("template", [
    "{patch_name}: {book_title}",
    "{book_title} | {genre_tags} - tail",
    "{book_title}: {patch_name} - {genre_tags}",
    "{book_title} - : {patch_name}",
    "{book_title} /: {patch_name}",
    "{book_title}: {patch_name}: {patch_name}",
])
def test_title_template_rejects_non_suffix_or_empty_separator_forms(template):
    with pytest.raises(ValueError):
        validate_book_youtube_config({"title_template": template})


def test_valid_custom_template_omits_exact_optional_fragments():
    book = _book()
    book.automation_config = json.dumps({"youtube": {
        "title_template": "{book_title}: {patch_name} | {genre_tags}",
        "genre_tags": "",
    }})
    patch = _patch()
    patch.name = ""
    assert resolve_patch_youtube_metadata(book, patch, None)["title"] == "Nha Tro"


def test_resolved_limits_are_validated():
    with pytest.raises(ValueError):
        resolve_patch_youtube_metadata(_book(), _patch(), {"title": "x" * 101})
    with pytest.raises(ValueError):
        resolve_patch_youtube_metadata(_book(), _patch(), {"description": "x" * 5001})


def _long_book(genre_tags="", title_length=60):
    return SimpleNamespace(title="B" * title_length, automation_config=json.dumps({
        "youtube": {"genre_tags": genre_tags}}))


def _long_patch(name_length=50):
    return SimpleNamespace(name="C" * name_length, chapter_start=1, chapter_end=10,
                           patch_index=0, audio_path=None)


def test_generated_title_is_fitted_to_youtube_limit():
    """A long book title plus a long patch name must not blow the 100 char cap."""
    result = resolve_patch_youtube_metadata(_long_book(), _long_patch(), None)
    assert len(result["title"]) <= 100
    assert result["title"].startswith("B" * 60)


def test_generated_title_drops_genre_suffix_before_truncating_text():
    """The genre suffix is the least useful part, so it goes first."""
    book = _long_book(genre_tags="kinh di, huyen huyen", title_length=50)
    result = resolve_patch_youtube_metadata(book, _long_patch(name_length=30), None)
    assert len(result["title"]) <= 100
    assert "kinh di" not in result["title"]
    assert result["tags"] == ["kinh di", "huyen huyen"]


def test_generated_title_keeps_genre_suffix_when_it_still_fits():
    result = resolve_patch_youtube_metadata(_book(), _patch(), None)
    assert result["title"].endswith("| kinh di, huyen huyen")


def test_persistence_and_migration(tmp_path):
    conn = db.connect(str(tmp_path / "metadata.db"))
    db.init_schema(conn)
    assert "youtube_override" in {row["name"] for row in conn.execute("PRAGMA table_info(patch)")}
    conn.execute("INSERT INTO book (title, original_filename, epub_path, patch_size, created_at, updated_at) VALUES ('Book', 'x', 'x', 8, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    book_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute("INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end, created_at, updated_at) VALUES (?, 0, 1, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)", (book_id,))
    patch_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    save_book_youtube_config(conn, book_id, {"genre_tags": "a,b"})
    save_patch_youtube_override(conn, patch_id, {"genre_tags": "x", "description": ""})
    assert get_book_youtube_config(conn, book_id)["genre_tags"] == "a,b"
    assert get_patch_youtube_override(conn, patch_id) == {"genre_tags": "x"}
    conn.execute("UPDATE book SET automation_config = '{bad' WHERE id = ?", (book_id,))
    conn.execute("UPDATE patch SET youtube_override = '{bad' WHERE id = ?", (patch_id,))
    conn.commit()
    assert get_book_youtube_config(conn, book_id)["genre_tags"] == ""
    assert get_patch_youtube_override(conn, patch_id) == {}


# --- timeline toggle + extended description ------------------------------------


def _config(**values):
    return {"description": "book description", "genre_tags": "kinh di", **values}


def test_timeline_is_omitted_when_timeline_display_is_disabled(tmp_path):
    audio = _timeline_audio(tmp_path)
    shown = resolve_patch_youtube_metadata(_book(), _patch(str(audio)), None,
                                           config=_config(timeline_enabled=True))["description"]
    hidden = resolve_patch_youtube_metadata(_book(), _patch(str(audio)), None,
                                            config=_config(timeline_enabled=False))["description"]
    assert "00:10 Chapter 1" in shown
    assert "00:10" not in hidden
    assert hidden == "book description"


def test_description_extra_is_appended_after_the_timeline(tmp_path):
    audio = _timeline_audio(tmp_path)
    extra = {"enabled": True, "template": "Contact: {contact_email}", "contact_email": "me@example.com"}
    description = resolve_patch_youtube_metadata(
        _book(), _patch(str(audio)), None, config=_config(description_extra=extra))["description"]
    assert description.endswith("Contact: me@example.com")
    assert description.index("00:10 Chapter 1") < description.index("Contact:")


def test_description_extra_drops_lines_whose_placeholder_is_blank():
    extra = {"enabled": True, "contact_email": "me@example.com", "story_title": "",
             "template": "Contact: {contact_email}\nTruyen: {story_title}\nAlways"}
    assert render_description_extra(extra) == "Contact: me@example.com\nAlways"


def test_description_extra_is_skipped_when_disabled():
    extra = {"enabled": False, "contact_email": "me@example.com", "template": "Contact: {contact_email}"}
    assert render_description_extra(extra) == ""


def test_description_extra_survives_braces_the_author_typed():
    extra = {"enabled": True, "template": "Literal {not_a_field} stays", "contact_email": "me@example.com"}
    assert render_description_extra(extra) == "Literal {not_a_field} stays"


def test_description_extra_reaches_the_generated_fallback_description():
    extra = {"enabled": True, "template": "Fair use: {fair_use_url}",
             "fair_use_url": "https://example.com/fair-use"}
    description = resolve_patch_youtube_metadata(
        _book(), _patch(), None,
        config=_config(description="", description_extra=extra))["description"]
    assert "Fair use: https://example.com/fair-use" in description


def test_youtube_config_defaults_expose_timeline_and_extra_block():
    config = validate_book_youtube_config({})
    assert config["timeline_enabled"] is True
    assert config["description_extra"]["enabled"] is False
    assert "{contact_email}" in config["description_extra"]["template"]


def test_youtube_config_rejects_a_non_boolean_timeline_flag():
    with pytest.raises(ValueError):
        validate_book_youtube_config({"timeline_enabled": "yes"})


def test_playlist_link_is_appended_to_an_author_written_description():
    playlist = {"mode": "existing", "playlist_id": "PL123"}
    description = resolve_patch_youtube_metadata(
        _book(), _patch(), None, config=_config(playlist=playlist))["description"]
    assert "https://www.youtube.com/playlist?list=PL123" in description
    assert description.startswith("book description")


def test_playlist_link_reaches_the_generated_fallback_description():
    playlist = {"mode": "existing", "playlist_id": "PL456"}
    description = resolve_patch_youtube_metadata(
        _book(), _patch(), None, config=_config(description="", playlist=playlist))["description"]
    lines = description.split("\n")
    assert f"{PLAYLIST_LINK_LABEL} https://www.youtube.com/playlist?list=PL456" in lines
    # Near the top so YouTube shows it without "xem thêm".
    assert lines.index(f"{PLAYLIST_LINK_LABEL} https://www.youtube.com/playlist?list=PL456") < 6


def test_playlist_link_follows_the_patch_override_destination():
    description = resolve_patch_youtube_metadata(
        _book(), _patch(), {"playlist": {"mode": "existing", "playlist_id": "PLoverride"}},
        config=_config(playlist={"mode": "existing", "playlist_id": "PLbook"}))["description"]
    assert "list=PLoverride" in description
    assert "list=PLbook" not in description


def test_playlist_link_is_not_duplicated_when_the_author_already_wrote_it():
    playlist = {"mode": "existing", "playlist_id": "PL789"}
    description = resolve_patch_youtube_metadata(
        _book(), _patch(), None,
        config=_config(description="Nghe tiep: https://www.youtube.com/playlist?list=PL789",
                       playlist=playlist))["description"]
    assert description.count("list=PL789") == 1


def test_no_playlist_means_no_link_block():
    description = resolve_patch_youtube_metadata(
        _book(), _patch(), None, config=_config(description=""))["description"]
    assert "youtube.com/playlist" not in description


# --- Intro offset -------------------------------------------------------------
# Video phát intro trước nội dung patch, nên timeline (đo trên WAV) phải dời theo.

def test_timeline_shifts_by_intro_and_gives_the_intro_its_own_chapter(tmp_path):
    audio = _timeline_audio(tmp_path)
    description = resolve_patch_youtube_metadata(
        _book(), _patch(str(audio)), None, {"intro_seconds": 12.0})["description"]
    assert description == ("book description\n\n00:00 Giới thiệu\n00:12 Intro"
                           "\n00:22 Chapter 1\n00:32 Chapter 2")


def test_short_intro_keeps_the_first_chapter_at_zero(tmp_path):
    # Dưới 10 giây thì YouTube không nhận intro là một chương riêng; mốc đầu phải
    # là 0:00 nếu không cả danh sách chương bị bỏ.
    audio = _timeline_audio(tmp_path)
    description = resolve_patch_youtube_metadata(
        _book(), _patch(str(audio)), None, {"intro_seconds": 4.0})["description"]
    assert description == "book description\n\n00:00 Intro\n00:14 Chapter 1\n00:24 Chapter 2"


def test_timeline_without_intro_is_unshifted(tmp_path):
    audio = _timeline_audio(tmp_path)
    for context in ({}, {"intro_seconds": 0.0}, {"intro_seconds": None}):
        description = resolve_patch_youtube_metadata(_book(), _patch(str(audio)), None, context)["description"]
        assert description == "book description\n\n00:00 Intro\n00:10 Chapter 1\n00:20 Chapter 2"
