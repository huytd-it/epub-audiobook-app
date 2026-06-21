from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app import repository
from app.deps import locked_conn

router = APIRouter()


@router.get("/books/{book_id}/download/audio")
def download_audio(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
    if book is None or not book.final_audio_path:
        raise HTTPException(404, "Final audio not ready yet")
    return FileResponse(book.final_audio_path, filename=f"{book.title}.wav")


@router.get("/books/{book_id}/download/video")
def download_video(request: Request, book_id: int):
    with locked_conn(request) as conn:
        book = repository.get_book(conn, book_id)
    if book is None or not book.final_video_path:
        raise HTTPException(404, "Final video not ready yet")
    return FileResponse(book.final_video_path, filename=f"{book.title}.mp4")
