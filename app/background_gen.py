"""Auto-populate a book's shared background rotation without a manual upload.

No LLM is involved: a prompt is assembled from data the app already has (the
book's genre tags, already collected for YouTube metadata - see
youtube_metadata.get_book_youtube_config - plus a fixed visual-style template
and a small set of generic scene descriptors), not from analysing chapter
text. That keeps this dependency-free and free-to-run, at the cost of images
that suit the book's genre/mood rather than any specific scene. The book
title and patch names are deliberately kept out of the prompt text itself:
text-to-image models asked to render arbitrary words tend to draw garbled
glyphs instead of illustrating them, and Vietnamese proper nouns make that
worse. They still shape the per-image seed (see _stable_seed) so the same
book+slot regenerates the same image across retries instead of drifting.

Fetches go through app.material_cache, so re-running generation for a book
(after a partial failure, or just to top up the pool) only pays the network
cost for images it doesn't already have.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote

import requests

from app import material_cache, repository
from app.config import settings
from app.video_config import get_book_video_config, save_book_video_config, validate_media_path
from app.youtube_metadata import get_book_youtube_config, split_tags

logger = logging.getLogger(__name__)

# A book's background pool is a shared rotation (video_config.py's
# "backgrounds"), not one image per patch, so this bounds the whole pool
# rather than scaling with chapter count. High enough to give real variety,
# low enough that one "generate" click can't fire an unbounded burst of
# requests at a free API.
MAX_COUNT = 12

_STYLE_TEMPLATES = {
    "realistic": "photorealistic digital illustration, cinematic lighting, highly detailed",
    "anime": "anime style illustration, vibrant colors, detailed background art",
    "watercolor": "soft watercolor painting, delicate brush strokes, pastel palette",
    "oil_painting": "oil painting, rich textures, dramatic chiaroscuro lighting",
    "fantasy_art": "fantasy concept art, epic atmosphere, dramatic sky, highly detailed",
}
STYLES = frozenset(_STYLE_TEMPLATES)
DEFAULT_STYLE = "realistic"
DEFAULT_COUNT = 4

# Best-effort Vietnamese web-novel genre -> English visual mood. Deliberately
# small and exact-match only: a wrong or partial translation would steer the
# image further from the book than no translation at all, so an unmapped tag
# is dropped rather than guessed at or passed through untranslated.
_GENRE_KEYWORDS = {
    "linh dị": "supernatural horror",
    "kinh dị": "horror",
    "huyền huyễn": "fantasy",
    "huyền huyễn phương tây": "western fantasy",
    "tiên hiệp": "cultivation fantasy",
    "kiếm hiệp": "wuxia martial arts",
    "võ hiệp": "wuxia martial arts",
    "đô thị": "urban contemporary",
    "ngôn tình": "romance",
    "trinh thám": "detective mystery",
    "khoa huyễn": "science fiction",
    "dị giới": "fantasy realm",
    "xuyên không": "isekai time travel fantasy",
    "trọng sinh": "reincarnation drama",
    "quan trường": "political intrigue",
    "gia đấu": "family power struggle drama",
    "cổ đại": "ancient China period",
    "hệ thống": "game system fantasy",
}

# Cycled evenly across the requested count (see build_prompts) so a pool of,
# say, 6 images doesn't repeat the same descriptor back to back.
_SCENE_DESCRIPTORS = [
    "a wide establishing shot",
    "a quiet atmospheric moment",
    "a mysterious hidden location",
    "a tense confrontation",
    "a dramatic turning point",
    "a peaceful transitional scene",
    "an intense climactic moment",
    "a reflective closing scene",
]

_POLLINATIONS_URL = "https://image.pollinations.ai/prompt/{prompt}"
# Pollinations serves JPEG regardless of the request having no format
# parameter, not PNG - checked against the live API rather than assumed, since
# writing JPEG bytes under a ".png" name would work by luck (ffmpeg/PIL sniff
# real image formats by content, not extension) right up until some other
# tool trusts the extension instead.
_CONTENT_TYPE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _translate_genres(genre_tags: str) -> str:
    keywords: list[str] = []
    for tag in split_tags(genre_tags):
        keyword = _GENRE_KEYWORDS.get(tag.strip().lower())
        if keyword and keyword not in keywords:
            keywords.append(keyword)
    return ", ".join(keywords)


def build_prompts(count: int, style: str, genre_tags: str = "") -> list[str]:
    """Return `count` distinct-but-consistent image prompts for a book.

    Every prompt shares the same style and genre mood; only the scene
    descriptor varies, spread evenly across _SCENE_DESCRIPTORS so consecutive
    slots don't read as near-duplicates.
    """
    if count < 1:
        return []
    style_desc = _STYLE_TEMPLATES.get(style, _STYLE_TEMPLATES[DEFAULT_STYLE])
    genre_desc = _translate_genres(genre_tags)
    prompts = []
    for index in range(count):
        slot = index * len(_SCENE_DESCRIPTORS) // count if count > 1 else 0
        descriptor = _SCENE_DESCRIPTORS[min(slot, len(_SCENE_DESCRIPTORS) - 1)]
        parts = [descriptor, genre_desc, style_desc, "no text, no watermark"]
        prompts.append(", ".join(part for part in parts if part))
    return prompts


def _stable_seed(book_id: int, index: int) -> int:
    """Deterministic per-slot seed so re-running generation for the same book
    reproduces the same image instead of drawing a new one every retry."""
    return (book_id * 1_000_003 + index) % (2**31 - 1)


def _backgrounds_dir() -> Path:
    root = Path(settings.data_root) / "backgrounds"
    root.mkdir(parents=True, exist_ok=True)
    return root


def fetch_image(
    conn, prompt: str, width: int, height: int, *, seed: int,
    timeout: float = 30.0, retries: int = 2, backoff_seconds: float = 3.0,
) -> Path:
    """Return a cached image for this exact prompt+size, fetching it from
    Pollinations on a miss.

    Pollinations has no SLA: measured against the live API while building
    this, most requests return in a couple of seconds but a share of them
    read-timeout outright even at a generous 30s. A request that's going to
    fail tends to fail fast (refused/reset) or slow (read timeout) rather
    than with a retryable HTTP status, so the retry loop here covers both -
    it's what actually recovers a transient failure within a single job run,
    rather than pushing every retry up to the job queue's own backoff (which
    would otherwise cost this image's *entire* run, cache hits on the other
    slots included, just to retry the one that timed out).

    Raises the last error once every attempt is exhausted; the caller (see
    generate_for_book) treats that as "this slot failed" and moves on.
    """
    key = material_cache.make_key("pollinations", prompt, width, height)
    cached = material_cache.get(conn, key)
    if cached is not None:
        return cached
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        if attempt:
            time.sleep(backoff_seconds * attempt)
        try:
            response = requests.get(
                _POLLINATIONS_URL.format(prompt=quote(prompt, safe="")),
                params={"width": width, "height": height, "nologo": "true", "seed": seed},
                timeout=timeout,
            )
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0].strip()
            ext = _CONTENT_TYPE_EXT.get(content_type)
            if ext is None:
                raise ValueError(f"unexpected content-type from pollinations: {content_type!r}")
            return material_cache.put(
                conn, key, response.content, source="pollinations",
                prompt=prompt, width=width, height=height, ext=ext,
            )
        except Exception as exc:
            last_error = exc
            logger.warning(
                "background_gen: pollinations attempt %d/%d failed: %s",
                attempt + 1, retries + 1, exc,
            )
    assert last_error is not None
    raise last_error


def generate_for_book(
    conn, book_id: int, *, count: int, style: str,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> list[str]:
    """Generate up to `count` backgrounds for a book and merge them into its
    shared rotation (video_config.py's "backgrounds").

    A per-image failure is logged and skipped rather than aborting the whole
    run - Pollinations has no SLA, and requiring every single image to
    succeed would make the feature unusable on a flaky connection. Only
    "every image failed" is treated as an error, since returning success
    while having generated nothing would silently no-op the request.

    Runs one image at a time (not in parallel): Pollinations is a shared free
    service, and nothing here is latency-sensitive enough to justify the
    extra load a burst of concurrent requests would put on it.
    """
    if count < 1 or count > MAX_COUNT:
        raise ValueError(f"count must be between 1 and {MAX_COUNT}")
    if style not in STYLES:
        raise ValueError(f"unknown style: {style!r}")
    book = repository.get_book(conn, book_id)
    if book is None:
        raise ValueError(f"book {book_id} not found")

    width, height = (int(part) for part in (book.video_resolution or "1920x1080").split("x"))
    genre_tags = get_book_youtube_config(conn, book_id).get("genre_tags", "")
    prompts = build_prompts(count, style, genre_tags)
    dest_dir = _backgrounds_dir()

    generated: list[str] = []
    for index, prompt in enumerate(prompts):
        if should_cancel is not None and should_cancel():
            break
        if on_progress is not None:
            on_progress(index, count)
        try:
            cached_path = fetch_image(conn, prompt, width, height, seed=_stable_seed(book_id, index))
        except Exception:
            logger.warning(
                "background_gen: image %d/%d failed for book %s", index + 1, count, book_id, exc_info=True,
            )
            continue
        cache_key = material_cache.make_key("pollinations", prompt, width, height)
        dest = dest_dir / f"gen_{book_id}_{index:03d}_{cache_key[:8]}{cached_path.suffix}"
        if not dest.exists():
            shutil.copy2(cached_path, dest)
        generated.append(validate_media_path(str(dest), dest_dir))
    if on_progress is not None:
        on_progress(len(prompts), count)
    if not generated:
        raise ValueError("no backgrounds could be generated (every request failed)")

    video_config = get_book_video_config(conn, book)
    merged = list(dict.fromkeys([*video_config["backgrounds"], *generated]))
    save_book_video_config(conn, book_id, {**video_config, "backgrounds": merged})
    return generated
