from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf

from app import audio_mastering


def test_shared_audiobook_loudness_target_and_resampler():
    chain = audio_mastering.loudnorm_filter(sample_rate=48_000)
    assert "loudnorm=I=-16:TP=-1.5:LRA=11" in chain
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


def _voiced_rms_db(audio: np.ndarray) -> float:
    data = np.asarray(audio, dtype=np.float64).reshape(-1)
    floor = 10.0 ** (audio_mastering.CHUNK_VOICE_FLOOR_DBFS / 20.0)
    voiced = data[np.abs(data) > floor]
    return 20.0 * float(np.log10(np.sqrt(np.mean(voiced ** 2))))


def test_level_chunk_loudness_evens_out_quiet_and_loud_chunks():
    rate = 48_000
    time = np.arange(rate * 2, dtype=np.float64) / rate
    tone = np.sin(2 * np.pi * 220 * time).astype(np.float32)
    quiet = audio_mastering.level_chunk_loudness(0.05 * tone)
    loud = audio_mastering.level_chunk_loudness(0.5 * tone)

    assert abs(_voiced_rms_db(quiet) - audio_mastering.TARGET_CHUNK_RMS_DBFS) < 1.0
    assert abs(_voiced_rms_db(loud) - audio_mastering.TARGET_CHUNK_RMS_DBFS) < 1.0
    # 20 dB apart going in (0.05 vs 0.5 amplitude), nearly identical coming out.
    assert abs(_voiced_rms_db(quiet) - _voiced_rms_db(loud)) < 1.0


def test_level_chunk_loudness_leaves_silence_and_empties_alone():
    assert audio_mastering.level_chunk_loudness(np.zeros(1000, dtype=np.float32)).tolist() == [0.0] * 1000
    assert audio_mastering.level_chunk_loudness(np.zeros(0, dtype=np.float32)).size == 0
    faint = (1e-6 * np.ones(1000, dtype=np.float32))
    assert audio_mastering.level_chunk_loudness(faint) is faint or np.array_equal(
        audio_mastering.level_chunk_loudness(faint), faint)


def test_level_chunk_loudness_never_clips_and_is_idempotent():
    rng = np.random.default_rng(7)
    spiky = np.zeros(48_000, dtype=np.float32)
    spiky[::480] = 0.9  # high crest factor: loud peak, quiet body
    spiky += (0.01 * rng.standard_normal(spiky.shape)).astype(np.float32)

    once = audio_mastering.level_chunk_loudness(spiky)
    assert float(np.abs(once).max()) <= audio_mastering.CHUNK_PEAK_CEILING + 1e-6
    twice = audio_mastering.level_chunk_loudness(once)
    assert np.allclose(once, twice, atol=1e-5)
