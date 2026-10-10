"""Shared loudness mastering for audiobook WAV and video outputs.

Patch WAVs are mastered only after every TTS chunk (and optional sound effect)
has been assembled. That preserves the relative dynamics between chunks while
giving audiobook WAVs a consistent perceived level. Video uses a louder target
for its final stereo narration/music mix.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from json import JSONDecodeError
from os import replace as atomic_replace
from pathlib import Path

import soundfile as sf
import numpy as np

from app.config import settings


# Comfortable spoken-word loudness with true-peak headroom for AAC/YouTube.
# LRA=11 is a ceiling, not added compression: steady TTS narration keeps its
# naturally smaller loudness range.
TARGET_LUFS = -16.0
# YouTube-oriented video mix; keep the audiobook WAV comfortable at -16 LUFS.
VIDEO_TARGET_LUFS = -14.0
TARGET_TRUE_PEAK_DBFS = -1.5
TARGET_LRA = 11.0
VIDEO_SAMPLE_RATE = 48_000


@dataclass(frozen=True)
class LoudnessMeasurement:
    input_i: float
    input_tp: float
    input_lra: float
    input_thresh: float
    target_offset: float


def loudnorm_filter(
    measurement: LoudnessMeasurement | None = None,
    *,
    target_lufs: float = TARGET_LUFS,
    sample_rate: int | None = None,
    dither: bool = False,
    print_format: str | None = None,
) -> str:
    """Build the shared FFmpeg loudnorm chain.

    A supplied first-pass measurement enables the accurate second pass used for
    WAV mastering. Video mixes assembled inside one filter graph use the dynamic
    one-pass form because narration and music do not exist as a mixed file yet.
    """
    chain = (
        f"loudnorm=I={target_lufs:g}:TP={TARGET_TRUE_PEAK_DBFS:g}:"
        f"LRA={TARGET_LRA:g}"
    )
    if measurement is not None:
        chain += (
            f":measured_I={measurement.input_i:g}"
            f":measured_TP={measurement.input_tp:g}"
            f":measured_LRA={measurement.input_lra:g}"
            f":measured_thresh={measurement.input_thresh:g}"
            f":offset={measurement.target_offset:g}:linear=true"
        )
    if print_format:
        chain += f":print_format={print_format}"
    if sample_rate:
        resample = f"aresample={int(sample_rate)}:resampler=soxr:precision=28"
        if dither:
            resample += ":dither_method=triangular_hp"
        chain += f",{resample}"
    return chain


def _measurement_from_stderr(stderr: str) -> LoudnessMeasurement:
    decoder = json.JSONDecoder()
    payload = None
    for index, char in enumerate(stderr):
        if char != "{":
            continue
        try:
            candidate, _ = decoder.raw_decode(stderr[index:])
        except (JSONDecodeError, TypeError):
            continue
        if isinstance(candidate, dict) and "input_i" in candidate:
            payload = candidate
    if payload is None:
        raise RuntimeError("FFmpeg loudnorm did not return a measurement")

    try:
        values = {
            key: float(payload[key])
            for key in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("FFmpeg loudnorm returned an invalid measurement") from exc
    if not all(math.isfinite(value) for value in values.values()):
        raise RuntimeError("cannot loudness-normalize silent or invalid audio")
    return LoudnessMeasurement(**values)


def measure_loudness(path: str | Path) -> LoudnessMeasurement:
    result = subprocess.run(
        [
            settings.get_ffmpeg_path(), "-hide_banner", "-nostdin", "-nostats",
            "-i", str(Path(path)), "-map", "0:a:0", "-vn",
            "-af", loudnorm_filter(print_format="json"),
            "-f", "null", os.devnull,
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return _measurement_from_stderr(result.stderr)


def _pcm_codec(subtype: str) -> str:
    return {
        "PCM_U8": "pcm_u8",
        "PCM_16": "pcm_s16le",
        "PCM_24": "pcm_s24le",
        "PCM_32": "pcm_s32le",
        "FLOAT": "pcm_f32le",
        "DOUBLE": "pcm_f64le",
    }.get(subtype, "pcm_s24le")


# Per-chunk leveling target: every TTS chunk leaves synthesis at the same speech
# RMS so concatenated narration sounds even. The final integrated master
# (normalize_wav_in_place, TARGET_LUFS) then only sets the absolute level.
# ZeroTTS in particular wanders several dB between chunks (measured ~8 dB RMS
# spread on one patch), and a whole-file master preserves those gaps because it
# applies a single global gain.
TARGET_CHUNK_RMS_DBFS = -20.0
# Never turn the knob further than this: a near-silent/broken chunk must not
# be amplified into audible hiss, nor a hot one squashed into pumping.
MAX_CHUNK_GAIN_DB = 12.0
# Chunks quieter than this are left untouched (silence, gaps, failures).
MIN_CHUNK_RMS_DBFS = -45.0
# Speech detector floor: RMS is measured over samples above this so leading /
# trailing pauses don't drag the speech level down unevenly between chunks.
CHUNK_VOICE_FLOOR_DBFS = -40.0
# Hard ceiling after gain, mirroring the true-peak headroom of the master.
CHUNK_PEAK_CEILING = 0.98


def level_chunk_loudness(audio: np.ndarray) -> np.ndarray:
    """Match one TTS chunk to TARGET_CHUNK_RMS_DBFS. Pure numpy, idempotent.

    Returns a new float32 mono array (or the input untouched when it is empty
    or near-silent). A peak guard caps the result at CHUNK_PEAK_CEILING so a
    high-crest chunk can never clip — it just lands slightly quieter.
    """
    data = np.asarray(audio, dtype=np.float32).reshape(-1)
    if data.size == 0:
        return data
    overall = float(np.sqrt(np.mean(data ** 2)))
    if overall <= 0.0 or 20.0 * math.log10(overall) < MIN_CHUNK_RMS_DBFS:
        return data
    floor = 10.0 ** (CHUNK_VOICE_FLOOR_DBFS / 20.0)
    voiced = data[np.abs(data) > floor]
    reference = voiced if voiced.size else data
    rms = float(np.sqrt(np.mean(reference ** 2)))
    if rms <= 0.0:
        return data
    gain_db = TARGET_CHUNK_RMS_DBFS - 20.0 * math.log10(rms)
    gain_db = max(-MAX_CHUNK_GAIN_DB, min(MAX_CHUNK_GAIN_DB, gain_db))
    out = data * (10.0 ** (gain_db / 20.0))
    peak = float(np.abs(out).max(initial=0.0))
    if peak > CHUNK_PEAK_CEILING:
        out = out * (CHUNK_PEAK_CEILING / peak)
    return np.ascontiguousarray(out, dtype=np.float32)


def normalize_wav_in_place(path: str | Path) -> LoudnessMeasurement | None:
    """Two-pass loudness-master one WAV atomically, preserving its format.

    Already-mastered files stay byte-for-byte untouched to prevent cumulative
    processing when a complete book is assembled from mastered patch WAVs.
    """
    source = Path(path)
    info = sf.info(str(source))
    try:
        measurement = measure_loudness(source)
    except RuntimeError as exc:
        # Loudness is undefined for digital silence. Keep a deliberately silent
        # WAV intact; applying arbitrary gain would only amplify quantization
        # noise and should not make an otherwise valid pipeline job fail.
        if "silent or invalid audio" in str(exc):
            return None
        raise
    if (
        abs(measurement.input_i - TARGET_LUFS) <= 0.15
        and measurement.input_tp <= TARGET_TRUE_PEAK_DBFS + 0.05
        and measurement.input_lra <= TARGET_LRA + 0.1
    ):
        return measurement

    fd, temp_name = tempfile.mkstemp(
        dir=source.parent, prefix=f".{source.name}.", suffix=".mastering.wav"
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        subprocess.run(
            [
                settings.get_ffmpeg_path(), "-y", "-hide_banner", "-nostdin", "-nostats",
                "-i", str(source), "-map", "0:a:0", "-vn",
                "-af", loudnorm_filter(
                    measurement,
                    sample_rate=info.samplerate,
                    dither=True,
                    print_format="summary",
                ),
                "-ar", str(info.samplerate), "-ac", str(info.channels),
                "-c:a", _pcm_codec(info.subtype),
                str(temp_path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        mastered = sf.info(str(temp_path))
        if mastered.samplerate != info.samplerate or mastered.channels != info.channels:
            raise RuntimeError("mastered WAV format does not match its source")
        before_seconds = info.frames / info.samplerate
        after_seconds = mastered.frames / mastered.samplerate
        if abs(after_seconds - before_seconds) > 0.05:
            raise RuntimeError("mastered WAV duration changed unexpectedly")
        atomic_replace(temp_path, source)
    finally:
        temp_path.unlink(missing_ok=True)
    return measurement
