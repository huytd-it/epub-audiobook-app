"""Non-destructive editing and cleanup for voice reference clips.

A cloning clip only works well when it is a few clean seconds of one speaker:
no room hiss, no dead air at the ends, consistent loudness. Doing that by hand
in an external editor is the slow part of building a voice library, so this
module turns the same handful of fixes into one ffmpeg pass.

Everything is expressed as a single filter chain (``_build_filter_chain``) run
through one ``ffmpeg`` invocation, so a clip is decoded and re-encoded exactly
once no matter how many operations are stacked. The chain order is deliberate
and documented there - loudness measurement has to see the audio *after* the
noise and silence are gone, and fades have to land on the final timeline.

Callers hand in a plain dict (straight off the JSON request body); ``parse_ops``
validates and clamps it, so a bad value fails before ffmpeg is started rather
than as an opaque non-zero exit.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 30
_PROCESS_TIMEOUT = 600

# Re-encode settings per container. Voice clips are short, so the wav path stays
# uncompressed (no generational loss when a clip is cleaned more than once) and
# the lossy paths use a high fixed bitrate.
_ENCODERS: dict[str, list[str]] = {
    ".wav": ["-c:a", "pcm_s16le"],
    ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
    ".m4a": ["-c:a", "aac", "-b:a", "192k"],
    ".ogg": ["-c:a", "libvorbis", "-b:a", "192k"],
}

MAX_FADE_SECONDS = 30.0
MAX_GAIN_DB = 24.0
ALLOWED_SAMPLE_RATES = (16000, 22050, 24000, 32000, 44100, 48000)


class AudioProcessError(RuntimeError):
    """ffmpeg/ffprobe refused to process the clip."""


class InvalidOps(ValueError):
    """The requested operation set is not usable."""


@dataclass
class AudioOps:
    """A validated set of edits to apply to one clip in a single pass."""

    trim_start: float = 0.0
    trim_end: float | None = None
    highpass: bool = False
    lowpass: bool = False
    denoise: bool = False
    trim_silence: bool = False
    normalize: bool = False
    gain_db: float = 0.0
    fade_in: float = 0.0
    fade_out: float = 0.0
    mono: bool = False
    sample_rate: int | None = None

    def is_empty(self) -> bool:
        """True when applying this would only re-encode the file untouched."""
        return not (
            self.trim_start > 0
            or self.trim_end is not None
            or self.highpass
            or self.lowpass
            or self.denoise
            or self.trim_silence
            or self.normalize
            or self.gain_db
            or self.fade_in > 0
            or self.fade_out > 0
            or self.mono
            or self.sample_rate
        )

    def summary(self) -> list[str]:
        """Vietnamese labels for what was applied - shown back in the UI."""
        labels: list[str] = []
        if self.trim_start > 0 or self.trim_end is not None:
            end = "hết" if self.trim_end is None else f"{self.trim_end:.2f}s"
            labels.append(f"cắt {self.trim_start:.2f}s → {end}")
        if self.trim_silence:
            labels.append("xóa khoảng lặng đầu/cuối")
        if self.highpass:
            labels.append("lọc tiếng ù trầm")
        if self.lowpass:
            labels.append("lọc tiếng rít cao")
        if self.denoise:
            labels.append("giảm nhiễu nền")
        if self.normalize:
            labels.append("chuẩn hóa âm lượng")
        if self.gain_db:
            labels.append(f"tăng/giảm {self.gain_db:+.1f} dB")
        if self.fade_in > 0:
            labels.append(f"fade in {self.fade_in:.2f}s")
        if self.fade_out > 0:
            labels.append(f"fade out {self.fade_out:.2f}s")
        if self.mono:
            labels.append("trộn về mono")
        if self.sample_rate:
            labels.append(f"resample {self.sample_rate} Hz")
        return labels


def _as_float(raw, name: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise InvalidOps(f"Giá trị '{name}' không phải là số")


def parse_ops(raw: dict | None, duration: float | None = None) -> AudioOps:
    """Validate a request body into ``AudioOps``.

    ``duration`` (when known) is used to reject a selection that starts past the
    end of the clip - that would otherwise produce a silent zero-length file.
    """
    raw = raw or {}
    if not isinstance(raw, dict):
        raise InvalidOps("Danh sách xử lý âm thanh không hợp lệ")

    trim_start = _as_float(raw.get("trim_start") or 0, "trim_start")
    trim_end_raw = raw.get("trim_end")
    trim_end = None if trim_end_raw in (None, "") else _as_float(trim_end_raw, "trim_end")
    if trim_start < 0 or (trim_end is not None and trim_end < 0):
        raise InvalidOps("Mốc cắt không được là số âm")
    if trim_end is not None and trim_end <= trim_start:
        raise InvalidOps("Mốc kết thúc phải lớn hơn mốc bắt đầu")
    if duration and trim_start >= duration:
        raise InvalidOps("Mốc bắt đầu vượt quá độ dài file")
    # A selection that runs to (or past) the end is the same as no end bound;
    # dropping it keeps the filter chain shorter and avoids atrim rounding.
    if trim_end is not None and duration and trim_end >= duration:
        trim_end = None

    fade_in = _as_float(raw.get("fade_in") or 0, "fade_in")
    fade_out = _as_float(raw.get("fade_out") or 0, "fade_out")
    if not 0 <= fade_in <= MAX_FADE_SECONDS or not 0 <= fade_out <= MAX_FADE_SECONDS:
        raise InvalidOps(f"Thời lượng fade phải trong khoảng 0 - {MAX_FADE_SECONDS:g}s")

    gain_db = _as_float(raw.get("gain_db") or 0, "gain_db")
    if abs(gain_db) > MAX_GAIN_DB:
        raise InvalidOps(f"Mức tăng/giảm âm lượng phải trong khoảng ±{MAX_GAIN_DB:g} dB")

    sample_rate_raw = raw.get("sample_rate")
    sample_rate = None
    if sample_rate_raw not in (None, "", 0, "0"):
        try:
            sample_rate = int(sample_rate_raw)
        except (TypeError, ValueError):
            raise InvalidOps("Tần số lấy mẫu không hợp lệ")
        if sample_rate not in ALLOWED_SAMPLE_RATES:
            allowed = ", ".join(str(rate) for rate in ALLOWED_SAMPLE_RATES)
            raise InvalidOps(f"Tần số lấy mẫu chỉ nhận: {allowed}")

    return AudioOps(
        trim_start=trim_start,
        trim_end=trim_end,
        highpass=bool(raw.get("highpass")),
        lowpass=bool(raw.get("lowpass")),
        denoise=bool(raw.get("denoise")),
        trim_silence=bool(raw.get("trim_silence")),
        normalize=bool(raw.get("normalize")),
        gain_db=gain_db,
        fade_in=fade_in,
        fade_out=fade_out,
        mono=bool(raw.get("mono")),
        sample_rate=sample_rate,
    )


def _build_filter_chain(ops: AudioOps) -> list[str]:
    """Order the requested edits into an ffmpeg -af chain.

    The order is what makes the result predictable:
      1. atrim  - everything downstream sees only the kept selection.
      2. filters - rumble/hiss cuts run before the noise reducer so it does not
         spend its noise floor budget on frequencies being discarded anyway.
      3. silenceremove - after cleanup, when the true silence floor is visible.
      4. loudnorm - measures the audio the listener will actually hear.
      5. volume - a deliberate offset from the normalized level, so it must not
         be normalized away.
      6. fades - land on the final, post-trim timeline.
    """
    chain: list[str] = []

    if ops.trim_start > 0 or ops.trim_end is not None:
        spec = f"atrim=start={ops.trim_start:.4f}"
        if ops.trim_end is not None:
            spec += f":end={ops.trim_end:.4f}"
        chain.append(spec)
        # Rebase timestamps, otherwise later filters (and the fades) still see
        # the original offsets and the output starts with a gap.
        chain.append("asetpts=N/SR/TB")

    if ops.highpass:
        chain.append("highpass=f=85")
    if ops.lowpass:
        chain.append("lowpass=f=12000")
    if ops.denoise:
        chain.append("afftdn=nf=-25")

    if ops.trim_silence:
        # silenceremove only trims from the start, so reverse the stream to get
        # the tail as well. Peak detection is what we want for a hard cut.
        strip = (
            "silenceremove=start_periods=1:start_duration=0:"
            "start_threshold=-45dB:detection=peak"
        )
        chain.extend([strip, "areverse", strip, "areverse"])

    if ops.normalize:
        chain.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    if ops.gain_db:
        chain.append(f"volume={ops.gain_db:.2f}dB")

    if ops.fade_in > 0:
        chain.append(f"afade=t=in:st=0:d={ops.fade_in:.4f}")
    if ops.fade_out > 0:
        # The post-filter duration is unknown here (silenceremove/loudnorm both
        # change it), so fade the tail by reversing rather than guessing st=.
        chain.extend(["areverse", f"afade=t=in:st=0:d={ops.fade_out:.4f}", "areverse"])

    return chain


def build_command(
    src: Path, dest: Path, ops: AudioOps, source_sample_rate: int | None = None
) -> list[str]:
    """The full ffmpeg argv for one processing pass (exposed for tests)."""
    cmd = [settings.get_ffmpeg_path(), "-y", "-hide_banner", "-nostdin", "-i", str(src)]
    chain = _build_filter_chain(ops)
    if chain:
        cmd += ["-af", ",".join(chain)]
    if ops.mono:
        cmd += ["-ac", "1"]
    # loudnorm upsamples to 192 kHz internally for true-peak detection and that
    # rate leaks into the output unless it is pinned - quietly quadrupling a
    # wav clip's size just because the user ticked "chuẩn hóa âm lượng".
    rate = ops.sample_rate or (source_sample_rate if ops.normalize else None)
    if rate:
        cmd += ["-ar", str(rate)]
    cmd += _ENCODERS.get(dest.suffix.lower(), ["-c:a", "pcm_s16le"])
    cmd += ["-map_metadata", "-1", str(dest)]
    return cmd


def probe(path: Path) -> dict:
    """Duration / sample rate / channels for one clip, for the editor UI.

    Returns ``{}`` when ffprobe is unavailable or the file is unreadable - the
    editor degrades to using the browser-decoded duration rather than failing.
    """
    try:
        result = subprocess.run(
            [
                settings.get_ffprobe_path(), "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=sample_rate,channels,codec_name,bit_rate",
                "-show_entries", "format=duration,size",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        logger.warning("ffprobe failed for %s", path, exc_info=True)
        return {}
    if result.returncode != 0:
        logger.warning("ffprobe returned %s for %s: %s", result.returncode, path, result.stderr[-400:])
        return {}
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return {}

    stream = (payload.get("streams") or [{}])[0]
    fmt = payload.get("format") or {}

    def _num(raw, cast):
        try:
            return cast(raw)
        except (TypeError, ValueError):
            return None

    return {
        "duration_sec": _num(fmt.get("duration"), float),
        "size": _num(fmt.get("size"), int),
        "sample_rate": _num(stream.get("sample_rate"), int),
        "channels": _num(stream.get("channels"), int),
        "codec": stream.get("codec_name"),
        "bit_rate": _num(stream.get("bit_rate"), int),
    }


def process(
    src: Path, dest: Path, ops: AudioOps, source_sample_rate: int | None = None
) -> None:
    """Apply ``ops`` to ``src``, writing ``dest``.

    Always renders to a sibling temp file first and swaps it in with
    ``os.replace``, so an ffmpeg failure (or a crash mid-encode) can never leave
    a truncated clip where a working one used to be. That matters most when
    ``dest == src``: overwriting in place is the common case, and books
    reference clips by path, so the path has to keep pointing at valid audio.

    ``source_sample_rate`` lets a caller that already probed pass the rate in;
    it is only needed to pin loudnorm's output (see ``build_command``).
    """
    if ops.normalize and not ops.sample_rate and source_sample_rate is None:
        source_sample_rate = probe(src).get("sample_rate")
    temp = dest.with_name(f".{dest.name}.processing{dest.suffix}")
    cmd = build_command(src, temp, ops, source_sample_rate)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=_PROCESS_TIMEOUT,
        )
        if result.returncode != 0:
            raise AudioProcessError(
                f"ffmpeg thất bại (mã {result.returncode}): {(result.stderr or '').strip()[-500:]}"
            )
        if not temp.exists() or temp.stat().st_size == 0:
            raise AudioProcessError("ffmpeg không tạo được file kết quả (kết quả rỗng)")
        os.replace(temp, dest)
    except subprocess.TimeoutExpired:
        raise AudioProcessError("Xử lý âm thanh quá thời gian cho phép")
    except FileNotFoundError:
        raise AudioProcessError(
            "Không tìm thấy ffmpeg. Cài ffmpeg hoặc đặt ffmpeg.exe vào assets/bin."
        )
    finally:
        if temp.exists():
            temp.unlink(missing_ok=True)
