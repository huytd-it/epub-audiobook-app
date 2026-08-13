"""Job handler for app.background_gen: auto-populate a book's background pool."""
from __future__ import annotations

from app import background_gen, repository
from app.jobqueue.models import JobFatalError


def handle(ctx) -> dict:
    payload = ctx.job.payload
    book_id = payload.get("book_id") or ctx.job.book_id
    if book_id is None:
        raise JobFatalError("payload missing book_id")
    if repository.get_book(ctx.conn, book_id) is None:
        raise JobFatalError(f"book {book_id} not found")

    count = payload.get("count", background_gen.DEFAULT_COUNT)
    style = payload.get("style", background_gen.DEFAULT_STYLE)
    if not isinstance(count, int) or not 1 <= count <= background_gen.MAX_COUNT:
        raise JobFatalError(f"count must be an integer between 1 and {background_gen.MAX_COUNT}")
    if style not in background_gen.STYLES:
        raise JobFatalError(f"unknown style: {style!r}")

    def on_progress(current: int, total: int) -> None:
        ctx.progress(current, total, phase="fetch")

    # Per-image network failures are retried by generate_for_book falling
    # through to the next slot, not by this handler - what reaches the job
    # queue's own retry/backoff is only "every image in this run failed",
    # which is worth a fresh attempt after a delay in case Pollinations was
    # down, and JobFatalError above for payload errors that a retry can't fix.
    generated = background_gen.generate_for_book(
        ctx.conn, book_id, count=count, style=style,
        on_progress=on_progress, should_cancel=ctx.should_cancel,
    )
    ctx.progress(len(generated), len(generated), phase="done")
    return {"generated": generated}
