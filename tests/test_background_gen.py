"""Unit tests for app.background_gen: prompt building and the per-patch
no-LLM generation flow."""
from __future__ import annotations

import json
import random
from pathlib import Path

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


def _seed_patch(conn, book_id: int, patch_index: int = 0) -> int:
    now = "2026-01-01T00:00:00+00:00"
    cur = conn.execute(
        """INSERT INTO patch (book_id, patch_index, chapter_start, chapter_end,
                              chapter_no_start, chapter_no_end, name,
                              chunk_count, status, created_at, updated_at)
           VALUES (?, ?, 0, 1, 0, 1, 'P0', 1, 'pending', ?, ?)""",
        (book_id, patch_index, now, now),
    )
    conn.commit()
    return cur.lastrowid


def _patch(conn, book_id: int, patch_index: int = 0):
    return repository.get_patch(conn, _seed_patch(conn, book_id, patch_index))


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
# build_patch_prompt + roll_variation
# ---------------------------------------------------------------------------


def test_build_patch_prompt_uses_the_given_style_and_scene():
    scene = background_gen._SCENE_DESCRIPTORS[0]
    prompt = background_gen.build_patch_prompt("anime", scene, genre_tags="Linh dị")
    assert prompt.startswith(scene)
    assert "anime style illustration" in prompt
    assert "supernatural horror" in prompt
    assert all(ord(ch) < 128 for ch in prompt)


def test_build_patch_prompt_falls_back_to_default_style():
    scene = background_gen._SCENE_DESCRIPTORS[1]
    prompt = background_gen.build_patch_prompt("not-a-style", scene)
    assert prompt.startswith(scene)
    assert background_gen._STYLE_TEMPLATES[background_gen.DEFAULT_STYLE] in prompt


def test_roll_variation_returns_usable_values():
    for _ in range(50):
        variation = background_gen.roll_variation()
        assert variation["style"] in background_gen.STYLES
        assert variation["scene"] in background_gen._SCENE_DESCRIPTORS
        assert 0 <= variation["seed"] < 2**31 - 1


def test_roll_variation_differs_between_new_draws():
    first = background_gen.roll_variation(random.Random(1))
    second = background_gen.roll_variation(random.Random(2))
    assert (first["style"], first["scene"], first["seed"]) != (
        second["style"], second["scene"], second["seed"],
    )


def test_roll_variation_is_reproducible_for_the_same_rng():
    rng_a, rng_b = random.Random(42), random.Random(42)
    assert background_gen.roll_variation(rng_a) == background_gen.roll_variation(rng_b)


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
# generate_for_patch
# ---------------------------------------------------------------------------


def test_generate_for_patch_writes_a_managed_copy_and_sets_image_path(conn, monkeypatch):
    monkeypatch.setattr(
        background_gen.requests, "get",
        lambda *a, **k: FakeResponse(b"jpeg-bytes", content_type="image/jpeg"),
    )
    book_id = _seed_book(conn)
    patch = _patch(conn, book_id)
    path = background_gen.generate_for_patch(
        conn, repository.get_book(conn, book_id), patch,
        style="realistic", scene=background_gen._SCENE_DESCRIPTORS[0], seed=1,
    )
    dest = Path(path)
    assert dest.is_file()
    assert dest.read_bytes() == b"jpeg-bytes"
    # Managed per-book folder, not the flat library, and never in video config.
    assert str(dest).startswith(str(Path(settings.data_root) / "backgrounds" / "patch_bg"))
    assert repository.get_patch(conn, patch.id).image_path == str(dest)
    config = get_book_video_config(conn, repository.get_book(conn, book_id))
    assert config["backgrounds"] == []


def test_generate_for_patch_uses_the_books_resolution(conn, monkeypatch):
    seen_sizes = []

    def fake_get(url, params=None, timeout=None):
        seen_sizes.append((params["width"], params["height"]))
        return FakeResponse(b"jpeg-bytes", content_type="image/jpeg")

    monkeypatch.setattr(background_gen.requests, "get", fake_get)
    book_id = _seed_book(conn, resolution="1080x1920")
    patch = _patch(conn, book_id)
    background_gen.generate_for_patch(
        conn, repository.get_book(conn, book_id), patch,
        style="realistic", scene=background_gen._SCENE_DESCRIPTORS[0], seed=1,
    )
    assert seen_sizes == [(1080, 1920)]


def test_generate_for_patch_is_idempotent_on_rerun(conn, monkeypatch):
    calls = {"n": 0}

    def fake_get(url, params=None, timeout=None):
        calls["n"] += 1
        return FakeResponse(b"jpeg-bytes", content_type="image/jpeg")

    monkeypatch.setattr(background_gen.requests, "get", fake_get)
    book_id = _seed_book(conn)
    patch = _patch(conn, book_id)
    book = repository.get_book(conn, book_id)
    first = background_gen.generate_for_patch(
        conn, book, patch,
        style="anime", scene=background_gen._SCENE_DESCRIPTORS[1], seed=7,
    )
    second = background_gen.generate_for_patch(
        conn, book, patch,
        style="anime", scene=background_gen._SCENE_DESCRIPTORS[1], seed=7,
    )
    assert first == second
    assert calls["n"] == 1  # second run is a cache hit, no new fetch


def test_generate_for_patch_raises_on_failure_and_leaves_image_path_unset(conn, monkeypatch):
    monkeypatch.setattr(
        background_gen.requests, "get",
        lambda *a, **k: (_ for _ in ()).throw(background_gen.requests.ConnectionError("boom")),
    )
    book_id = _seed_book(conn)
    patch = _patch(conn, book_id)
    with pytest.raises(background_gen.requests.ConnectionError):
        background_gen.generate_for_patch(
            conn, repository.get_book(conn, book_id), patch,
            style="realistic", scene=background_gen._SCENE_DESCRIPTORS[0], seed=2,
        )
    assert repository.get_patch(conn, patch.id).image_path is None
    config = get_book_video_config(conn, repository.get_book(conn, book_id))
    assert config["backgrounds"] == []


def test_generate_for_patch_respects_cancellation(conn, monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(
        background_gen.requests, "get",
        lambda *a, **k: (calls.__setitem__("n", calls["n"] + 1), FakeResponse(b"x"))[1],
    )
    book_id = _seed_book(conn)
    patch = _patch(conn, book_id)
    with pytest.raises(ValueError, match="cancelled"):
        background_gen.generate_for_patch(
            conn, repository.get_book(conn, book_id), patch,
            style="realistic", scene=background_gen._SCENE_DESCRIPTORS[0], seed=3,
            should_cancel=lambda: True,
        )
    assert calls["n"] == 0
    assert repository.get_patch(conn, patch.id).image_path is None


# ---------------------------------------------------------------------------
# enqueue_for_patches
# ---------------------------------------------------------------------------


def test_enqueue_for_patches_enqueues_one_patch_scoped_job_per_patch(conn):
    book_id = _seed_book(conn)
    patches = [_patch(conn, book_id, idx) for idx in range(3)]
    assert background_gen.enqueue_for_patches(conn, book_id, patches) == 3

    from app.jobqueue import store
    jobs = store.list_jobs(conn, job_type="background_gen")
    assert len(jobs) == 3
    assert {job.patch_id for job in jobs} == {patch.id for patch in patches}
    assert {job.book_id for job in jobs} == {book_id}
    assert {job.dedupe_key for job in jobs} == {f"background_gen:patch={p.id}" for p in patches}
    for job in jobs:
        assert job.payload == {"patch_id": job.patch_id, "book_id": book_id}


def test_enqueue_for_patches_is_deduped_while_a_job_is_live(conn):
    book_id = _seed_book(conn)
    patch = _patch(conn, book_id)
    assert background_gen.enqueue_for_patches(conn, book_id, [patch]) == 1
    assert background_gen.enqueue_for_patches(conn, book_id, [patch]) == 0

    from app.jobqueue import store
    assert store.pending_count(conn, "background_gen") == 1


def test_enqueue_for_patches_allows_a_finished_job_to_run_again(conn):
    book_id = _seed_book(conn)
    patch = _patch(conn, book_id)
    assert background_gen.enqueue_for_patches(conn, book_id, [patch]) == 1

    from app.jobqueue import store
    job = store.list_jobs(conn, job_type="background_gen")[0]
    store.finish(conn, job.id, None)
    assert background_gen.enqueue_for_patches(conn, book_id, [patch]) == 1