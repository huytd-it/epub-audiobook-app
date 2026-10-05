"""Render lớp đồ hoạ dọc bằng Remotion, nhận audio + cover từ ffmpeg.

Phương án lai: TTS và cover vẫn do pipeline Python/ffmpeg lo (đã ổn định),
Remotion chỉ dựng khung hình dọc — hook, sub highlight theo từ, thanh tiến.
Python chỉ gọi `npx remotion render` với props JSON rồi validate bằng
video_integrity.validate_video, nên lỗi renderer không lọt xuống upload.

Yêu cầu: Node >= 18 và `npm install` trong thư mục remotion/ (xem README).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path

from app.config import Settings, settings
from app.short_cues import build_vertical_cues
from app.video_integrity import VideoExpectation, validate_video

logger = logging.getLogger(__name__)

COMPOSITION_ID = "ShortIntro"
DEFAULT_RENDER_TIMEOUT = 900


class RemotionUnavailable(RuntimeError):
    """Thiếu Node hoặc chưa cài dependency — lỗi cấu hình, đừng retry vô ích."""


def _npx() -> str:
    """Đường dẫn npx đã phân giải.

    Trên Windows lệnh là `npx.cmd`; subprocess không tự nối `.cmd` khi không
    bật shell, nên phải trả về đường dẫn thật thay vì chuỗi "npx".
    """
    for name in ("npx", "npx.cmd", "npx.exe", "npx.bat"):
        found = shutil.which(name)
        if found:
            return found
    raise RemotionUnavailable("Không tìm thấy Node/npx trên PATH")


def project_dir() -> Path:
    """Thư mục project Remotion cạnh app/ trong repo."""
    return Path(__file__).resolve().parent.parent / "remotion"


def probe_audio_duration(audio_path: str) -> float:
    """ffprobe audio để chia cue. Raise nếu probe hỏng."""
    cmd = [Settings.get_ffprobe_path(), "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", audio_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe audio thất bại: {result.stderr.strip()[:400]}")
    try:
        info = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe trả JSON không đọc được: {exc}") from exc
    duration = (info.get("format") or {}).get("duration")
    try:
        value = float(duration)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("ffprobe không trả về thời lượng audio") from exc
    if value <= 0:
        raise RuntimeError("Thời lượng audio không hợp lệ")
    return value


def build_props(*, audio_path: str, cover_path: str, script_text: str,
                duration_seconds: float, resolution: str, fps: int,
                music_path: str | None = None, book_title: str = "",
                hook_text: str = "") -> dict:
    """Props JSON cho composition ShortIntro.

    `audio_path`/`cover_path`/`music_path` là tên file sau khi đã stage vào
    public dir — Chrome của Remotion chặn `file://`, nên component đọc qua
    `staticFile()` chứ không nhận đường dẫn tuyệt đối.
    """
    width, height = (int(v) for v in resolution.split("x"))
    cues = build_vertical_cues(script_text, duration_seconds)
    return {
        "audioSrc": audio_path,
        "coverSrc": cover_path,
        "musicSrc": music_path or "",
        "width": width,
        "height": height,
        "fps": fps,
        "durationInFrames": max(1, int(round(duration_seconds * fps))),
        "bookTitle": book_title,
        "hookText": hook_text,
        "cues": cues,
    }


def stage_assets(stage_dir: str | Path, *, audio_path: str,
                 cover_path: str | None, music_path: str | None = None) -> dict[str, str]:
    """Copy media vào thư mục phẳng cho Remotion; trả về {stem: tên file}.

    Tên file giữ đuôi gốc (Remotion đoán mimetype theo đuôi) nhưng phần tên thay
    bằng tên cố định, nên props không phụ thuộc đường dẫn sách. Ba stem: audio /
    cover / music.
    """
    stage = Path(stage_dir)
    stage.mkdir(parents=True, exist_ok=True)
    staged: dict[str, str] = {}

    def _copy(stem: str, source: str, fallback_suffix: str) -> None:
        src = Path(source)
        if not src.is_file():
            raise FileNotFoundError(f"Media không tồn tại: {source}")
        name = f"{stem}{src.suffix or fallback_suffix}"
        shutil.copy2(src, stage / name)
        staged[stem] = name

    _copy("narration", audio_path, ".wav")
    if cover_path:
        _copy("cover", cover_path, ".jpg")
    if music_path:
        _copy("music", music_path, ".mp3")
    return staged


def render(props: dict, out_path: str, *,
           project: Path | None = None,
           stage_dir: str | Path | None = None,
           timeout: float = DEFAULT_RENDER_TIMEOUT) -> str:
    """Gọi `npx remotion render` rồi validate. Trả về out_path."""
    root = project or project_dir()
    if not root.is_dir():
        raise RemotionUnavailable(f"Không thấy thư mục Remotion: {root}")
    if not stage_dir:
        raise ValueError("render() cần stage_dir để phục vụ media qua public dir")

    props_file = root / ".short-props.json"
    props_file.write_text(json.dumps(props, ensure_ascii=False), encoding="utf-8")
    # --no-install: không bao giờ để Remotion tự kéo thêm package lúc render.
    cmd = [_npx(), "--no-install", "remotion", "render",
           str(root / "src" / "index.ts"),
           COMPOSITION_ID,
           str(out_path),
           f"--public-dir={stage_dir}",
           f"--props={props_file}"]
    logger.info("Remotion render: %s", " ".join(cmd))
    try:
        completed = subprocess.run(cmd, capture_output=True, text=True,
                                   cwd=str(root), timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"Remotion render timeout sau {timeout:.0f}s") from exc
    finally:
        props_file.unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-2000:]
        raise RuntimeError(f"Remotion render lỗi (exit {completed.returncode}): {detail}")

    expected = VideoExpectation(
        duration_seconds=props["durationInFrames"] / props["fps"],
        width=props["width"], height=props["height"], fps=float(props["fps"]),
        require_audio=True,
    )
    result = validate_video(out_path, expected=expected)
    if not result.valid:
        raise RuntimeError(
            f"Video Remotion không hợp lệ: {result.error_code}: {result.message}")
    return out_path