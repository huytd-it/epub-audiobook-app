from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from app import repository, voice_taxonomy
from app.config import settings
from app.deps import locked_conn
from app.jobqueue import store
from app.routes.queue import _job_dict, _pools
from app import google_drive


router = APIRouter(prefix="/api/ui", tags=["ui"])


def _book_summary(conn, book) -> dict:
    patches = repository.list_patches(conn, book.id)
    return {
        **asdict(book),
        "patches": {
            "total": len(patches),
            "done": sum(p.status == "done" for p in patches),
            "active": sum(p.status in {"pending", "processing"} for p in patches),
            "failed": sum(p.status == "failed" for p in patches),
        },
    }


@router.get("/bootstrap")
def bootstrap(request: Request):
    with locked_conn(request) as conn:
        stats = repository.get_queue_stats(conn)
        stats["jobs"] = store.counts(conn)
        books, total, _ = repository.list_books(conn, page=1, per_page=6)
        recent_jobs = [_job_dict(job) for job in store.list_jobs(conn, limit=8)]
        summaries = [_book_summary(conn, book) for book in books]
    return {
        "books": summaries,
        "book_count": total,
        "queue": stats,
        "jobs": recent_jobs,
        "pools": _pools(getattr(request.app.state, "job_queue", None)),
    }


@router.get("/books")
def books(request: Request, page: int = 1, per_page: int = 24):
    page = max(1, page)
    per_page = min(100, max(1, per_page))
    with locked_conn(request) as conn:
        rows, total, total_pages = repository.list_books(conn, page=page, per_page=per_page)
        items = [_book_summary(conn, book) for book in rows]
    return {"items": items, "page": page, "total": total, "total_pages": total_pages}


@router.get("/books/{book_id}")
def book(request: Request, book_id: int):
    from app.tts_engine import list_tts_models

    with locked_conn(request) as conn:
        row = repository.get_book(conn, book_id)
        if row is None:
            raise HTTPException(404, "Không tìm thấy sách")
        patches = [asdict(item) for item in repository.list_patches(conn, book_id)]
        chapters = [asdict(item) for item in repository.list_chapters(conn, book_id)]
        rules = [asdict(item) for item in repository.list_replace_rules(conn, book_id)]
        last_error = repository.get_last_error_for_book(conn, book_id)
    return {"book": asdict(row), "patches": patches, "chapters": chapters,
            "rules": rules, "last_error": last_error,
            "tts_models": list_tts_models()}


@router.get("/media")
def media(request: Request):
    backgrounds = Path(settings.data_root, "backgrounds")
    voices = Path(settings.data_root, "voices")
    backgrounds.mkdir(parents=True, exist_ok=True)
    voices.mkdir(parents=True, exist_ok=True)
    with locked_conn(request) as conn:
        music = [asdict(item) for item in repository.list_music(conn)]
        voice_items = []
        for path in sorted(voices.iterdir()):
            if path.is_file() and path.suffix.lower() in {".wav", ".mp3", ".m4a", ".ogg"}:
                meta = repository.get_voice_meta(conn, path.name)
                voice_items.append({
                    "name": path.name,
                    "size": path.stat().st_size,
                    "description": meta["description"] if meta else "",
                    "gender": meta["gender"] if meta else "",
                    "genre": voice_taxonomy.split_genres(meta["genre"] if meta else ""),
                })
    photos = [{"name": path.name, "size": path.stat().st_size,
               "is_video": path.suffix.lower() in {".mp4", ".webm", ".mov"}}
              for path in sorted(backgrounds.iterdir()) if path.is_file()]
    return {"music": music, "photos": photos, "voices": voice_items}


_RCLONE_CLIENT_ID_KEY = "rclone.drive_client_id"
_RCLONE_CLIENT_SECRET_KEY = "rclone.drive_client_secret"


@router.get("/drive")
def drive(request: Request):
    with locked_conn(request) as conn:
        targets = repository.list_drive_sync_targets(conn)
        exports = repository.list_all_patch_exports(conn, limit=30)
        accounts = google_drive.list_accounts(conn)
        clients = google_drive.list_clients(conn)
        rclone_client_id = repository.get_app_state(conn, _RCLONE_CLIENT_ID_KEY) or ""
        rclone_client_secret = repository.get_app_state(conn, _RCLONE_CLIENT_SECRET_KEY) or ""
    return {
        "targets": targets,
        "exports": exports,
        "accounts": accounts,
        "clients": clients,
        "rclone_client_id": rclone_client_id,
        "rclone_client_secret": rclone_client_secret,
    }


@router.get("/books/{book_id}/exports")
def book_exports(request: Request, book_id: int):
    with locked_conn(request) as conn:
        if repository.get_book(conn, book_id) is None:
            raise HTTPException(404, "Không tìm thấy sách")
        patches = repository.list_patches(conn, book_id)
        exports = []
        for patch in patches:
            for exp in repository.list_patch_exports(conn, patch.id):
                exports.append(asdict(exp))
        sync_targets = repository.list_drive_sync_targets(conn)
        accounts = google_drive.list_accounts(conn)
    return {"exports": exports, "sync_targets": sync_targets, "accounts": accounts}
