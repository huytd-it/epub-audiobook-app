"""Unit tests for app.background_gen: prompt building and the no-LLM generation flow."""
from __future__ import annotations

import json

import pytest

from app import background_gen, db, repository
from app.config import settings
from app.video_config import get_book_video_config


class FakeResponse:
    def __init__(self, content: bytes, content_type: str = "image/jpeg", status_code: int = 200):
        self.content = content
        self.headers = {"content-type": content_type}
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise background_gen.requests.HTTPError(f"status {self.status_code}")


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    c = db.connect(":memory:")
    db.init_schema(c)
    return c


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """fetch_image retries transient failures with a real backoff sleep - see
    its docstring. Skip the wait here so a test exercising "every attempt
    fails" doesn't actually spend several seconds sleeping."""
    monkeypatch.setattr(background_gen.time, "sleep", lambda seconds: None)


def _seed_book(conn, *, resolution="1920x1080", genre_tags="Linh dị, Đô Thị") -> int:
    now = "2026-01-01T00:00:00+00:00"
    automation_config = json.dumps({"youtube": {"genre_tags": genre_tags}})
    cur = conn.execute(
        """INSERT INTO book (title,original_filename,epub_path,video_resolution,automation_config,created_at,updated_at)
           VALUES ('Book','book.epub','book.epub',?,?,?,?)""",
        (resolution, automation_config, now, now),
    )
    conn.commit()
    return cur.lastrowid


# ---------------------------------------------------------------------------
# build_prompts
# ---------------------------------------------------------------------------


def test_build_prompts_returns_the_requested_count():
    prompts = background_gen.build_prompts(5, "realistic")
    assert len(prompts) == 5
    assert all(isinstance(p, str) and p for p in prompts)


def test_build_prompts_is_empty_for_zero_count():
    assert background_gen.build_prompts(0, "realistic") == []


def test_build_prompts_includes_the_style_template():
    prompts = background_gen.build_prompts(1, "watercolor")
    assert "watercolor painting" in prompts[0]


def test_build_prompts_falls_back_to_default_style_for_unknown_style():
    prompts = background_gen.build_prompts(1, "not-a-real-style")
    assert background_gen._STYLE_TEMPLATES[background_gen.DEFAULT_STYLE] in prompts[0]


def test_build_prompts_translates_known_genre_tags():
    prompts = background_gen.build_prompts(1, "realistic", genre_tags="Linh dị, Đô Thị")
    assert "supernatural horror" in prompts[0]
    assert "urban contemporary" in prompts[0]


def test_build_prompts_drops_unmapped_genre_tags_silently():
    prompts = background_gen.build_prompts(1, "realistic", genre_tags="Không có trong bảng ánh xạ")
    assert "Không có" not in prompts[0]


def test_build_prompts_never_puts_vietnamese_title_text_in_the_prompt():
    # Regression guard for the design choice in the module docstring: literal
    # Vietnamese proper nouns must never reach the prompt text, since
    # text-to-image models tend to render them as garbled glyphs.
    prompts = background_gen.build_prompts(3, "realistic", genre_tags="Huyền Huyễn")
    for prompt in prompts:
        assert all(ord(ch) < 128 for ch in prompt)


def test_build_prompts_varies_the_scene_descriptor_across_slots():
    prompts = background_gen.build_prompts(len(background_gen._SCENE_DESCRIPTORS), "realistic")
    descriptors_used = {p.split(",")[0] for p in prompts}
    assert len(descriptors_used) == len(background_gen._SCENE_DESCRIPTORS)


# ---------------------------------------------------------------------------
# fetch_image
# ---------------------------------------------------------------------------


def test_fetch_image_caches_a_fresh_fetch(conn, monkeypatch):
    calls = []

    def fake_get(url, params=None, timeout=None):
        calls.append((url, params))
        return FakeResponse(b"jpeg-bytes", content_type="image/jpeg")

    monkeypatch.setattr(background_gen.requests, "get", fake_get)
    path = background_gen.fetch_image(conn, "a red fox", 512, 512, seed=1)
    assert path.suffix == ".jpg"
    assert path.read_bytes() == b"jpeg-bytes"
    assert len(calls) == 1

    # Second call for the same prompt/size must hit the cache, not the network.
    background_gen.fetch_image(conn, "a red fox", 512, 512, seed=1)
    assert len(calls) == 1


def test_fetch_image_rejects_an_unexpected_content_type(conn, monkeypatch):
    monkeypatch.setattr(
        background_gen.requests, "get",
        lambda *a, **k: FakeResponse(b"<html>error</html>", content_type="text/html"),
    )
    with pytest.raises(ValueError):
        background_gen.fetch_image(conn, "a red fox", 512, 512, seed=1)


def test_fetch_image_retries_a_transient_failure_and_succeeds(conn, monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise background_gen.requests.ReadTimeout("timed out")
        return FakeResponse(b"jpeg-bytes", content_type="image/jpeg")

    monkeypatch.setattr(background_gen.requests, "get", fake_get)
    path = background_gen.fetch_image(conn, "a red fox", 512, 512, seed=1)
    assert path.read_bytes() == b"jpeg-bytes"
    assert calls["n"] == 3


def test_fetch_image_raises_once_every_retry_is_exhausted(conn, monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        raise background_gen.requests.ReadTimeout("timed out")

    monkeypatch.setattr(background_gen.requests, "get", fake_get)
    with pytest.raises(background_gen.requests.ReadTimeout):
        background_gen.fetch_image(conn, "a red fox", 512, 512, seed=1)
    assert calls["n"] == 3  # 1 initial attempt + 2 retries (fetch_image's default)


# ---------------------------------------------------------------------------
# generate_for_book
# ---------------------------------------------------------------------------


def test_generate_for_book_rejects_an_unknown_book(conn):
    with pytest.raises(ValueError):
        background_gen.generate_for_book(conn, 999, count=2, style="realistic")


@pytest.mark.parametrize("count,style", [(0, "realistic"), (99, "realistic"), (2, "not-a-style")])
def test_generate_for_book_rejects_invalid_count_or_style(conn, count, style):
    book_id = _seed_book(conn)
    with pytest.raises(ValueError):
        background_gen.generate_for_book(conn, book_id, count=count, style=style)


def test_generate_for_book_merges_generated_paths_into_video_config(conn, monkeypatch):
    monkeypatch.setattr(
        background_gen.requests, "get",
        lambda *a, **k: FakeResponse(b"jpeg-bytes", content_type="image/jpeg"),
    )
    book_id = _seed_book(conn)
    generated = background_gen.generate_for_book(conn, book_id, count=3, style="realistic")
    assert len(generated) == 3

    book = repository.get_book(conn, book_id)
    config = get_book_video_config(conn, book)
    assert set(generated) <= set(config["backgrounds"])


def test_generate_for_book_uses_the_books_resolution(conn, monkeypatch):
    seen_sizes = []

    def fake_get(url, params=None, timeout=None):
        seen_sizes.append((params["width"], params["height"]))
        return FakeResponse(b"jpeg-bytes", content_type="image/jpeg")

    monkeypatch.setattr(background_gen.requests, "get", fake_get)
    book_id = _seed_book(conn, resolution="1080x1920")
    background_gen.generate_for_book(conn, book_id, count=1, style="realistic")
    assert seen_sizes == [(1080, 1920)]


def test_generate_for_book_skips_failed_images_but_keeps_the_rest(conn, monkeypatch):
    # The failing slot exhausts every retry (see _no_retry_backoff), so it has
    # to fail consistently for its own prompt rather than on a global call
    # count - a fixed call number would instead land on one of that slot's
    # own retry attempts and get silently "fixed" by the retry loop.
    failing_prompt = background_gen.build_prompts(3, "realistic", genre_tags="Linh dị, Đô Thị")[1]
    failing_url = background_gen._POLLINATIONS_URL.format(
        prompt=background_gen.quote(failing_prompt, safe="")
    )

    def fake_get(url, params=None, timeout=None):
        if url == failing_url:
            raise background_gen.requests.ConnectionError("boom")
        return FakeResponse(b"jpeg-bytes", content_type="image/jpeg")

    monkeypatch.setattr(background_gen.requests, "get", fake_get)
    book_id = _seed_book(conn)
    generated = background_gen.generate_for_book(conn, book_id, count=3, style="realistic")
    assert len(generated) == 2


def test_generate_for_book_raises_when_every_image_fails(conn, monkeypatch):
    monkeypatch.setattr(
        background_gen.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(background_gen.requests.ConnectionError("boom")),
    )
    book_id = _seed_book(conn)
    with pytest.raises(ValueError):
        background_gen.generate_for_book(conn, book_id, count=2, style="realistic")


def test_generate_for_book_does_not_touch_config_when_every_image_fails(conn, monkeypatch):
    book_id = _seed_book(conn)
    book = repository.get_book(conn, book_id)
    before = get_book_video_config(conn, book)

    monkeypatch.setattr(
        background_gen.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(background_gen.requests.ConnectionError("boom")),
    )
    with pytest.raises(ValueError):
        background_gen.generate_for_book(conn, book_id, count=2, style="realistic")

    after = get_book_video_config(conn, book)
    assert before["backgrounds"] == after["backgrounds"]


def test_generate_for_book_is_idempotent_on_rerun(conn, monkeypatch):
    monkeypatch.setattr(
        background_gen.requests, "get",
        lambda *a, **k: FakeResponse(b"jpeg-bytes", content_type="image/jpeg"),
    )
    book_id = _seed_book(conn)
    first = background_gen.generate_for_book(conn, book_id, count=2, style="realistic")
    second = background_gen.generate_for_book(conn, book_id, count=2, style="realistic")
    assert first == second

    book = repository.get_book(conn, book_id)
    config = get_book_video_config(conn, book)
    assert sorted(config["backgrounds"]) == sorted(set(first))


def test_generate_for_book_respects_cancellation(conn, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(
        background_gen.requests, "get",
        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), FakeResponse(b"x"))[1],
    )
    book_id = _seed_book(conn)
    generated = background_gen.generate_for_book(
        conn, book_id, count=5, style="realistic", should_cancel=lambda: calls["n"] >= 1,
    )
    assert len(generated) == 1
    assert calls["n"] == 1
