from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from app import audio_mastering


def test_shared_audiobook_loudness_target_and_resampler():
    chain = audio_mastering.loudnorm_filter(sample_rate=48_000)
    assert "loudnorm=I=-18:TP=-1.5:LRA=11" in chain
    assert "aresample=48000:resampler=soxr:precision=28" in chain


def test_measure_loudness_parses_ffmpeg_json(monkeypatch, tmp_path):
    payload = {
        "input_i": "-23.30",
        "input_tp": "-1.10",
        "input_lra": "4.80",
        "input_thresh": "-33.40",
        "target_offset": "-0.10",
    }
    monkeypatch.setattr(
        audio_mastering.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(stderr="noise\n" + json.dumps(payload)),
    )
    source = tmp_path / "source.wav"
    source.write_bytes(b"not read by the mocked first pass")

    measured = audio_mastering.measure_loudness(source)

    assert measured.input_i == -23.3
    assert measured.input_tp == -1.1
    assert measured.input_lra == 4.8


def test_measured_filter_is_linear_two_pass():
    measured = audio_mastering.LoudnessMeasurement(-23.3, -1.1, 4.8, -33.4, -0.1)
    chain = audio_mastering.loudnorm_filter(measured, sample_rate=24_000, dither=True)
    assert "measured_I=-23.3" in chain
    assert "measured_TP=-1.1" in chain
    assert "linear=true" in chain
    assert "dither_method=triangular_hp" in chain


def test_invalid_or_silent_measurement_is_rejected():
    payload = json.dumps({
        "input_i": "-inf",
        "input_tp": "-inf",
        "input_lra": "0",
        "input_thresh": "-70",
        "target_offset": "0",
    })
    with pytest.raises(RuntimeError, match="silent or invalid"):
        audio_mastering._measurement_from_stderr(payload)


def test_real_two_pass_mastering_reaches_target_and_preserves_wav_format(tmp_path):
    rate = 24_000
    seconds = 8
    time = np.arange(rate * seconds, dtype=np.float64) / rate
    # A gently modulated speech-like signal starts well below the target.
    envelope = 0.3 + (0.7 * np.sin(2 * np.pi * 1.7 * time) ** 2)
    signal = 0.025 * envelope * (
        np.sin(2 * np.pi * 180 * time) + 0.35 * np.sin(2 * np.pi * 540 * time)
    )
    source = tmp_path / "quiet.wav"
    sf.write(source, signal, rate, subtype="PCM_16")

    audio_mastering.normalize_wav_in_place(source)

    info = sf.info(source)
    output = audio_mastering.measure_loudness(source)
    assert (info.samplerate, info.channels, info.subtype) == (rate, 1, "PCM_16")
    assert info.frames == rate * seconds
    assert abs(output.input_i - audio_mastering.TARGET_LUFS) <= 0.2
    assert output.input_tp <= audio_mastering.TARGET_TRUE_PEAK_DBFS + 0.1
