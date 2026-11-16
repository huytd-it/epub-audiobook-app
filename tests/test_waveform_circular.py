"""Circular waveform: the polar lookup is precomputed once, never per frame."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import video_gen
from app.config import settings

CIRCULAR = {"waveform_enabled": True, "waveform_layout": "circular", "waveform_height": 200,
            "waveform_color": "#fb7185", "waveform_opacity": 1.0}


def _have_ffmpeg() -> bool:
    path = settings.get_ffmpeg_path()
    return bool(shutil.which(path) or Path(path).is_file())


def test_circular_geq_runs_only_on_the_one_frame_lookup_tables():
    chains, _, _ = video_gen._waveform_chains(CIRCULAR, 1280, 720, 1, "[0:v]", 30)
    geq_chains = [c for c in chains if "geq=" in c]
    # Every geq builds a static table from nullsrc and is frozen to one frame; the
    # audio-driven strip must go through remap, not a per-pixel expression.
    assert geq_chains and all(c.startswith("nullsrc=") and "trim=end_frame=1" in c
                              and "loop=loop=-1:size=1" in c for c in geq_chains)
    assert any("remap=" in c for c in chains)


@pytest.mark.skipif(not _have_ffmpeg(), reason="ffmpeg not available")
def test_circular_waveform_renders_and_ends_with_the_narration(tmp_path):
    ffmpeg = settings.get_ffmpeg_path()
    still, wav, out = tmp_path / "still.png", tmp_path / "n.wav", tmp_path / "out.mp4"
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "color=c=blue:s=320x240",
                    "-frames:v", "1", str(still)], check=True, capture_output=True)
    subprocess.run([ffmpeg, "-y", "-f", "lavfi", "-i", "sine=f=440:sample_rate=24000:duration=2",
                    "-ac", "1", str(wav)], check=True, capture_output=True)
    video_gen.generate_segment(str(still), str(wav), str(out), resolution=(320, 240), fps=24,
                               waveform_config=CIRCULAR)
    probe = subprocess.run([settings.get_ffprobe_path(), "-v", "error", "-show_entries",
                            "format=duration", "-of", "csv=p=0", str(out)],
                           check=True, capture_output=True, text=True)
    # The looped lookup tables are infinite; the output must still stop with the audio.
    assert abs(float(probe.stdout) - 2.0) < 0.3
