"""Bulk apply of one production-settings group to many books."""
from __future__ import annotations

import pytest

from app.production_defaults import (bulk_apply_group, get_effective_normalization_config,
                                     get_effective_video_config, get_effective_youtube_config,
                                     get_group_mode, parse_book_config,
                                     save_global_production_defaults)
from app.video_config import save_book_video_config
from tests.test_production_defaults import _book, _client, _conn, _insert_book


def test_values_mode_only_touches_given_fields_and_marks_custom():
    conn = _conn()
    _insert_book(conn, book_id=1)
    _insert_book(conn, book_id=2)
    # Sách 2 có cấu hình video riêng (nền riêng) - phải giữ nguyên sau khi áp.
    save_book_video_config(conn, 2, {"backgrounds": ["D:/bg/a.jpg"], "subtitle_font_size": 40})
    result = bulk_apply_group(conn, "video", [1, 2], "values",
                              {"subtitle_font_size": 72, "subtitle_font_family": "Arial"})
    assert result["updated"] == [1, 2] and result["failed"] == []
    for book_id in (1, 2):
        book = _book(conn, book_id)
        assert get_group_mode(parse_book_config(book), "video", book=book) == "custom"
        video = get_effective_video_config(conn, book)
        assert video["subtitle_font_size"] == 72
        assert video["subtitle_font_family"] == "Arial"
    assert get_effective_video_config(conn, _book(conn, 2))["backgrounds"] == ["D:/bg/a.jpg"]


def test_values_mode_starts_inherited_books_from_global_defaults():
    conn = _conn()
    _insert_book(conn, book_id=1)
    save_global_production_defaults(conn, {"video": {"waveform_enabled": True, "subtitle_color": "#ff0000"}})
    bulk_apply_group(conn, "video", [1], "values", {"subtitle_color": "#00ff00"})
    video = get_effective_video_config(conn, _book(conn, 1))
    assert video["waveform_enabled"] is True
    assert video["subtitle_color"] == "#00ff00"


def test_inherit_mode_points_books_back_at_defaults():
    conn = _conn()
    _insert_book(conn, book_id=1)
    save_book_video_config(conn, 1, {"subtitle_font_size": 40})
    result = bulk_apply_group(conn, "video", [1], "inherit")
    book = _book(conn, 1)
    assert result["updated"] == [1]
    assert get_group_mode(parse_book_config(book), "video", book=book) == "inherit"
    assert get_effective_video_config(conn, book)["subtitle_font_size"] == 60


def test_invalid_values_rejected_once_and_missing_books_reported():
    conn = _conn()
    _insert_book(conn, book_id=1)
    with pytest.raises(ValueError):
        bulk_apply_group(conn, "video", [1], "values", {"subtitle_font_size": 500})
    result = bulk_apply_group(conn, "normalization", [1, 99], "values", {"dictionary": True})
    assert result["updated"] == [1]
    assert result["failed"][0]["book_id"] == 99
    assert get_effective_normalization_config(conn, _book(conn, 1))["dictionary"] is True


def test_youtube_bulk_skips_channel_bound_keys():
    conn = _conn()
    _insert_book(conn, book_id=1)
    before = get_effective_youtube_config(conn, _book(conn, 1))
    bulk_apply_group(conn, "youtube", [1], "values",
                     {"privacy_status": "unlisted", "auto_upload": True,
                      "playlist": {"mode": "existing", "playlist_id": "PL1"}})
    after = get_effective_youtube_config(conn, _book(conn, 1))
    assert after["privacy_status"] == "unlisted"
    assert after["playlist"] == before["playlist"]
    assert after["auto_upload"] == before["auto_upload"]


def test_bulk_endpoints(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        conn = client.app.state.conn
        _insert_book(conn, book_id=1)
        _insert_book(conn, book_id=2)
        listing = client.get("/production-settings/books")
        applied = client.post("/production-settings/bulk-apply", json={
            "group": "video", "mode": "values", "book_ids": [1, 2],
            "values": {"subtitle_background_padding": 30},
        })
        bad = client.post("/production-settings/bulk-apply", json={
            "group": "video", "mode": "values", "book_ids": [], "values": {},
        })
        reloaded = client.get("/production-settings?book_id=2").json()
    assert listing.status_code == 200
    assert {item["id"] for item in listing.json()["items"]} == {1, 2}
    assert applied.status_code == 200, applied.text
    assert applied.json()["updated"] == [1, 2]
    assert bad.status_code == 400
    assert reloaded["modes"]["video"] == "custom"
    assert reloaded["effective"]["video"]["subtitle_background_padding"] == 30
