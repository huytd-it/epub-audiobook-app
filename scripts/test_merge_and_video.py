"""Phase 5/6 acceptance check: merge several wavs into one final wav, then mux with a background
image into a final mp4 via ffmpeg."""
import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import soundfile as sf

from app.audio_merge import concat_chunks_to_wav, merge_patches_to_final
from app.video_gen import generate_video


def main() -> None:
    tmp = Path("/tmp/merge_test")
    tmp.mkdir(exist_ok=True)
    sample_rate = 48000

    patch_paths = []
    expected_total_duration = 0.0
    for i, seconds in enumerate([1.0, 2.5, 0.7]):
        chunk_a = np.zeros(int(seconds * 0.5 * sample_rate), dtype=np.float32)
        chunk_b = (np.sin(2 * np.pi * 440 * np.arange(int(seconds * 0.5 * sample_rate)) / sample_rate) * 0.1).astype(np.float32)
        path = str(tmp / f"patch{i}.wav")
        concat_chunks_to_wav([chunk_a, chunk_b], sample_rate, path)
        patch_paths.append(path)
        expected_total_duration += seconds

    final_wav = str(tmp / "final.wav")
    merge_patches_to_final(patch_paths, final_wav)

    info = sf.info(final_wav)
    print(f"Final wav duration: {info.duration:.2f}s (expected ~{expected_total_duration:.2f}s)")
    assert abs(info.duration - expected_total_duration) < 0.05, "merged duration mismatch"
    print("Merge OK: duration matches sum of inputs within tolerance")

    bg_image = tmp / "bg.jpg"
    if not bg_image.exists():
        # 1x1 solid color jpg, good enough to validate the ffmpeg pipeline without a real asset
        from PIL import Image

        Image.new("RGB", (640, 360), color=(30, 30, 30)).save(bg_image)

    final_mp4 = str(tmp / "final.mp4")
    generate_video(final_wav, str(bg_image), final_mp4)
    print(f"Wrote {final_mp4}")

    import subprocess

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", final_mp4],
        capture_output=True, text=True, check=True,
    )
    video_duration = float(probe.stdout.strip())
    print(f"Video duration: {video_duration:.2f}s")
    assert abs(video_duration - expected_total_duration) < 0.2, "video duration doesn't match audio"
    print("Video OK: audio/video duration in sync")


if __name__ == "__main__":
    main()
