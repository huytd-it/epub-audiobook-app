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
    reset_all_jobs_on_startup: bool = False  # dev-only: reset every patch + book_job → pending on boot


settings = Settings()
