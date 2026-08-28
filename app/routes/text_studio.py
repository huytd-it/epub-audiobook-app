"""Text Studio: edit patch text, search/replace, spell check, effect markers — plus the
LightTTS endpoints the Book Detail page drives its per-patch and batch runs from."""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
import time
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.responses import FileResponse, Response, StreamingResponse

from app import audio_merge, repository, text_analysis
from app.config import settings
from app.deps import locked_conn
from app.light_tts import _BACKENDS, LightTTSEngine, _check_backend, list_voices
from app.production_defaults import get_effective_audio_config

logger = logging.getLogger(__name__)

router = APIRouter()

# Same inter-chunk gap the worker merges with, so LightTTS audio (and the chapter
# timeline derived from it) lines up with worker-produced patches.
_CHUNK_PAUSE_MS = 300


def _require_patch(conn, book_id: int, patch_id: int):
    patch = repository.get_patch(conn, patch_id)
    if patch is None or patch.book_id != book_id:
        raise HTTPException(status_code=404, detail="patch not found")
    return patch


def _book_patch_dir(book_id: int) -> Path:
    return Path(settings.data_root) / "books" / str(book_id) / "patches"


def _chunk_dir(book_id: int, patch_id: int) -> Path:
    return _book_patch_dir(book_id) / f"{patch_id}_chunks"


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _mix_effects(wav_bytes: bytes, text: str, conn) -> bytes:
    """Overlay sound-effect clips onto synthesized speech at the marker positions.

    Placement is proportional (marker offset / text length), which is all we can do
    without word timings. Returns the input untouched whenever anything doesn't line
    up — no markers, no matching library entry, a sample-rate mismatch — so a bad
    effect can never corrupt the speech."""
    markers_found: list[tuple[int, str]] = []
    for pattern in text_analysis._EFFECT_PATTERNS:
        for match in pattern.finditer(text):
            markers_found.append((match.start(), match.group(1 if match.lastindex else 0)))
    if not markers_found:
        return wav_bytes

    effect_map: dict[str, dict] = {}
    for effect in repository.list_sound_effects(conn):
        effect_map.setdefault(effect["marker"].strip().lower(), effect)
    if not effect_map:
        return wav_bytes

    try:
        tts_data, tts_sr = sf.read(io.BytesIO(wav_bytes))
    except Exception:
        return wav_bytes
    if tts_data.ndim > 1:
        tts_data = tts_data.mean(axis=1)
    mixed = tts_data.astype(np.float64)
    text_len = len(text)

    for position, raw_marker in markers_found:
        effect = effect_map.get(raw_marker.strip().lower())
        if effect is None:
            continue
        effect_path = effect.get("file_path", "")
        if not effect_path or not Path(effect_path).exists():
            continue
        try:
            eff_data, eff_sr = sf.read(effect_path)
        except Exception:
            continue
        if eff_sr != tts_sr:
            continue
        if eff_data.ndim > 1:
            eff_data = eff_data.mean(axis=1)
        ratio = position / text_len if text_len > 0 else 0
        start = int(ratio * len(mixed))
        end = min(start + len(eff_data), len(mixed))
        if end - start <= 0:
            continue
        mixed[start:end] += eff_data[: end - start]

    peak = np.max(np.abs(mixed))
    if peak > 1.0:
        mixed = mixed / peak

    out_buf = io.BytesIO()
    sf.write(out_buf, mixed, tts_sr, format="WAV")
    return out_buf.getvalue()


def _synth_chunk_with_retries(engine: LightTTSEngine, chunk_text: str, voice: str | None = None) -> bytes:
    """Synthesize one chunk, retrying up to ``light_tts_chunk_retries`` times with a
    short backoff. Blocking — call via asyncio.to_thread. Raises the last error once
    every attempt has failed."""
    attempts = max(1, settings.light_tts_chunk_retries)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            wav_bytes, _ = engine.synthesize_to_wav_bytes(chunk_text, voice)
            return wav_bytes
        except Exception as exc:  # noqa: BLE001 - reported to the client after retries
            last_exc = exc
            if attempt + 1 < attempts:
                time.sleep(0.5 * (attempt + 1))
    raise last_exc


# ---------------------------------------------------------------------------
# Page + text editing
# ---------------------------------------------------------------------------




@router.get("/books/{book_id}/text-studio/patches/{patch_id}")
def get_patch_text(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = _require_patch(conn, book_id, patch_id)
        text = repository.get_effective_patch_text(conn, patch)
        warnings = repository.list_patch_warnings(conn, patch_id)
    return JSONResponse({"text": text, "warnings": warnings, "is_edited": patch.clean_text is not None})


@router.put("/books/{book_id}/text-studio/patches/{patch_id}")
async def save_patch_text(request: Request, book_id: int, patch_id: int):
    body = await request.json()
    text = body.get("text", "")
    with locked_conn(request) as conn:
        _require_patch(conn, book_id, patch_id)
        repository.save_patch_clean_text(conn, patch_id, text)
        plan_inputs = repository.fetch_patch_chunk_inputs(conn, repository.get_patch(conn, patch_id))
    chunk_count = len(await asyncio.to_thread(repository.build_chunk_plan_from_inputs, plan_inputs))
    with locked_conn(request) as conn:
        repository.update_patch_chunk_count(conn, patch_id, chunk_count)
    return JSONResponse({"ok": True, "chunk_count": chunk_count})


@router.post("/books/{book_id}/text-studio/patches/{patch_id}/analyze")
async def analyze_patch_text(request: Request, book_id: int, patch_id: int):
    try:
        client_text = (await request.json()).get("text")
    except Exception:
        client_text = None
    with locked_conn(request) as conn:
        patch = _require_patch(conn, book_id, patch_id)
        text = client_text if client_text is not None else repository.get_effective_patch_text(conn, patch)
        warnings = text_analysis.analyze_text(text)
        repository.save_patch_warnings(conn, patch_id, warnings)
        warnings = repository.list_patch_warnings(conn, patch_id)
    return JSONResponse({"warnings": warnings, "count": len(warnings)})


@router.post("/books/{book_id}/text-studio/patches/{patch_id}/apply-warning")
async def apply_warning(request: Request, book_id: int, patch_id: int):
    body = await request.json()
    warning_id = body.get("warning_id")
    action = body.get("action")  # "accept" or "dismiss"
    if warning_id is None or action not in ("accept", "dismiss"):
        raise HTTPException(status_code=400, detail="warning_id and action required")
    with locked_conn(request) as conn:
        _require_patch(conn, book_id, patch_id)
        repository.update_patch_warning_status(conn, warning_id, 1 if action == "accept" else 2)
    return JSONResponse({"ok": True})


@router.post("/books/{book_id}/text-studio/patches/{patch_id}/replace")
async def search_replace(request: Request, book_id: int, patch_id: int):
    body = await request.json()
    search = body.get("search", "")
    replace = body.get("replace", "")
    is_regex = body.get("is_regex", False)
    if not search:
        raise HTTPException(status_code=400, detail="search text required")
    with locked_conn(request) as conn:
        patch = _require_patch(conn, book_id, patch_id)
        text = repository.get_effective_patch_text(conn, patch)
        if is_regex:
            try:
                new_text, count = re.subn(search, replace, text)
            except re.error as exc:
                raise HTTPException(status_code=400, detail=f"Invalid regex: {exc}")
        else:
            count = text.count(search)
            new_text = text.replace(search, replace)
        repository.save_patch_clean_text(conn, patch_id, new_text)
    return JSONResponse({"text": new_text, "replacements": count})


@router.post("/books/{book_id}/text-studio/replace")
async def replace_book_text(request: Request, book_id: int):
    """Apply one replacement to every patch, preserving any manual patch edits."""
    body = await request.json()
    search = body.get("search", "")
    replace = body.get("replace", "")
    is_regex = body.get("is_regex", False)
    if not search:
        raise HTTPException(status_code=400, detail="search text required")
    try:
        matcher = re.compile(search) if is_regex else None
    except re.error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid regex: {exc}") from exc

    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail="book not found")
        replacements = 0
        changed_patches = 0
        for patch in repository.list_patches(conn, book_id):
            text = repository.get_effective_patch_text(conn, patch)
            if matcher:
                new_text, count = matcher.subn(replace, text)
            else:
                count = text.count(search)
                new_text = text.replace(search, replace)
            if count:
                repository.save_patch_clean_text(conn, patch.id, new_text)
                replacements += count
                changed_patches += 1
    return JSONResponse({"replacements": replacements, "changed_patches": changed_patches})


@router.post("/books/{book_id}/text-studio/patches/{patch_id}/reset")
def reset_patch_text(request: Request, book_id: int, patch_id: int):
    with locked_conn(request) as conn:
        patch = _require_patch(conn, book_id, patch_id)
        repository.reset_patch_clean_text(conn, patch_id)
        text = repository.build_patch_text(conn, patch)
    return JSONResponse({"text": text})


@router.post("/books/{book_id}/text-studio/normalize-preview")
async def normalize_preview(request: Request, book_id: int):
    form = await request.form()
    text = form.get("text", "")
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    from app.normalization import NormalizationOptions, normalize_text

    options = NormalizationOptions(
        numbers=form.get("numbers") == "on",
        junk=form.get("junk") == "on",
        spellcheck=form.get("spellcheck") == "on",
        dictionary=form.get("dictionary") == "on",
        transliteration=form.get("transliteration") == "on",
        abbreviations=form.get("abbreviations") == "on",
        breaks=form.get("breaks") == "on",
    )
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail="book not found")
        rules = repository.list_replace_rules(conn, book_id)
    return JSONResponse({"text": repository.apply_replace_rules(normalize_text(text, options), rules)})


# ---------------------------------------------------------------------------
# LightTTS: backends, voices, chunk listing and synthesis
# ---------------------------------------------------------------------------


@router.get("/text-studio/light-tts/backends")
def list_light_tts_backends():
    backends = []
    for name, info in _BACKENDS.items():
        try:
            _check_backend(name)
            available = True
        except RuntimeError:
            available = False
        backends.append({"id": name, "label": info["description"], "available": available})
    return {"backends": backends}


@router.get("/text-studio/light-tts/voices")
def list_light_tts_voices(backend: str):
    if backend not in _BACKENDS:
        raise HTTPException(status_code=400, detail="unknown backend")
    return {"voices": list_voices(backend)}


@router.get("/books/{book_id}/patches/{patch_id}/chunk-texts")
def patch_chunk_texts(request: Request, book_id: int, patch_id: int, max_chars: int = 0):
    """Split a patch into TTS chunks and return their text (no synthesis, no save).

    Backs the per-chunk preview list. ``max_chars`` follows the same precedence as
    preview-stream: explicit override > per-patch > global default."""
    with locked_conn(request) as conn:
        patch = _require_patch(conn, book_id, patch_id)
        effective_max_chars = max_chars if max_chars > 0 else (patch.max_chars or settings.tts_max_chars)
        plan_inputs = repository.fetch_patch_chunk_inputs(conn, patch, max_chars=effective_max_chars)
    plan = repository.build_chunk_plan_from_inputs(plan_inputs)
    total = len(plan)

    # patch.chunk_count is a fast char-count estimate; the real sentence-aware
    # split differs. When listing with the patch's own config, reconcile the
    # stored estimate so table, progress bar and this list agree. Skipped while
    # processing so resume bookkeeping isn't disturbed.
    reconciled = False
    if max_chars == 0 and patch.status != "processing" and patch.chunk_count != total:
        with locked_conn(request) as conn:
            repository.update_patch_chunk_count(conn, patch_id, total)
        reconciled = True

    return JSONResponse({
        "total": total,
        "max_chars": effective_max_chars,
        "reconciled": reconciled,
        "chunks": [
            {"index": i, "text": item["text"], "chars": len(item["text"])}
            for i, item in enumerate(plan)
        ],
    })


@router.post("/books/{book_id}/patches/reconcile-chunk-counts")
def reconcile_chunk_counts(request: Request, book_id: int):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(status_code=404, detail="book not found")
        stale_ids = repository.list_stale_chunk_count_patch_ids(conn, book_id)
    # This loops over every stale patch in the book, so keeping the lock for the whole
    # walk would stall the app for as long as the slowest book takes to renormalize.
    updated = []
    for patch_id in stale_ids:
        with locked_conn(request) as conn:
            patch = repository.get_patch(conn, patch_id)
            if patch is None or patch.status == "processing":
                continue
            plan_inputs = repository.fetch_patch_chunk_inputs(conn, patch)
        chunk_count = len(repository.build_chunk_plan_from_inputs(plan_inputs))
        with locked_conn(request) as conn:
            repository.update_patch_chunk_count(conn, patch_id, chunk_count)
        updated.append({"id": patch_id, "chunk_count": chunk_count})
    return {"updated": updated}


@router.get("/books/{book_id}/patches/{patch_id}/chunk-audio/{index}")
def serve_patch_chunk_audio(request: Request, book_id: int, patch_id: int, index: int):
    """Serve one persisted chunk WAV so the preview-stream client can play chunks as
    they land (or are reused from an earlier run)."""
    if index < 0:
        raise HTTPException(status_code=400, detail="invalid chunk index")
    with locked_conn(request) as conn:
        _require_patch(conn, book_id, patch_id)
    path = _chunk_dir(book_id, patch_id) / f"chunk_{index:03d}.wav"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="chunk not found")
    return FileResponse(str(path), media_type="audio/wav")


@router.get("/books/{book_id}/patches/{patch_id}/completed-chunks-preview")
def completed_chunks_preview(request: Request, book_id: int, patch_id: int):
    """Describe persisted chunks for the dialog's in-browser progressive preview.

    The client plays these WAVs consecutively instead of creating a temporary merged
    file. Pauses match the production merge plan and each item carries its caption.
    """
    with locked_conn(request) as conn:
        patch = _require_patch(conn, book_id, patch_id)
        book = repository.get_book(conn, book_id)
        inputs = repository.fetch_patch_chunk_inputs(conn, patch)
        audio_config = get_effective_audio_config(conn, book)

    plan = repository.build_chunk_plan_from_inputs(inputs)
    chunk_dir = _chunk_dir(book_id, patch_id)
    try:
        tts_request = json.loads((chunk_dir / ".tts_request.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        tts_request = {
            "tts_engine": audio_config.get("model_id"),
            "voice": audio_config.get("voice_id"),
            "source": "book_settings",
        }
    completed = [
        {"index": index, "text": item["text"], "pause_ms": pause_ms}
        for index, (item, pause_ms) in enumerate(
            zip(
                plan,
                audio_merge.build_pause_plan(
                    plan,
                    audio_config["chunk_pause_ms"],
                    audio_config["chapter_pause_ms"],
                ),
            )
        )
        if (chunk_dir / f"chunk_{index:03d}.wav").is_file()
    ]
    return {"chunks": completed, "tts": tts_request}


@router.post("/books/{book_id}/text-studio/patches/{patch_id}/preview-paragraph")
async def preview_paragraph(request: Request, book_id: int, patch_id: int):
    """Synthesize an arbitrary snippet for listening only — nothing is saved."""
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    with_effects = body.get("with_effects", False)

    with locked_conn(request) as conn:
        _require_patch(conn, book_id, patch_id)
        try:
            engine = LightTTSEngine(
                backend=body.get("tts_engine") or body.get("backend") or settings.light_tts_backend,
                voice=body.get("voice") or settings.light_tts_voice,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=503, detail=str(exc))
        try:
            wav_bytes, _ = await asyncio.to_thread(engine.synthesize_to_wav_bytes, text, body.get("voice") or None)
        except Exception as exc:  # noqa: BLE001 - surfaced to the client
            raise HTTPException(status_code=500, detail=f"TTS synthesis failed: {exc}")
        if with_effects:
            wav_bytes = _mix_effects(wav_bytes, text, conn)

    return Response(content=wav_bytes, media_type="audio/wav")


@router.get("/books/{book_id}/text-studio/patches/{patch_id}/preview-stream")
async def preview_stream(
    request: Request,
    book_id: int,
    patch_id: int,
    backend: str = "",
    tts_engine: str = "",
    voice: str = "",
    with_effects: int = 0,
    max_chars: int = 0,
):
    """Enqueue LightTTS and forward the job log's structured SSE events."""
    from app.jobqueue import joblog, store
    from app.jobqueue.handlers.light_tts import dedupe_key
    from app.jobqueue.models import TERMINAL_STATUSES

    with locked_conn(request) as conn:
        _require_patch(conn, book_id, patch_id)
    key = dedupe_key(patch_id)
    engine = tts_engine or backend or settings.light_tts_backend
    payload = {"patch_id": patch_id, "book_id": book_id, "tts_engine": engine,
               "voice": voice, "max_chars": max_chars, "with_effects": with_effects}
    with locked_conn(request) as conn:
        existing = store.find_live_by_dedupe(conn, key)
        if existing is not None:
            job_id = existing.id
        else:
            job_id = store.enqueue(conn, "light_tts", payload=payload, book_id=book_id, dedupe_key=key)
            if job_id is None:
                live = store.find_live_by_dedupe(conn, key)
                job_id = live.id if live else None
    if job_id is None:
        async def _bail():
            yield _sse({"type": "error", "message": "Không tạo được job LightTTS"})
        return StreamingResponse(_bail(), media_type="text/event-stream")

    async def _bridge():
        cursor = 0
        while True:
            events, cursor = await asyncio.to_thread(joblog.read_events, job_id, from_line=cursor)
            for event in events:
                yield _sse(event)
            with locked_conn(request) as conn:
                job = store.get(conn, job_id)
            if job is None:
                return
            if job.status in TERMINAL_STATUSES:
                tail_events, _ = await asyncio.to_thread(joblog.read_events, job_id, from_line=cursor)
                for event in tail_events:
                    yield _sse(event)
                if job.status == "failed":
                    yield _sse({"type": "error", "message": job.error_message or "job thất bại"})
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.3)
    return StreamingResponse(_bridge(), media_type="text/event-stream")


@router.post("/books/{book_id}/tts/generate")
async def generate_selected_tts(request: Request, book_id: int):
    """Queue selected patches through the common five-model audiobook pipeline."""
    from app.jobqueue.backfill import enqueue_pending_patch_jobs
    from app.tts_engine import normalize_tt_payload, requires_book_reference

    body = await request.json()
    patch_ids = [int(value) for value in body.get("patch_ids") or []]
    auto_upload_youtube = body.get("auto_upload_youtube")
    auto_upload_youtube = True if auto_upload_youtube is True else (
        False if auto_upload_youtube is False else None)
    auto_create_video = body.get("auto_create_video")
    auto_create_video = True if auto_create_video is True else (
        False if auto_create_video is False else None)
    # Upload bao hàm tạo video; nếu operator không truyền cờ nào thì enqueue bỏ trống
    # để handler dùng cờ persisted của sách.
    if auto_upload_youtube is True:
        auto_create_video = True
    try:
        retry_count = int(body.get("retry_count", 2))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="retry_count must be an integer")
    if not 0 <= retry_count <= 10:
        raise HTTPException(status_code=400, detail="retry_count must be between 0 and 10")
    payload = normalize_tt_payload({
        "tts_engine": body.get("model_id"),
        "voice": body.get("voice_id"),
        "max_chars": body.get("max_chars"),
        "with_effects": body.get("with_effects"),
        "tts_options": body.get("tts_options"),
    })
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
        if book is None:
            raise HTTPException(status_code=404, detail="book not found")
        # Only the cloning models need the book's own clip, and only when no preset voice
        # was picked - a "preset:..." voice brings its own (generated) reference along.
        try:
            needs_book_clip = requires_book_reference(payload["tts_engine"], payload["voice"])
        except ValueError as exc:  # a "preset:..." voice naming no real engine/voice
            raise HTTPException(status_code=400, detail=str(exc))
        if needs_book_clip:
            if not book.voice_clip_path or not Path(book.voice_clip_path).is_file():
                raise HTTPException(status_code=400, detail="model requires the book reference voice")
        if payload["tts_engine"] in {"edge-tts", "gtts"} and not payload["voice"]:
            raise HTTPException(status_code=400, detail="model requires a voice or language")
        queued = enqueue_pending_patch_jobs(
            conn, book_id, payload["tts_engine"], voice=payload["voice"],
            max_chars=payload["max_chars"], with_effects=payload["with_effects"],
            tts_options=payload["tts_options"],
            patch_ids=patch_ids, auto_create_video=auto_create_video,
            auto_upload_youtube=auto_upload_youtube, retry_count=retry_count,
            missing_audio_only=True,
        )
    return {"queued": queued, "auto_create_video": auto_create_video,
            "auto_upload_youtube": auto_upload_youtube, "retry_count": retry_count}


def _finish_patch_audio(
    plan: list[dict],
    chunk_paths: list[str],
    audio_path: str,
    patch_id: int,
    with_effects: bool,
    conn,
    db_lock,
) -> None:
    """Post-merge steps shared by every LightTTS entry point: chapter timeline sidecar,
    optional effect mixing, and marking the patch done. Blocking — call off-thread."""
    info = sf.info(audio_path)
    chapters, _ = audio_merge.build_chapter_marks(
        plan, [sf.info(path).frames for path in chunk_paths], info.samplerate, _CHUNK_PAUSE_MS,
    )
    audio_merge.try_write_timeline(
        Path(audio_path).with_suffix(".timeline.json"), info.samplerate, chapters, info.frames,
    )

    if with_effects:
        merged = Path(audio_path).read_bytes()
        with db_lock:
            mixed = _mix_effects(merged, "\n\n".join(item["text"] for item in plan), conn)
        Path(audio_path).write_bytes(mixed)

    with db_lock:
        repository.mark_patch_done(conn, patch_id, audio_path)

