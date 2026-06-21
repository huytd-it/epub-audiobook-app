"""Mux a static background image with the final audio into a simple mp4 via ffmpeg."""
from __future__ import annotations

import subprocess


def generate_video(audio_path: str, background_image_path: str, out_path: str, *, use_nvenc: bool = False) -> None:
    video_codec = "h264_nvenc" if use_nvenc else "libx264"
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", background_image_path,
        "-i", audio_path,
        "-c:v", video_codec,
        "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-shortest",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
