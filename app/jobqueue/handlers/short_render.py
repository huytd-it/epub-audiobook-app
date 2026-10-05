"""Render 1 short: TTS(script) + cover + nhạc, rồi dựng lớp đồ hoạ dọc.

Phương án lai (bước 3): bước audio/timeline luôn do TTS + ffmpeg lo — đây là
phần đã ổn định. Chỉ lớp hình dọc mới phân nhánh theo `shorts.renderer`:
  - ffmpeg   : video_gen.generate_standalone_video (mặc định, không cần Node)
  - remotion : app.short_remotion dựng cover + hook + sub highlight theo từ
Cả hai đều validate bằng video_integrity trước khi short chuyển sang 'ready'.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np

from app import repository, shorts_repository, video_gen
from app.audio_merge import atomic_write_wav
from app.config import settings
from app.jobqueue.models import JobFatalError
from app.video_config import validate_short_config
from app.video_integrity import VideoExpectation, validate_video

logger = logging.getLogger(__name__)


def _shorts_dir(short_id: int) -> Path:
    d = Path(settings.data_root) / "shorts" / str(short_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _synthesize_narration(text: str, *, engine_id: str, voice: str | None,
                          reference_wav_path: str | None, out_path: str) -> float:
    """TTS toàn script 1 lần -> wav. Trả về sample_rate.

    Engine trả np.ndarray (xem TTSEngine.synthesize_chunk), không phải tự ghi
    file — nên phải tự ghép nhiều chunk rồi ghi qua atomic_write_wav để một lần
    ghi hỏng không thay thế file đang đọc được.
    """
    import soundfile as sf
    from app.chunker import split_into_tts_chunks
    from app.tts_engine import create_tts_engine

    engine = create_tts_engine(engine_id, voice=voice)
    chunks = split_into_tts_chunks(text, max_chars=int(settings.tts_max_chars or 400))
    if not chunks:
        raise RuntimeError("Kịch bản rỗng sau khi tách chunk")
    blocks = [
        engine.synthesize_chunk(chunk, reference_wav_path=reference_wav_path)
        for chunk in chunks
    ]
    audio = np.concatenate([np.asarray(block).reshape(-1) for block in blocks])
    if audio.size == 0:
        raise RuntimeError("TTS trả về audio rỗng")
    sample_rate = int(getattr(engine, "sample_rate", 0) or 0)
    if sample_rate <= 0:
        raise RuntimeError("TTS engine không khai báo sample_rate")

    def _write(temp_path: str) -> None:
        sf.write(temp_path, audio, sample_rate, format="WAV", subtype="PCM_16")

    atomic_write_wav(out_path, _write)
    return sample_rate


def _handle(ctx, short_id: int) -> dict:
    short = shorts_repository.get_short(ctx.conn, short_id)
    if short is None:
        raise JobFatalError(f"short {short_id} không tồn tại")
    if not (short.script_text or "").strip():
        raise JobFatalError("short chưa có kịch bản")

    saved_cfg = shorts_repository.get_render_config(short)
    cfg = validate_short_config({**saved_cfg, "resolution": short.resolution})
    ctx.progress(0, 4, phase="tts")
    shorts_repository.update_short(ctx.conn, short.id, status="rendering",
                                   render_config_json=json.dumps(cfg))

    book = repository.get_book(ctx.conn, short.book_id)
    if book is None:
        raise JobFatalError(f"book {short.book_id} không tồn tại")
    voice = short.voice_id or book.tts_voice_id or None

    out_dir = _shorts_dir(short.id)
    audio_path = str(out_dir / "narration.wav")
    engine_id = book.tts_model or settings.tts_engine
    try:
        _synthesize_narration(short.script_text, engine_id=engine_id, voice=voice,
                              reference_wav_path=book.voice_clip_path or None,
                              out_path=audio_path)
    except Exception as exc:
        raise RuntimeError(f"TTS short thất bại: {exc}") from exc
    if not Path(audio_path).is_file():
        raise RuntimeError("TTS không tạo ra file audio")

    ctx.progress(1, 4, phase="cover")
    # Cover/background: ưu tiên cover sách, rồi background sách, rồi default.
    cover = (book.cover_image_path or book.background_image_path
             or settings.default_background_image)
    music_path = None
    if short.music_id:
        mrow = ctx.conn.execute("SELECT file_path FROM music WHERE id=?",
                                (short.music_id,)).fetchone()
        if mrow:
            music_path = mrow["file_path"]

    video_path = str(out_dir / f"short_{short.id}_{short.resolution.replace('x', '_')}.mp4")
    renderer = getattr(short, "renderer", "ffmpeg") or "ffmpeg"

    ctx.progress(2, 4, phase="encoding")
    if renderer == "remotion":
        from app import short_remotion
        try:
            duration = short_remotion.probe_audio_duration(audio_path)
            # Chrome của Remotion chặn file://, nên media được stage vào thư mục
            # phẳng và props chỉ mang tên file (component đọc qua staticFile).
            stage_dir = out_dir / "remotion-assets"
            staged = short_remotion.stage_assets(
                stage_dir, audio_path=audio_path, cover_path=str(cover),
                music_path=music_path)
            props = short_remotion.build_props(
                audio_path=staged["narration"],
                cover_path=staged.get("cover", ""),
                script_text=short.script_text, duration_seconds=duration,
                resolution=short.resolution, fps=int(cfg.get("fps") or 30),
                music_path=staged.get("music"),
                book_title=book.title,
                hook_text=str(saved_cfg.get("hook_text") or book.title),
            )
            short_remotion.render(props, video_path, stage_dir=stage_dir)
        except short_remotion.RemotionUnavailable as exc:
            # Lỗi cấu hình môi trường, retry cũng vô ích.
            raise JobFatalError(str(exc)) from exc
        except FileNotFoundError as exc:
            raise JobFatalError(str(exc)) from exc
        except Exception as exc:
            raise RuntimeError(f"Remotion render thất bại: {exc}") from exc
    else:
        video_gen.generate_standalone_video(
            audio_path, cover, video_path,
            resolution=short.resolution, fps=int(cfg.get("fps") or 30),
            fit_mode=str(cfg.get("fit_mode") or "auto"),
            music_path=music_path, music_volume=float(book.music_volume or 0.15),
        )
        # short_remotion đã validate bằng cách nó mong đợi; nhánh ffmpeg thì
        # kiểm tra tối thiểu để file rỗng không lọt xuống upload.
        result = validate_video(video_path)
        if not result.valid:
            raise RuntimeError(
                f"Video không hợp lệ: {result.error_code}: {result.message}")

    ctx.progress(4, 4, phase="done")
    shorts_repository.update_short(ctx.conn, short.id, status="ready",
                                   video_path=video_path)
    w, h = (int(v) for v in short.resolution.split("x"))
    return {"video_path": video_path, "resolution": short.resolution,
            "renderer": renderer, "size": f"{w}x{h}"}


def handle(ctx) -> dict:
    short_id = ctx.job.payload.get("short_id")
    if short_id is None:
        raise JobFatalError("payload thiếu short_id")
    return _handle(ctx, int(short_id))