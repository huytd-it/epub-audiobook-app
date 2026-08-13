"""Job handler for app.background_gen: auto-generate ONE image per patch.

A patch-scoped job rolls its style/scene/seed on the first attempt and
persists them into the job payload (see roll_variation), so a retry of the
same job reproduces the same prompt + Pollinations seed instead of drifting.
A brand-new patch always creates a brand-new draw.
"""
from __future__ import annotations

from app import background_gen, repository
from app.jobqueue import store
from app.jobqueue.models import JobFatalError


def handle(ctx) -> dict:
    payload = dict(ctx.job.payload)
    patch_id = payload.get("patch_id") or ctx.job.patch_id
    if patch_id is None:
        raise JobFatalError("payload missing patch_id")
    patch = repository.get_patch(ctx.conn, patch_id)
    if patch is None:
        raise JobFatalError(f"patch {patch_id} not found")
    book = repository.get_book(ctx.conn, patch.book_id)
    if book is None:
        raise JobFatalError(f"book {patch.book_id} not found")

    variation = payload.get("variation")
    if not (
        isinstance(variation, dict)
        and variation.get("style") in background_gen.STYLES
        and variation.get("scene") in background_gen._SCENE_DESCRIPTORS
        and isinstance(variation.get("seed"), int)
    ):
        # First attempt (or a payload without a usable draw): roll once and
        # persist, so every later attempt of this job is stable. A new job
        # for a fresh patch always rolls a different draw.
        variation = background_gen.roll_variation()
        store.update_payload(
            ctx.conn, ctx.job.id, {**payload, "variation": variation},
            worker_id=ctx.job.worker_id,
        )

    def on_progress(current: int, total: int) -> None:
        ctx.progress(current, total, phase="fetch")

    # Per-image network failures bubble up as a normal error: the queue retries
    # with backoff, and because the variation is already persisted the retry
    # reproduces the same prompt+seed (a fresh paint, not a different one).
    # JobFatalError stays reserved for payload/patch problems a retry can't fix.
    image_path = background_gen.generate_for_patch(
        ctx.conn, book, patch,
        style=variation["style"], scene=variation["scene"], seed=variation["seed"],
        on_progress=on_progress, should_cancel=ctx.should_cancel,
    )
    ctx.progress(1, 1, phase="done")
    return {"image_path": image_path, "patch_id": patch.id}