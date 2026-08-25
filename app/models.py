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
    voice_clip_path: str | None
    voice_transcript: str | None
    created_at: str
    updated_at: str
    video_resolution: str = "1920x1080"
    video_fps: int = 30
    default_image_animation: str = "none"
    normalize_numbers_enabled: int = 1
    normalize_junk_enabled: int = 1
    normalize_spellcheck_enabled: int = 1
    normalize_dictionary_enabled: int = 0
    normalize_transliteration_enabled: int = 0
    normalize_abbreviations_enabled: int = 1
    normalize_breaks_enabled: int = 1
    music_id: int | None = None
    music_volume: float = 0.15
    overlay_config: str | None = None
    text_position: str = "top"
    text_align: str = "center"
    text_font_size: int = 52
    text_color: str = "#FFFFFF"
    text_shadow: int = 1
    text_box_enabled: int = 0
    text_box_color: str = "#000000"
    text_box_opacity: float = 0.6
    text_box_padding: int = 12
    text_box_radius: int = 12
    automation_config: str | None = None
    auto_create_video: int = 1
    auto_upload_youtube: int = 1
    tts_model: str | None = None
    tts_max_chars: int | None = None
    tts_with_effects: int = 0
    tts_voice_id: str | None = None
    export_tts_model: str | None = None
    export_tts_max_chars: int | None = None
    export_tts_with_effects: int | None = None
    export_tts_voice_id: str | None = None

@dataclass
class Chapter:
    id: int
    book_id: int
    chapter_index: int
    title: str
    text: str
    char_count: int
    is_excluded: bool = False
    chapter_no: int | None = None  # số chương đọc được từ tiêu đề ("Chương 12" -> 12)
    text_hash: str | None = None  # dùng để so khớp khi re-import EPUB


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
    image_path: str | None = None
    image_type: str = "static"
    # Khoảng số chương đọc từ tiêu đề (vd 1–10). Ổn định qua các lần re-import, khác
    # với chapter_start/chapter_end vốn là chỉ số vị trí và sẽ xê dịch khi thêm chương.
    chapter_no_start: int | None = None
    chapter_no_end: int | None = None
    name: str = ""
    chunk_count: int = 0
    chunk_count_exact: int = 0  # 1 once chunk_count is the real split count, not the estimate
    next_chunk_index: int = 0
    max_chars: int | None = None
    clean_text: str | None = None
    clean_text_hash: str | None = None
    text_fingerprint: str | None = None
    youtube_override: str | None = None


@dataclass
class Music:
    id: int
    name: str
    file_path: str
    duration_sec: float | None
    created_at: str
    description: str = ""
    license: str = ""


@dataclass
class PatchExport:
    id: int
    patch_id: int
    drive_folder_id: str
    drive_folder_link: str
    status: str  # exported | partially_imported | imported | failed
    exported_chunk_count: int
    imported_chunk_count: int
    error_message: str | None
    created_at: str
    updated_at: str
    drive_account_id: int | None = None  # NULL = export from before multi-account
    account_email: str | None = None  # joined in from google_drive_credentials for display
    sync_target_id: int | None = None
    local_folder_path: str | None = None
    sync_target_name: str | None = None
    sync_target_email: str | None = None

@dataclass
class BookJob:
    id: int
    book_id: int
    job_type: str  # 'video' for now
    status: str  # pending | processing | done | failed
    attempt_count: int
    error_message: str | None
    output_path: str | None
    created_at: str
    updated_at: str


@dataclass
class TextReplaceRule:
    id: int
    book_id: int
    find: str
    replace: str
    is_regex: bool
    position: int
