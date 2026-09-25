"""AI task builders: book context + prompt construction + result persistence.

Extensibility: every generatable artifact is a *task* registered via
:func:`register_task`. A task is ``name -> builder(context) -> {system, user,
max_tokens, response_format}`` plus an optional ``saver(conn, book, result)``.
New features (SEO keywords, chapter summaries, playlist descriptions, ...)
only add a task here — the ``/ai/generate`` route dispatches generically.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_EXCERPT_CHARS = 4000
MAX_CHAPTER_TITLES = 30


# ---------------------------------------------------------------------------
# Book context — everything the AI needs to generate *about this book*
# ---------------------------------------------------------------------------

def build_book_context(conn, book, *, max_excerpt_chars: int = MAX_EXCERPT_CHARS) -> dict[str, Any]:
    """Collect the facts AI prompts are grounded in: real EPUB metadata saved
    at upload time + chapter titles + a short excerpt of chapter 1.

    Reads ``chapter`` rows directly so callers don't need model objects.
    """
    chapters = conn.execute(
        "SELECT title, text FROM chapter WHERE book_id = ? ORDER BY chapter_index",
        (book.id,),
    ).fetchall()
    titles = [str(r["title"] or "").strip() for r in chapters if str(r["title"] or "").strip()]
    excerpt = ""
    if chapters:
        excerpt = str(chapters[0]["text"] or "").strip()[:max_excerpt_chars]
    total_chars = sum(len(str(r["text"] or "")) for r in chapters)
    subjects = [s.strip() for s in str(getattr(book, "subjects", "") or "").split(",") if s.strip()]
    return {
        "book_id": book.id,
        "title": getattr(book, "title", "") or "",
        "author": getattr(book, "author", "") or "",
        "description": getattr(book, "description", "") or "",
        "language": getattr(book, "language", "") or "",
        "publisher": getattr(book, "publisher", "") or "",
        "subjects": subjects,
        "chapter_count": len(chapters),
        "chapter_titles": titles[:MAX_CHAPTER_TITLES],
        "chapter_titles_truncated": len(titles) > MAX_CHAPTER_TITLES,
        "total_chars": total_chars,
        "excerpt_chapter_1": excerpt,
        "has_cover": bool(getattr(book, "cover_image_path", None)),
    }


def context_to_brief(ctx: dict[str, Any]) -> str:
    """Render the context as a compact brief block embedded in prompts."""
    lines = [f"- Tên sách: {ctx['title'] or '(chưa rõ)'}"]
    if ctx["author"]:
        lines.append(f"- Tác giả: {ctx['author']}")
    if ctx["language"]:
        lines.append(f"- Ngôn ngữ: {ctx['language']}")
    if ctx["publisher"]:
        lines.append(f"- Nhà xuất bản: {ctx['publisher']}")
    if ctx["subjects"]:
        lines.append(f"- Thể loại: {', '.join(ctx['subjects'])}")
    if ctx["description"]:
        lines.append(f"- Tóm tắt gốc: {ctx['description'][:800]}")
    lines.append(f"- Số chương: {ctx['chapter_count']} (~{ctx['total_chars']:,} ký tự)")
    if ctx["chapter_titles"]:
        shown = ctx["chapter_titles"][:12]
        more = f" (+{len(ctx['chapter_titles']) - len(shown)} chương nữa)" if len(ctx["chapter_titles"]) > len(shown) else ""
        lines.append(f"- Các chương đầu: {'; '.join(shown)}{more}")
    if ctx["excerpt_chapter_1"]:
        lines.append(f"- Trích đoạn chương 1: {ctx['excerpt_chapter_1'][:1500]}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Task registry
# ---------------------------------------------------------------------------

@dataclass
class AITask:
    name: str
    label: str
    builder: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]
    saver: Callable | None = None  # (conn, book, result, params) -> dict summary


_TASKS: dict[str, AITask] = {}


def register_task(name: str, label: str, builder, saver=None) -> None:
    _TASKS[name.strip().lower()] = AITask(name.strip().lower(), label, builder, saver)


def list_tasks() -> list[dict[str, str]]:
    return [{"name": t.name, "label": t.label} for t in _TASKS.values()]


def get_task(name: str) -> AITask:
    task = _TASKS.get((name or "").strip().lower())
    if task is None:
        raise ValueError(f"Task AI {name!r} chưa có (đã có: {', '.join(sorted(_TASKS)) or '—'})")
    return task


# ---------------------------------------------------------------------------
# Built-in tasks
# ---------------------------------------------------------------------------

def _build_youtube_content(ctx: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    lang = (params.get("language") or ctx["language"] or "vi").strip() or "vi"
    lang_name = "Tiếng Việt" if lang.lower().startswith("vi") else lang
    brief = context_to_brief(ctx)
    system = (
        "Bạn là biên tập viên nội dung YouTube cho kênh sách nói. "
        f"Luôn viết bằng {lang_name}. Trả về JSON object duy nhất, không markdown."
    )
    user = (
        "Dựa trên thông tin sách sau, hãy tạo nội dung đăng YouTube cho CẢ BỘ SÁCH "
        "(playlist/description cấp sách, không chia tập):\n"
        f"{brief}\n\n"
        "Yêu cầu JSON (đúng các key này):\n"
        "- playlist_title: tên playlist ≤ 100 ký tự, dạng \"Tên Sách | Sách Nói ...\" "
        "(giữ đúng tên sách & tác giả đã cho, không bịa tên khác).\n"
        "- playlist_description: 3-6 câu giới thiệu hấp dẫn + lời mời nghe trọn bộ, ≤ 800 ký tự.\n"
        "- video_title_template: mẫu tiêu đề từng tập, giữ nguyên các placeholder "
        "{book_title} {episode_number} {chapter_start} {chapter_end} {patch_name} {genre_tags}.\n"
        "- video_description: mô tả mẫu từng tập 4-8 câu (có chỗ cho timeline chương), ≤ 1500 ký tự.\n"
        "- genre_tags: chuỗi tags phân tách dấu phẩy, 5-10 tags (thể loại + \"sach noi, audiobook, truyen...\").\n"
        "- thumbnail_prompt: 1 câu prompt tiếng Anh vẽ bìa sách (không chữ), phong cách cinematic."
    )
    return {"system": system, "user": user, "max_tokens": 1500, "response_format": "json"}


def _save_youtube_content(conn, book, result: dict, params: dict) -> dict:
    """Persist the draft: AI JSON -> ai_content_json + prefill youtube config."""
    from app.youtube_metadata import get_book_youtube_config, save_book_youtube_config

    conn.execute("UPDATE book SET ai_content_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                 (json.dumps(result, ensure_ascii=False), book.id))
    applied: list[str] = []
    if params.get("apply", True):
        try:
            cfg = get_book_youtube_config(conn, book.id)
            if result.get("genre_tags"):
                cfg["genre_tags"] = str(result["genre_tags"])[:500]
                applied.append("genre_tags")
            if result.get("video_description"):
                cfg["description"] = str(result["video_description"])[:5000]
                applied.append("description")
            if result.get("video_title_template"):
                cfg["title_template"] = str(result["video_title_template"])[:500]
                applied.append("title_template")
            extra = dict(cfg.get("description_extra") or {})
            if result.get("playlist_title") and not extra.get("story_title"):
                extra["story_title"] = str(result["playlist_title"])[:200]
            if book.title and not extra.get("story_title"):
                extra["story_title"] = book.title[:200]
            cfg["description_extra"] = extra
            save_book_youtube_config(conn, book.id, cfg)
            applied.append("description_extra.story_title")
        except Exception as exc:
            logger.warning("ai_content: apply youtube config failed for book %s: %s", book.id, exc)
    conn.commit()
    return {"saved_ai_content": True, "applied_to_youtube_config": applied,
            "playlist_title": result.get("playlist_title", ""),
            "playlist_description": result.get("playlist_description", "")}


def _build_thumbnail_prompt(ctx: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    brief = context_to_brief(ctx)
    style = (params.get("style") or "cinematic, epic, highly detailed").strip()
    system = "Bạn là art director chuyên vẽ bìa sách nói YouTube. Trả về JSON duy nhất, không markdown."
    user = (
        "Dựa trên sách sau, viết 1 image prompt tiếng Anh (1-3 câu) để vẽ ẢNH BÌA "
        "ngang 16:9 cho cả bộ sách — KHÔNG vẽ chữ, KHÔNG watermark:\n"
        f"{brief}\n\n"
        f"Phong cách: {style}.\n"
        "JSON key duy nhất: {\"prompt\": \"...\"}."
    )
    return {"system": system, "user": user, "max_tokens": 300, "response_format": "json"}


register_task("youtube_content", "Nội dung YouTube (cấp sách)", _build_youtube_content, _save_youtube_content)
register_task("thumbnail_prompt", "Prompt ảnh bìa", _build_thumbnail_prompt)


# ---------------------------------------------------------------------------
# High-level runners
# ---------------------------------------------------------------------------

def _parse_json_object(text: str) -> dict:
    text = (text or "").strip()
    if not text:
        raise ValueError("AI trả về rỗng")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"AI không trả về JSON: {text[:200]}")
        parsed = json.loads(match.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("AI không trả về JSON object")
    return parsed


def run_task(conn, book, task_name: str, *, params: dict | None = None,
             provider_id: str | None = None, model: str | None = None) -> dict:
    """Run a registered text task and persist via its saver. Returns the result."""
    from app import ai_providers as _providers

    task = get_task(task_name)
    params = dict(params or {})
    ctx = build_book_context(conn, book)
    spec = task.builder(ctx, params)
    raw = _providers.generate_text(
        system=spec["system"], user=spec["user"], provider_id=provider_id, model=model,
        max_tokens=int(spec.get("max_tokens") or 1500),
        response_format=str(spec.get("response_format") or "text"),
    )
    result = _parse_json_object(raw) if str(spec.get("response_format")) == "json" else {"text": raw}
    summary: dict = {"task": task.name, "result": result}
    if task.saver is not None and params.get("save", True):
        try:
            summary["saved"] = task.saver(conn, book, result, params)
        except Exception as exc:
            logger.warning("ai_content: saver failed for task %s book %s: %s", task.name, book.id, exc)
            summary["saved"] = {"error": str(exc)}
    return summary


def generate_book_thumbnail(conn, book, *, prompt: str | None = None,
                            provider_id: str | None = None, model: str | None = None,
                            style: str | None = None, size: str = "1280x720",
                            save: bool = True) -> dict:
    """Generate ONE 16:9 cover for the whole book (per-book thumbnail).

    - Without an explicit prompt, task ``thumbnail_prompt`` drafts one from the
      book context (grounded in the saved EPUB metadata).
    - The image is stored under data/ai_thumbnails/<book_id>/ and (when save)
      recorded on book.ai_thumbnail_path. It is NOT auto-set as the video
      background — the user picks it in the overlay studio.
    """
    from app import ai_providers as _providers
    from app.config import settings

    final_prompt = (prompt or "").strip()
    prompt_source = "override"
    if not final_prompt:
        ctx = build_book_context(conn, book)
        spec = _build_thumbnail_prompt(ctx, {"style": style} if style else {})
        raw = _providers.generate_text(
            system=spec["system"], user=spec["user"], provider_id=provider_id,
            model=model, max_tokens=300, response_format="json",
        )
        try:
            final_prompt = str(_parse_json_object(raw).get("prompt") or "").strip()
        except ValueError:
            final_prompt = raw.strip()
        prompt_source = "ai"
    if not final_prompt:
        raise ValueError("Không tạo được prompt ảnh bìa")
    # Safety suffix: no text/watermark (image models garble Vietnamese words).
    if "no text" not in final_prompt.lower():
        final_prompt += ", no text, no watermark, 16:9 wide composition"

    image_bytes = _providers.generate_image_bytes(
        prompt=final_prompt, provider_id=provider_id, model=model, size=size)
    if not image_bytes or len(image_bytes) < 1024:
        raise RuntimeError("AI trả về ảnh rỗng")

    out_dir = Path(settings.data_root) / "ai_thumbnails" / str(book.id)
    out_dir.mkdir(parents=True, exist_ok=True)
    # Keep a short history per book; newest is the active one.
    existing = sorted(out_dir.glob("cover_*.png"))
    next_index = len(existing) + 1
    dest = out_dir / f"cover_{next_index:03d}.png"
    dest.write_bytes(image_bytes)
    if save:
        conn.execute("UPDATE book SET ai_thumbnail_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                     (str(dest), book.id))
        conn.commit()
    return {"path": str(dest), "bytes": len(image_bytes),
            "prompt": final_prompt, "prompt_source": prompt_source, "saved": save}
