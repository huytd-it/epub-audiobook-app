"""Render real speech through the production WAV/video pipeline and measure it.

Run from the project root, for example:
    python -m scripts.check_audio_loudness --engine zerotts --voice kimoanh \
        --out-dir data/loudness-check/before
    python -m scripts.check_audio_loudness --source data/books/38/audio/38_017.wav \
        --start 60 --seconds 60 --out-dir data/loudness-check/existing

Only writes diagnostic files in out-dir; does not enqueue jobs or update books.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from dataclasses import asdict
from pathlib import Path

import soundfile as sf
from PIL import Image

from app import audio_mastering, audio_merge, video_gen
from app.chunker import split_into_tts_chunks
from app.config import settings
from app.tts_engine import create_tts_engine


SAMPLE_TEXT = (
    "Trời vừa hửng sáng, gió từ mặt sông thổi vào mát rượi. "
    "Người kể chuyện dừng lại một nhịp rồi đọc tiếp trang sách còn dang dở. "
    "Trong căn phòng yên tĩnh, tiếng bước chân khẽ vang lên ngoài hành lang. "
    "Anh đặt cuốn sách xuống bàn, mở cửa sổ và nhìn ra khu vườn. "
    "Hôm nay chúng ta sẽ bắt đầu một hành trình mới, anh nói. "
    "Có những đoạn cần đọc chậm để người nghe cảm nhận được từng chi tiết, "
    "cũng có những câu phải rõ ràng và dứt khoát. "
    "Dù nghe bằng tai nghe hay loa điện thoại, lời kể vẫn cần giữ được sự tự nhiên. "
    "Một cơn gió nhẹ lướt qua, mang theo hương hoa và tiếng chim từ xa."
)


def measure(path: Path) -> dict:
    probe = subprocess.run(
        [settings.get_ffprobe_path(), "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=codec_name,sample_rate,channels,duration",
         "-of", "json", str(path)],
        check=True, capture_output=True, text=True,
    )
    return {
        "path": str(path),
        "format": json.loads(probe.stdout)["streams"][0],
        **asdict(audio_mastering.measure_loudness(path)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="Existing audio/video; otherwise synthesize sample speech")
    parser.add_argument("--start", type=float, default=0)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--engine", default="edge-tts")
    parser.add_argument("--voice")
    parser.add_argument("--music", type=Path)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    # Each run has its own outputs so before/after listening stays reproducible.
    args.out_dir.mkdir(parents=True, exist_ok=False)
    raw = args.out_dir / "raw.wav"
    if args.source:
        subprocess.run(
            [settings.get_ffmpeg_path(), "-y", "-hide_banner", "-nostdin",
             "-ss", str(args.start), "-i", str(args.source), "-t", str(args.seconds),
             "-map", "0:a:0", "-vn", "-c:a", "pcm_s24le", str(raw)],
            check=True, capture_output=True,
        )
    else:
        (args.out_dir / "text.txt").write_text(SAMPLE_TEXT, encoding="utf-8")
        engine = create_tts_engine(args.engine, voice=args.voice)
        chunks = []
        for text in split_into_tts_chunks(SAMPLE_TEXT, max_chars=400):
            chunks.append(engine.synthesize_chunk(text))
        audio_merge.concat_chunks_to_wav(chunks, engine.sample_rate, str(raw))
    mastered = args.out_dir / "mastered.wav"
    shutil.copyfile(raw, mastered)
    audio_mastering.normalize_wav_in_place(mastered)
    background = args.out_dir / "background.png"
    Image.new("RGB", (640, 360), (28, 35, 46)).save(background)
    video = args.out_dir / "video.mp4"
    video_gen.generate_segment(
        str(background), str(mastered), str(video), resolution=(640, 360), fps=30,
        music_path=str(args.music) if args.music else None,
    )
    report = {
        "source": str(args.source) if args.source else {"engine": args.engine, "voice": args.voice},
        "source_start": args.start if args.source else 0,
        "wav_target_lufs": audio_mastering.TARGET_LUFS,
        "video_filter": video_gen.AUDIO_LOUDNESS_FILTER,
        "duration_seconds": sf.info(raw).duration,
        "stages": {name: measure(path) for name, path in
                   (("raw", raw), ("mastered", mastered), ("video", video))},
    }
    encoded = json.dumps(report, indent=2, ensure_ascii=False)
    (args.out_dir / "measurements.json").write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
