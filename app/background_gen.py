"""Auto-generate ONE background image per patch, stored in a managed folder.

No LLM is involved: a prompt is assembled from data the app already has (the
book's genre tags, already collected for YouTube metadata - see
youtube_metadata.get_book_youtube_config - plus a fixed visual-style template
and a generic scene descriptor), not from analysing chapter text. That keeps
this dependency-free and free-to-run, at the cost of images that suit the
book's genre/mood rather than any specific scene. The book title and patch
name are deliberately kept out of the prompt text itself: text-to-image
models asked to render arbitrary words tend to draw garbled glyphs instead of
illustrating them, and Vietnamese proper nouns make that worse.

Each patch gets exactly one image. Its style, scene and Pollinations seed are
rolled once per job (see roll_variation) and persisted into the job payload
on the first attempt, so a retry of the same job reproduces the same
request - and, through the content-addressed app.material_cache, the same
bytes - while a freshly created patch always starts from a brand-new draw.

Generated images are copied out of the cache into a managed directory,
data/backgrounds/patch_bg/<book_id>/, and the patch row's image_path is
pointed at that copy. Nothing is merged into the shared video config's
"backgrounds" list and nothing lands in the flat data/backgrounds library:
every listing endpoint (routes/video.py's /video/backgrounds, routes/photos,
routes/ui_api.py's /media, routes/books._list_backgrounds) iterates that
folder non-recursively, so a nested patch_bg/ directory is invisible to it.
The copy also survives material_cache GC, which only deletes cache entries
whose file_path is not referenced - patch.image_path is referenced (see
media_library.referenced_media_paths) - so the managed file is protected
while the cache row behind it can be reclaimed like any other.
"""
from __future__ import annotations

import logging
import random
import shutil
import time
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import quote

import requests

from app import material_cache, repository
from app.config import settings
from app.jobqueue import store as job_store
from app.youtube_metadata import get_book_youtube_config, split_tags

logger = logging.getLogger(__name__)

_STYLE_TEMPLATES = {
    "realistic": "photorealistic digital illustration, cinematic lighting, highly detailed",
    "anime": "anime style illustration, vibrant colors, detailed background art",
    "watercolor": "soft watercolor painting, delicate brush strokes, pastel palette",
    "oil_painting": "oil painting, rich textures, dramatic chiaroscuro lighting",
    "fantasy_art": "fantasy concept art, epic atmosphere, dramatic sky, highly detailed",
}
STYLES = frozenset(_STYLE_TEMPLATES)
_STYLES_ORDERED = tuple(_STYLE_TEMPLATES)
DEFAULT_STYLE = "realistic"

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

# A single patch job picks one of these at random (see roll_variation); the
# list is generic so the draw always yields a usable description.
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


def _assemble_prompt(descriptor: str, genre_desc: str, style_desc: str) -> str:
    parts = [descriptor, genre_desc, style_desc, "no text, no watermark"]
    return ", ".join(part for part in parts if part)


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
        prompts.append(_assemble_prompt(descriptor, genre_desc, style_desc))
    return prompts


def build_patch_prompt(style: str, scene: str, genre_tags: str = "") -> str:
    """Assemble the single prompt for one patch's background image."""
    style_desc = _STYLE_TEMPLATES.get(style, _STYLE_TEMPLATES[DEFAULT_STYLE])
    genre_desc = _translate_genres(genre_tags)
    return _assemble_prompt(scene, genre_desc, style_desc)


def roll_variation(rng: random.Random | None = None) -> dict:
    """Fresh random style + scene + seed for a NEW patch background job.

    Called once per job (on its first attempt) and persisted into the job
    payload: later retries of the same job reuse the same variation, so they
    reproduce the same request instead of drifting, while a newly created
    patch always rolls a different draw.
    """
    rng = rng or random
    return {
        "style": rng.choice(_STYLES_ORDERED),
        "scene": rng.choice(_SCENE_DESCRIPTORS),
        "seed": rng.randrange(2**31 - 1),
    }


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
    rather than pushing every retry up to the job queue's own backoff.

    Raises the last error once every attempt is exhausted; the caller (see
    generate_for_patch) lets that bubble up to the job queue, which retries
    the job with the same variation (and therefore the same prompt+seed, so
    the cache keeps a retry from drifting to a different image).
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


def _managed_patch_dir(book_id: int) -> Path:
    """Per-book managed folder for generated patch backgrounds.

    A nested subdirectory of data/backgrounds, so it never shows up in the
    media/photo/backgrounds listings (they iterate non-recursively) while
    still living next to the rest of the media data.
    """
    root = Path(settings.data_root) / "backgrounds" / "patch_bg" / str(book_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def generate_for_patch(
    conn,
    book,
    patch,
    *,
    style: str,
    scene: str,
    seed: int,
    on_progress: Callable[[int, int], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    timeout: float = 30.0,
    retries: int = 2,
    backoff_seconds: float = 3.0,
) -> str:
    """Generate ONE background for a patch and save it where the patch
    expects it: a copy in the book's managed patch_bg folder, referenced by
    patch.image_path.

    Returns the managed absolute path. Raises the underlying fetch error when
    the image could not be produced - a job retry reruns this with the same
    style/scene/seed, so the retry converges on the same image instead of
    painting something new. Never touches the shared video config's
    "backgrounds" list nor the flat library folder.
    """
    width, height = (int(part) for part in (book.video_resolution or "1920x1080").split("x"))
    from app.production_defaults import get_effective_youtube_config
    genre_tags = get_effective_youtube_config(conn, book).get("genre_tags", "")
    prompt = build_patch_prompt(style, scene, genre_tags)
    if on_progress is not None:
        on_progress(0, 1)
    if should_cancel is not None and should_cancel():
        raise ValueError("background generation cancelled")
    cached = fetch_image(
        conn, prompt, width, height, seed=seed,
        timeout=timeout, retries=retries, backoff_seconds=backoff_seconds,
    )
    cache_key = material_cache.make_key("pollinations", prompt, width, height)
    dest = _managed_patch_dir(book.id) / f"patch_{patch.id}_{cache_key[:8]}{cached.suffix}"
    if not dest.exists():
        shutil.copy2(cached, dest)
    path = str(dest)
    repository.save_patch_image(conn, patch.id, path)
    if on_progress is not None:
        on_progress(1, 1)
    return path


def enqueue_for_patches(conn, book_id: int, patches: Iterable) -> int:
    """Enqueue a patch-scoped background_gen job for each given patch.

    NOT called when patches are built. Creating patches (rebuild/auto-build/
    extend) deliberately queues nothing, so a build never blocks on - or logs
    failures from - the slow Pollinations image call; callers that actually
    want images must invoke this explicitly.

    Must only be called AFTER the patch rows are committed: the repository
    builders (rebuild_patches/auto_build_patches/extend_patches) commit
    before returning, but store.enqueue drops job.patch_id for any patch it
    cannot see in the DB, which would make the job fatal on a nonexistent
    patch - so enqueuing before the insert commits is a correctness bug, not
    just a race.

    Dedupe is per patch (key "background_gen:patch=<id>", enforced by the
    partial unique index on pending/running jobs): at most one live job can
    exist per patch, and a finished job never blocks a later re-run. Each
    enqueue draws its randomness only when the job first runs, so a new job
    for a fresh patch starts from a different draw.
    """
    queued = 0
    for patch in patches:
        patch_id = patch.id if not isinstance(patch, int) else patch
        if patch_id is None:
            continue
        if job_store.enqueue(
            conn,
            "background_gen",
            payload={"patch_id": patch_id, "book_id": book_id},
            book_id=book_id,
            patch_id=patch_id,
            dedupe_key=f"background_gen:patch={patch_id}",
        ) is not None:
            queued += 1
    return queued
