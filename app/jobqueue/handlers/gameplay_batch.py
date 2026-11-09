"""Expand a planned gameplay batch into clips in bounded, resumable chunks.

A replay simulates a full match (0.1-2s depending on the game), so a 1000-clip
request cannot be expanded inside the API call. The route only records the plan;
this handler turns it into clip rows on the worker, ``CHUNK`` at a time, and
re-queues itself for the next slice. Keeping each run short means a restart (or
the reaper) loses at most one slice, and the frozen ``offset`` in the payload
makes expansion resume exactly where it stopped.
"""
from __future__ import annotations

from pathlib import Path

from app.gameplay_pool import expand_batch_slice
from app.jobqueue import store
from app.jobqueue.models import JobFatalError

# Clips per job run. At ~1s per replay this keeps a slice around 20s, far under
# the 120s reaper window, while still making visible progress on the queue page.
CHUNK = 20


def handle(ctx) -> dict:
    payload = ctx.job.payload or {}
    game_ids = list(payload.get("game_ids") or [])
    request_id = str(payload.get("request_id") or "")
    requested = int(payload.get("requested") or 0)
    offset = int(payload.get("offset") or 0)
    if not game_ids or not request_id or requested <= 0 or offset >= requested:
        raise JobFatalError("gameplay batch payload is incomplete or already expanded")

    dims = payload.get("resolution") or [1920, 1080]
    dest = payload.get("dest_dir")
    result = expand_batch_slice(
        ctx.conn, game_ids=game_ids, request_id=request_id, offset=offset,
        count=min(CHUNK, requested - offset),
        width=int(dims[0]), height=int(dims[1]), fps=int(payload.get("fps") or 30),
        quality=int(payload.get("quality") or 23), config=payload.get("config") or None,
        dest_dir=Path(dest) if dest else None,
    )
    ctx.progress(result["next_offset"], requested, "expand")

    remaining = requested - result["next_offset"]
    if remaining > 0 and not ctx.should_cancel():
        store.enqueue(ctx.conn, "gameplay_batch",
                      payload={**payload, "offset": result["next_offset"]},
                      dedupe_key=f"gameplay_batch:{request_id}:{result['next_offset']}",
                      max_attempts=3)
    return {"batch_request": request_id, "expanded": result["created"],
            "next_offset": result["next_offset"], "remaining": remaining}
