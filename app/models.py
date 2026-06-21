"""Typed row representations for the SQLite tables."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Book:
    id: int
    title: str
    original_filename: str
    epub_path: str
    patch_size: int
    status: str  # parsing | ready | processing | done | failed
    final_audio_path: str | None
    final_video_path: str | None
    background_image_path: str | None
    created_at: str
    updated_at: str


@dataclass
class Chapter:
    id: int
    book_id: int
    chapter_index: int
    title: str
    text: str
    char_count: int


@dataclass
class Patch:
    id: int
    book_id: int
    patch_index: int
    chapter_start: int
    chapter_end: int
    status: str  # pending | processing | done | failed
    audio_path: str | None
    error_message: str | None
    attempt_count: int
    created_at: str
    updated_at: str
