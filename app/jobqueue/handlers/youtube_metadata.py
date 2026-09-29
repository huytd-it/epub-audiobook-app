"""Job handler: sinh mô tả + thẻ YouTube cho một sách bằng API AI trong .env.

Chạy qua hàng đợi thay vì request đồng bộ vì một lượt gọi LLM có thể chờ hàng
chục giây. Job chỉ ghi hai trường `description` và `genre_tags`; phần còn lại của
cấu hình YouTube lấy từ config đang hiệu lực nên không bị mất gì người dùng
đã chỉnh ở nhóm khác.
"""
from __future__ import annotations

from app import ai_content, repository
from app.jobqueue.models import JobFatalError
from app.production_defaults import get_effective_youtube_config
from app.youtube_metadata import save_book_youtube_config


def handle(ctx) -> dict:
    book_id = ctx.job.payload.get("book_id") or ctx.job.book_id
    if book_id is None:
        raise JobFatalError("payload thiếu book_id")
    book = repository.get_book(ctx.conn, book_id)
    if book is None:
        raise JobFatalError(f"book {book_id} không tồn tại")

    ctx.progress(0, 1, phase="generating")
    ctx.log(f"sinh nội dung & thẻ cho book {book_id} ({book.title})")
    try:
        result = ai_content.generate_book_metadata(ctx.conn, book_id)
    except ai_content.ProviderNotConfigured as exc:
        # Thiếu key/model thì thử lại cũng thế: fatal thay vì đốt hết attempt.
        raise JobFatalError(str(exc)) from exc

    if ctx.should_cancel():
        return {"cancelled": True}

    config = get_effective_youtube_config(ctx.conn, book)
    save_book_youtube_config(
        ctx.conn, book_id,
        {**config, "description": result["description"], "genre_tags": result["genre_tags"]},
    )
    ctx.progress(1, 1, phase="done")
    ctx.log(
        f"đã ghi {len(result['tags'])} thẻ và {len(result['description'])} ký tự mô tả bằng {result['label']}",
    )
    return {
        "description": result["description"],
        "genre_tags": result["genre_tags"],
        "provider": result["provider"],
        "model": result["model"],
    }
