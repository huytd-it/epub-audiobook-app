from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_APP_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    # extra="ignore": .env is shared with other tooling, so an unknown key there must not
    # take the whole app (and the test suite) down at import time.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    data_root: str = str(_APP_ROOT / "data")
    db_path: str = str(_APP_ROOT / "data" / "app.db")
    log_path: str = str(_APP_ROOT / "data" / "app.log")
    default_patch_size: int = 10
    default_page_size: int = 20
    tts_max_chars: int = 400
    tts_engine: str = "voxcpm2"
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

    # Hugging Face token for Colab/Kaggle notebooks (baked into the exported package so the
    # notebook can download VoxCPM2 without hitting unauthenticated rate limits). Leave empty
    # to rely on secrets/prompt in the notebook instead. Get a token at:
    # https://huggingface.co/settings/tokens
    hf_token: str = ""

    # Google Drive export (Colab/Kaggle round trip). Can reuse the same OAuth client as
    # YouTube - just enable the Drive API on that Google Cloud project.
    google_drive_client_id: str = ""
    google_drive_client_secret: str = ""

    # rclone binary used to push staging folders (codex5-10 targets) up to Google Drive
    # from the /drive page. Defaults to the name on PATH; set an absolute path to override.
    rclone_path: str = "rclone"

    # Font path for text overlay on patch background images (Pillow).
    # Set to the absolute path of a .ttf/.otf font file supporting Vietnamese.
    # If empty or file not found, Pillow falls back to its built-in bitmap font.
    default_font_path: str = ""

    # Maximum music file size in MB for uploads to the music library.
    music_max_size_mb: int = 20

    # Lightweight TTS preview
    light_tts_backend: str = "edge-tts"
    light_tts_voice: str = "vi-VN-HoaiMyNeural"
    # Directory holding piper *.onnx models. A piper voice id is resolved to
    # "<piper_voices_dir>/<id>.onnx" at synth time. Empty => use the id as-is.
    piper_voices_dir: str = ""
    # How many times to attempt each LightTTS chunk before reporting it failed.
    light_tts_chunk_retries: int = 3
    # Queue job chạy nền
    # Loại nào không liệt kê ở đây nhận queue_default_concurrency.
    queue_concurrency: str = "audiobook_tts=1,video=2,youtube_upload=1,patch_video=1,gameplay_clip=1"
    queue_default_concurrency: int = 10
    queue_log_retention_days: int = 7
    # Job 'running' im lặng quá lâu bị coi là chết và trả về 'pending'.
    queue_reap_after_seconds: int = 120
    # Ôm GPU/CPU khi render patch video — một job tại một thời điểm tránh cạnh tranh
    # với audiobook_tts và giữ luồng upload ổn định.
    patch_video_concurrency: int = 1
    gameplay_clip_concurrency: int = 1
    gameplay_pool_target_seconds: int = 3600
    gameplay_pool_maintainer_enabled: bool = False
    gameplay_renderer_version: str = "1"

    @staticmethod
    @lru_cache(maxsize=1)
    def get_ffmpeg_path() -> str:
        import shutil
        from pathlib import Path
        _PROJECT_BIN = Path(__file__).resolve().parent.parent / "assets" / "bin"
        local = _PROJECT_BIN / "ffmpeg.exe"
        if local.exists():
            return str(local)
        exe = shutil.which(Settings().ffmpeg_path)
        if exe:
            return exe
        fallbacks = [
            r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
            str(Path.home() / "AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin/ffmpeg.exe"),
        ]
        for p in fallbacks:
            if Path(p).exists():
                return p
        return Settings().ffmpeg_path

    @staticmethod
    @lru_cache(maxsize=1)
    def get_ffprobe_path() -> str:
        import shutil
        from pathlib import Path
        _PROJECT_BIN = Path(__file__).resolve().parent.parent / "assets" / "bin"
        local = _PROJECT_BIN / "ffprobe.exe"
        if local.exists():
            return str(local)
        exe = shutil.which("ffprobe")
        if exe:
            return exe
        ffmpeg_path = Settings.get_ffmpeg_path()
        if ffmpeg_path != "ffmpeg":
            candidate = str(Path(ffmpeg_path).parent / "ffprobe.exe")
            if Path(candidate).exists():
                return candidate
        return "ffprobe"


settings = Settings()
