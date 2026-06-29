from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

_APP_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    data_root: str = str(_APP_ROOT / "data")
    db_path: str = str(_APP_ROOT / "data" / "app.db")
    log_path: str = str(_APP_ROOT / "data" / "app.log")
    default_patch_size: int = 10
    tts_max_chars: int = 400
    default_background_image: str = str(_APP_ROOT / "assets" / "default_background.jpg")
    use_nvenc: bool = False
    worker_poll_interval: float = 2.0
    worker_shutdown_timeout_seconds: float = 300.0
    enable_worker: bool = True  # set ENABLE_WORKER=false in dev to suppress the background loop
    tts_write_chunk_files: bool = True  # set TTS_WRITE_CHUNK_FILES=false to skip per-chunk WAV files
    ffmpeg_path: str = str(_APP_ROOT / "assets" / "bin" / "ffmpeg.exe")
    reset_all_jobs_on_startup: bool = False  # dev-only: reset every patch + book_job → pending on boot

    # YouTube upload
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_default_tags: str = "audiobook,epub,video"
    youtube_default_privacy: str = "private"  # private | unlisted | public
    youtube_auto_upload: bool = True  # auto-upload after video generation


settings = Settings()
