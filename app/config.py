from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings

_APP_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    data_root: str = str(_APP_ROOT / "data")
    db_path: str = str(_APP_ROOT / "data" / "app.db")
    default_patch_size: int = 10
    tts_max_chars: int = 400
    default_background_image: str = str(_APP_ROOT / "assets" / "default_background.jpg")
    use_nvenc: bool = False
    worker_poll_interval: float = 2.0


settings = Settings()
