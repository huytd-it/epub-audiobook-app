"""AI task builders: book context + prompt construction + result persistence.

Extensibility: every generatable artifact is a *task* registered via
:func:`register_task`. A task is ``name -> builder(context) -> {system, user,
max_tokens, response_format}`` plus an optional ``saver(conn, book, result)``.
New features (SEO keywords, chapter summaries, playlist descriptions, ...)
only add a task here — the ``/ai/generate`` route dispatches generically.

Provider I/O for YouTube metadata lives in the same module: ``ai_content_provider``
+ key in .env select the backend (Gemini ``:generateContent`` vs OpenAI-compatible
``/chat/completions``); pure I/O + parse here, persistence in
``app/jobqueue/handlers/youtube_metadata.py``.
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import requests

from app import egress
from app.config import settings

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


def _build_short_script(ctx: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Kịch bản short 60-90s hé lộ tình tiết + caption chung 3 kênh."""
    brief = context_to_brief(ctx)
    system = (
        "Bạn là biên tập viên short video sách nói (TikTok/Reels/Shorts). "
        "Luôn viết bằng Tiếng Việt. Trả về JSON object duy nhất, không markdown."
    )
    user = (
        "Dựa trên sách sau, viết kịch bản video dọc 60-90s (~150-220 từ đọc) "
        "HÉ LỘ tình tiết hấp dẫn nhưng không spoil kết cục, giọng kể lôi cuốn:\n"
        f"{brief}\n\n"
        "Yêu cầu JSON (đúng các key này):\n"
        "- script: lời thoại đọc TTS, 150-220 từ, mở bằng hook 3s gây tò mò.\n"
        "- caption: 1-3 câu đăng chung cho Facebook/TikTok/YouTube Shorts (≤300 ký tự).\n"
        "- hashtags: chuỗi hashtags phân tách dấu phẩy, 5-8 cái.\n"
        "- story_hook: 1 câu hook ngắn cho thumbnail/tiêu đề."
    )
    return {"system": system, "user": user, "max_tokens": 1200, "response_format": "json"}


def parse_short_script(text: str) -> dict[str, Any]:
    """JSON của model -> {script, caption, hashtags, story_hook} đã làm sạch."""
    payload = _extract_json(text)
    if payload is None:
        raise GenerationError(f"Model không trả về JSON hợp lệ: {(text or '')[:200]}")
    script = str(payload.get("script") or "").strip()
    caption = str(payload.get("caption") or "").strip()[:500]
    hashtags = _normalize_tags(payload.get("hashtags", payload.get("tags")))
    hook = str(payload.get("story_hook") or payload.get("hook") or "").strip()[:200]
    if not script:
        raise GenerationError("Model không sinh ra kịch bản (script).")
    words = len(script.split())
    if words < 40 or words > 400:
        raise GenerationError(f"Kịch bản dài bất thường ({words} từ, kỳ vọng 150-220).")
    return {"script": script, "caption": caption,
            "hashtags": ", ".join(hashtags), "story_hook": hook}


register_task("short_script", "Kịch bản short 60-90s", _build_short_script)


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


PROVIDERS: dict[str, dict[str, str]] = {
    "gemini": {
        "label": "Gemini",
        "key_env": "GEMINI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "model": "gemini-2.5-flash",
    },
    "openai": {
        "label": "OpenAI",
        "key_env": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-mini",
    },
    "custom": {
        "label": "OpenAI-compatible",
        "key_env": "AI_CONTENT_API_KEY",
        "base_url": "",
        "model": "gpt-4o-mini",
    },
}

# YouTube cho 5000 ký tự mô tả; phần sinh ra chỉ chiếm đầu, còn khối app tự
# nối (link playlist, timeline chương, thông báo bản quyền) đi sau.
DESCRIPTION_LIMIT = 3000
MAX_TAGS = 12
# Model cần bối cảnh đủ để đoán thể loại, nhưng sách 1000 chương thì prompt
# không tài nào đọc nổi — chỉ đưa tên chương của đoạn đầu và cuối.
MAX_PROMPT_CHAPTERS = 30


class ProviderNotConfigured(RuntimeError):
    """Chưa cấu hình provider/key trong .env — thử lại cũng vô ích."""


class GenerationError(RuntimeError):
    """Đã gọi provider nhưng không lấy được nội dung dùng được."""


@dataclass(frozen=True)
class ProviderConfig:
    provider: str
    model: str
    api_key: str
    base_url: str

    @property
    def label(self) -> str:
        return f"{PROVIDERS[self.provider]['label']} · {self.model}"


def selected_provider() -> str:
    """Provider sẽ dùng, suy ra từ .env.

    Ưu tiên AI_CONTENT_PROVIDER khi có ghi. Nếu không mà người dùng đã trỏ
    AI_CONTENT_BASE_URL vào một endpoint riêng thì đó chắc chắn là server
    OpenAI-compatible (gemini không nhận base_url tùy ý), nên coi như 'custom'
    thay vì mặc định 'gemini' rồi gọi nhầm dạng :generateContent. Còn lại thì
    mặc định gemini vì key của nó nằm sẵn trong biến môi trường chuẩn.
    """
    explicit = (settings.ai_content_provider or "").strip().lower()
    if explicit:
        return explicit
    return "custom" if (settings.ai_content_base_url or "").strip() else "gemini"


def resolve_provider() -> ProviderConfig:
    provider = selected_provider()
    spec = PROVIDERS.get(provider)
    if spec is None:
        supported = ", ".join(sorted(PROVIDERS))
        raise ProviderNotConfigured(f"AI_CONTENT_PROVIDER không hợp lệ: {provider} (chọn: {supported})")
    api_key = (settings.ai_content_api_key or os.getenv(spec["key_env"], "")).strip()
    if not api_key:
        raise ProviderNotConfigured(f"Chưa có API key cho provider {provider} — đặt {spec['key_env']} trong .env")
    base_url = (settings.ai_content_base_url or spec["base_url"]).strip().rstrip("/")
    if not base_url:
        raise ProviderNotConfigured(f"Provider {provider} cần AI_CONTENT_BASE_URL (endpoint OpenAI-compatible)")
    model = (settings.ai_content_model or spec["model"]).strip()
    return ProviderConfig(provider=provider, model=model, api_key=api_key, base_url=base_url)


def provider_status() -> dict[str, Any]:
    """Trạng thái provider cho UI — không ném lỗi, nút bấm mới gọi resolve_provider."""
    provider = selected_provider()
    try:
        config = resolve_provider()
    except ProviderNotConfigured as exc:
        return {"configured": False, "provider": provider, "model": "", "label": "", "detail": str(exc)}
    return {
        "configured": True,
        "provider": provider,
        "model": config.model,
        "label": config.label,
        "detail": "",
    }


# ------------------------------------------------------------------ gọi API

def _raise_for_status(response: requests.Response) -> None:
    if response.status_code < 400:
        return
    body = (response.text or "").strip()
    raise GenerationError(f"provider trả HTTP {response.status_code}: {body[:400]}")


def _gemini_text(prompt: str, config: ProviderConfig, timeout: float) -> str:
    response = egress.request(
        "ai", "POST", f"{config.base_url}/models/{config.model}:generateContent",
        headers={"x-goog-api-key": config.api_key, "Content-Type": "application/json"},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"},
        },
        timeout=timeout,
    )
    _raise_for_status(response)
    payload = response.json()
    candidates = payload.get("candidates") or []
    if not candidates:
        raise GenerationError(f"Gemini không trả về nội dung: {str(payload)[:300]}")
    parts = ((candidates[0].get("content") or {}).get("parts")) or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        raise GenerationError("Gemini trả về nội dung rỗng.")
    return text


def _chat_completion_text(response: requests.Response, config: ProviderConfig) -> str:
    """Nội dung từ /chat/completions, chấp nhận cả dạng JSON lẫn SSE.

    Gateway nội bộ (OpenCode server) trả chuỗi `data: {...}` dù payload đã yêu cầu
    stream=false — nếu chỉ gọi response.json() thì body không parse được, job sẽ
    fail trong khi provider thực ra đã sinh nội dung đúng.
    """
    try:
        body = response.json()
    except ValueError:
        chunks: list[str] = []
        for line in (response.text or "").splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data = line[len("data:"):].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue
            for choice in event.get("choices") or []:
                delta = (choice.get("delta") or {}).get("content") or (choice.get("message") or {}).get("content")
                if delta:
                    chunks.append(delta)
        text = "".join(chunks)
        if not text.strip():
            raise GenerationError(f"{config.label} trả về body không đọc được.")
        return text.strip()

    choices = body.get("choices") or []
    if not choices:
        raise GenerationError(f"{config.label} không trả về nội dung.")
    text = ((choices[0].get("message") or {}).get("content")) or ""
    if not text.strip():
        raise GenerationError(f"{config.label} trả về nội dung rỗng.")
    return text.strip()


def _openai_text(prompt: str, config: ProviderConfig, timeout: float, *, json_mode: bool = True) -> str:
    payload = {
        "model": config.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.7,
        # Job cần trả về một lần để parse; để tuỳ server có thể ra SSE nhiều
        # dòng (xem _chat_completion_text).
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    response = egress.request(
        "ai", "POST", f"{config.base_url}/chat/completions",
        headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=timeout,
    )
    if response.status_code == 400 and json_mode:
        # Server OpenAI-compatible cũ thường chỉ biết /chat/completions mà không
        # hiểu response_format. Prompt vẫn yêu cầu JSON nên bỏ cờ này vẫn parse được.
        logger.info("%s không hỗ trợ response_format, thử lại không có cờ đó", config.label)
        return _openai_text(prompt, config, timeout, json_mode=False)
    _raise_for_status(response)
    return _chat_completion_text(response, config)


def complete(prompt: str, *, config: ProviderConfig | None = None) -> str:
    """Một lượt gọi text, trả về nội dung thô của model."""
    config = config or resolve_provider()
    timeout = max(5.0, float(settings.ai_content_timeout_seconds))
    if config.provider == "gemini":
        return _gemini_text(prompt, config, timeout)
    return _openai_text(prompt, config, timeout)


# ------------------------------------------------------------------- prompt

_SYSTEM_HINT = "Bạn là biên tập viên kênh sách nói tiếng Việt trên YouTube."


def build_prompt(book_title: str, chapter_titles: list[str], config: dict | None = None) -> str:
    """Prompt sinh metadata cho MỘT sách, không phụ thuộc tên patch/tập.

    Mô tả của mỗi video được ghép lại: phần do app tự dựng (tiêu đề, danh sách
    chương, link playlist, timeline) nằm ngoài `description`, còn `description`
    chính là phần "về cuốn sách" mà model viết ra. Vì vậy prompt không nhắc tập
    số, chương hiện tại hay template.
    """
    config = config or {}
    titles = [title.strip() for title in chapter_titles if title and title.strip()]
    if len(titles) > MAX_PROMPT_CHAPTERS:
        head = titles[: MAX_PROMPT_CHAPTERS // 2]
        tail = titles[-MAX_PROMPT_CHAPTERS // 2 :]
        titles = [*head, "…", *tail]
    existing = str(config.get("genre_tags") or "").strip()
    extra = config.get("description_extra") if isinstance(config.get("description_extra"), dict) else {}
    known = "\n".join(
        f"- {label}: {value}"
        for label, value in (
            ("tên truyện trong khối bản quyền", extra.get("story_title")),
            ("nguồn truyện", extra.get("story_source_name")),
        )
        if value
    )
    # Các khối văn bản dài dựng sẵn: biểu thức f-string không được chứa dấu \n
    # trên Python < 3.12.
    chapter_block = "Tên chương (đầu và cuối):\n" + "\n".join(f"- {title}" for title in titles) if titles else ""
    known_block = f"Thông tin đã biết (đừng bịa thêm, nếu có thì ưu tiên số liệu này):\n{known}" if known else ""
    genre_block = f"Thể loại đang dùng cho sách (có thể giữ lại hoặc bổ sung, không cần trùng hết): {existing}" if existing else ""
    return f"""{_SYSTEM_HINT}

Viết metadata YouTube cho cuốn sách nói dưới đây, bằng tiếng Việt.

Tên sách: {book_title}
Số chương: {len(chapter_titles)}
{chapter_block}
{known_block}
{genre_block}

Trả về DUY NHẤT một JSON object, không markdown, không giải thích ngoài JSON:
{{
  "description": "3-6 câu giới thiệu cuốn sách: nội dung chính, giọng đọc sách nói, vì sao đáng nghe. Không nhắc tập số/chương hiện tại, không chèn link, không chèn hashtag, không viết tiêu đề video. Dưới {DESCRIPTION_LIMIT} ký tự.",
  "tags": ["tên sách", "sách nói", "audiobook", "thể loại", ...]
}}

tags: tối đa {MAX_TAGS} thẻ, viết thường (viết thường và số), mỗi thẻ một cụm từ ngắn, không lặp lại, không dùng ký tự đặc biệt. Ưu tiên thể loại và từ khóa tìm kiếm mà người nghe tiếng Việt hay dùng."""


# -------------------------------------------------------------------- parse

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)
_OBJECT_RE = re.compile(r"\{.*\}", re.S)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Model hay bọc JSON trong ```json ... ```; lấy object đầu tiên parse được."""
    candidates: list[str] = []
    fenced = _FENCE_RE.findall(text or "")
    candidates.extend(block.strip() for block in fenced)
    stripped = (text or "").strip()
    if stripped:
        candidates.append(stripped)
        match = _OBJECT_RE.search(stripped)
        if match:
            candidates.append(match.group(0))
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _normalize_tags(value: Any) -> list[str]:
    if isinstance(value, str):
        parts: list[Any] = re.split(r"[,\n]", value)
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        parts = []
    tags: list[str] = []
    for part in parts:
        tag = re.sub(r"^#+\s*", "", str(part or "").strip())
        # Bỏ ký tự YouTube không nhận trong tag và chuẩn hoá khoảng trắng.
        tag = re.sub(r"[#\"']+", " ", tag)
        tag = re.sub(r"\s+", " ", tag).strip(" ,;:|/-")
        if tag and tag.lower() not in {existing.lower() for existing in tags}:
            tags.append(tag)
    return tags[:MAX_TAGS]


def parse_metadata(text: str) -> dict[str, Any]:
    """JSON của model -> {"description", "genre_tags"} sạch, dùng được để lưu."""
    payload = _extract_json(text)
    if payload is None:
        raise GenerationError(f"Model không trả về JSON hợp lệ: {(text or '')[:200]}")
    description = str(payload.get("description") or payload.get("mo_ta") or "").strip()
    tags = _normalize_tags(payload.get("tags", payload.get("genre_tags")))
    if not description and not tags:
        raise GenerationError("Model không sinh ra mô tả lẫn thẻ nào.")
    if not description:
        raise GenerationError("Model không sinh ra mô tả (description).")
    if not tags:
        raise GenerationError("Model không sinh ra thẻ nào (tags).")
    return {
        "description": description[:DESCRIPTION_LIMIT],
        "genre_tags": ", ".join(tags),
        "tags": tags,
    }


# ------------------------------------------------------------------- tiện ích

def generate_book_metadata(conn, book_id: int, *, config_provider=None) -> dict[str, Any]:
    """Sinh metadata cho một sách. Trả về description + genre_tags (đã nối) và
    nhãn provider để log. Không ghi DB — handler lo việc đó."""
    from app import repository
    from app.production_defaults import get_effective_youtube_config

    book = repository.get_book(conn, book_id)
    if book is None:
        raise GenerationError(f"book {book_id} không tồn tại")
    chapters = repository.list_chapters(conn, book_id)
    config = get_effective_youtube_config(conn, book)
    provider = config_provider or resolve_provider()
    prompt = build_prompt(book.title, [chapter.title for chapter in chapters], config)
    raw = complete(prompt, config=provider)
    result = parse_metadata(raw)
    return {
        "description": result["description"],
        "genre_tags": result["genre_tags"],
        "tags": result["tags"],
        "provider": provider.provider,
        "model": provider.model,
        "label": provider.label,
    }
