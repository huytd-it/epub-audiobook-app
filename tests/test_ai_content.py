"""Sinh mô tả + thẻ YouTube bằng API AI: provider, prompt và parse.

Không test gọi mạng thật — chỉ kiểm tra phần quyết định hành vi: chọn provider
từ .env, dựng prompt đủ ngữ cảnh, và parse được JSON lạc của model (bọc fence,
trả về object lẫn trong text, tags dạng chuỗi).
"""
from __future__ import annotations

import json

import pytest

from app import ai_content
from app.config import settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ("GEMINI_API_KEY", "OPENAI_API_KEY", "AI_CONTENT_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "ai_content_api_key", "")
    monkeypatch.setattr(settings, "ai_content_model", "")
    monkeypatch.setattr(settings, "ai_content_base_url", "")


def test_gemini_provider_reads_its_env_key(monkeypatch):
    monkeypatch.setattr(settings, "ai_content_provider", "")
    monkeypatch.setenv("GEMINI_API_KEY", "secret")
    config = ai_content.resolve_provider()
    assert config.provider == "gemini"
    assert config.model == "gemini-2.5-flash"
    assert config.base_url.startswith("https://generativelanguage.googleapis.com")
    assert config.label == "Gemini · gemini-2.5-flash"


def test_custom_base_url_without_explicit_provider_means_openai_compatible(monkeypatch):
    """Chỉ trỏ base URL vào endpoint riêng thì không được gọi nhầm dạng gemini."""
    monkeypatch.setattr(settings, "ai_content_provider", "")
    monkeypatch.setattr(settings, "ai_content_api_key", "secret")
    monkeypatch.setattr(settings, "ai_content_base_url", "http://127.0.0.1:8080/v1")
    monkeypatch.setattr(settings, "ai_content_model", "local-model")
    config = ai_content.resolve_provider()
    assert (config.provider, config.model) == ("custom", "local-model")


def test_explicit_gemini_provider_ignores_a_stray_base_url(monkeypatch):
    monkeypatch.setattr(settings, "ai_content_provider", "gemini")
    monkeypatch.setattr(settings, "ai_content_base_url", "http://127.0.0.1:8080/v1")
    monkeypatch.setattr(settings, "ai_content_api_key", "secret")
    assert ai_content.resolve_provider().provider == "gemini"


def test_missing_key_is_reported_instead_of_raising_at_status(monkeypatch):
    monkeypatch.setattr(settings, "ai_content_provider", "gemini")
    status = ai_content.provider_status()
    assert status["configured"] is False
    assert "GEMINI_API_KEY" in status["detail"]
    with pytest.raises(ai_content.ProviderNotConfigured):
        ai_content.resolve_provider()


def test_custom_provider_requires_base_url_and_key(monkeypatch):
    monkeypatch.setattr(settings, "ai_content_provider", "custom")
    monkeypatch.setenv("AI_CONTENT_API_KEY", "secret")
    with pytest.raises(ai_content.ProviderNotConfigured):
        ai_content.resolve_provider()
    monkeypatch.setattr(settings, "ai_content_base_url", "https://example.test/v1/")
    config = ai_content.resolve_provider()
    assert config.base_url == "https://example.test/v1"
    assert ai_content.provider_status()["configured"] is True


def test_unknown_provider_names_the_supported_ones(monkeypatch):
    monkeypatch.setattr(settings, "ai_content_provider", "anthropic")
    with pytest.raises(ai_content.ProviderNotConfigured) as excinfo:
        ai_content.resolve_provider()
    assert "gemini" in str(excinfo.value) and "openai" in str(excinfo.value)


def test_explicit_api_key_wins_over_env(monkeypatch):
    monkeypatch.setattr(settings, "ai_content_provider", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    monkeypatch.setattr(settings, "ai_content_api_key", " from-settings ")
    assert ai_content.resolve_provider().api_key == "from-settings"


def test_prompt_carries_book_context_and_json_contract():
    prompt = ai_content.build_prompt(
        "Đại Vệ Chí Dị",
        ["Mở đầu", "Chương 1", "Chương 2"],
        {
            "genre_tags": "tiểu thuyết, đô thị",
            "description_extra": {"story_title": "Đại Vệ Chí Dị", "story_source_name": ""},
        },
    )
    assert "Đại Vệ Chí Dị" in prompt
    assert "Chương 1" in prompt
    assert "tiểu thuyết, đô thị" in prompt
    assert '"tags"' in prompt
    # Mô tả của từng video không nhắc tập số nên prompt cũng không được gợi ý.
    assert "{book_title}" not in prompt


def test_prompt_trims_long_chapter_lists():
    prompt = ai_content.build_prompt("Sách", [f"Chương {i}" for i in range(200)])
    assert "Chương 199" in prompt
    assert "Chương 100" not in prompt


def test_parse_accepts_fenced_json_and_normalizes_tags():
    payload = json.dumps(
        {"description": "  Một cuốn sách nói hay  ", "tags": ["#sách nói", "Audiobook", "sách nói", "Tiểu thuyết"]}
    )
    result = ai_content.parse_metadata(f"Sửa lại nhé:\n```json\n{payload}\n```")
    assert result["description"] == "Một cuốn sách nói hay"
    # Viết hoa/thứ tự giữ nguyên, chỉ khử trùng lặp không phân biệt hoa thường.
    assert result["tags"] == ["sách nói", "Audiobook", "Tiểu thuyết"]
    assert result["genre_tags"] == "sách nói, Audiobook, Tiểu thuyết"


def test_parse_accepts_a_comma_separated_tag_string():
    result = ai_content.parse_metadata(json.dumps({"description": "Mô tả", "tags": "sách nói, trinh thám"}))
    assert result["tags"] == ["sách nói", "trinh thám"]


def test_parse_extracts_an_object_embedded_in_prose():
    text = 'Đây là kết quả: {"description": "Mô tả", "tags": ["a"]} — hết.'
    assert ai_content.parse_metadata(text)["description"] == "Mô tả"


def test_parse_rejects_output_without_usable_fields():
    with pytest.raises(ai_content.GenerationError):
        ai_content.parse_metadata("Tôi không thể giúp việc này.")
    with pytest.raises(ai_content.GenerationError):
        ai_content.parse_metadata(json.dumps({"description": "Mô tả", "tags": []}))


def test_parse_truncates_description_to_the_youtube_budget():
    result = ai_content.parse_metadata(json.dumps({"description": "x" * 9000, "tags": ["a"]}))
    assert len(result["description"]) == ai_content.DESCRIPTION_LIMIT


def test_openai_call_drops_response_format_when_server_rejects_it(monkeypatch):
    calls: list[dict] = []

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict):
            self.status_code = status_code
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self):
            return self._payload

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(json)
        if "response_format" in json:
            return FakeResponse(400, {"error": {"message": "unsupported"}})
        return FakeResponse(200, {"choices": [{"message": {"content": '{"description": "d", "tags": ["t"]}'}}]})

    monkeypatch.setattr(ai_content.requests, "post", fake_post)
    monkeypatch.setattr(settings, "ai_content_provider", "openai")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")

    text = ai_content.complete("prompt")
    assert json.loads(text)["description"] == "d"
    assert "response_format" in calls[0] and "response_format" not in calls[1]


def test_generate_book_metadata_writes_nothing_itself(tmp_path, monkeypatch):
    """Module chỉ sinh dữ liệu; việc lưu thuộc job handler."""
    from datetime import datetime, timezone

    from app import db

    conn = db.connect(":memory:")
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO book (title,original_filename,epub_path,patch_size,status,created_at,updated_at)
           VALUES ('Đại Vệ Chí Dị','b.epub','b.epub',10,'ready',?,?)""",
        (now, now),
    )
    book_id = cur.lastrowid
    conn.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, 0, 'Mở đầu', 'x', 1)",
        (book_id,),
    )
    conn.commit()

    monkeypatch.setattr(ai_content, "resolve_provider", lambda: ai_content.ProviderConfig(
        provider="gemini", model="gemini-2.5-flash", api_key="k", base_url="https://x",
    ))
    monkeypatch.setattr(ai_content, "complete", lambda prompt, config=None: json.dumps(
        {"description": "Mô tả sinh ra", "tags": ["sách nói", "tiểu thuyết"]}
    ))
    result = ai_content.generate_book_metadata(conn, book_id)
    assert result["description"] == "Mô tả sinh ra"
    assert result["genre_tags"] == "sách nói, tiểu thuyết"
    assert conn.execute("SELECT automation_config FROM book WHERE id=?", (book_id,)).fetchone()[0] in (None, "{}")
