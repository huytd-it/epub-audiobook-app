from __future__ import annotations

import shutil
from functools import lru_cache
from pathlib import Path

from app.config import settings

_PROJECT_BIN = Path(__file__).resolve().parent.parent / "assets" / "bin"


@lru_cache(maxsize=1)
def get_ffmpeg_path() -> str:
    local = _PROJECT_BIN / "ffmpeg.exe"
    if local.exists():
        return str(local)
    exe = shutil.which(settings.ffmpeg_path)
    if exe:
        return exe
    fallbacks = [
        r"C:\ProgramData\chocolatey\bin\ffmpeg.exe",
        str(Path.home() / "AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-8.1.2-full_build/bin/ffmpeg.exe"),
    ]
    for p in fallbacks:
        if Path(p).exists():
            return p
    return settings.ffmpeg_path


@lru_cache(maxsize=1)
def get_ffprobe_path() -> str:
    local = _PROJECT_BIN / "ffprobe.exe"
    if local.exists():
        return str(local)
    exe = shutil.which("ffprobe")
    if exe:
        return exe
    ffmpeg_path = get_ffmpeg_path()
    if ffmpeg_path != "ffmpeg":
        candidate = str(Path(ffmpeg_path).parent / "ffprobe.exe")
        if Path(candidate).exists():
            return candidate
    return "ffprobe"
