"""Tiến độ và thời gian dự kiến cho các job render video.

Hai nguồn số liệu, dùng theo thứ tự:

1. Trước khi ffmpeg báo frame đầu tiên: ước lượng từ lịch sử. Mỗi lần render xong,
   tỉ lệ "giây render / giây video" được lưu theo chữ ký cấu hình (độ phân giải, fps,
   codec, kiểu nền, hiệu ứng). Job mới cùng cấu hình lấy tỉ lệ đó nhân với thời lượng
   audio. Lịch sử nằm trong app_state vì bảng job bị dọn mỗi lần khởi động.
2. Khi đang encode: vị trí thật ffmpeg báo qua '-progress' (xem
   video_gen.encoder_progress), ETA tính từ tốc độ đo được.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from app import repository, video_gen

RENDER_RATE_STATE_KEY = "video.render_rate"
_MAX_SIGNATURES = 64
# Thang tiến độ của job render: dành một đoạn đầu cho khâu chuẩn bị và một đoạn
# cuối cho đăng ký/publish, phần giữa chạy theo tiến độ encode thật.
SCALE = 1000
ENCODE_START = 20
ENCODE_END = 950


def config_signature(config: dict[str, Any]) -> str:
    """Chữ ký các yếu tố quyết định tốc độ render."""
    return "|".join(
        str(config.get(key) or "-")
        for key in ("resolution", "fps", "codec", "background", "effects")
    )


def _load_rates(conn: sqlite3.Connection) -> dict[str, float]:
    try:
        raw = json.loads(repository.get_app_state(conn, RENDER_RATE_STATE_KEY) or "{}")
    except (json.JSONDecodeError, sqlite3.Error):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {k: float(v) for k, v in raw.items() if isinstance(v, (int, float)) and v > 0}


def estimate_seconds(conn: sqlite3.Connection, signature: str, video_seconds: float) -> float | None:
    """Thời gian render dự kiến từ lịch sử; None khi chưa từng render lần nào."""
    rates = _load_rates(conn)
    if not rates or video_seconds <= 0:
        return None
    # Chưa có cấu hình y hệt thì lấy trung bình mọi cấu hình: thô, nhưng vẫn hơn
    # không có con số nào.
    rate = rates.get(signature) or sum(rates.values()) / len(rates)
    return rate * video_seconds


def record_rate(conn: sqlite3.Connection, signature: str, render_seconds: float,
                video_seconds: float) -> None:
    if render_seconds <= 0 or video_seconds <= 0:
        return
    rates = _load_rates(conn)
    rate = render_seconds / video_seconds
    previous = rates.pop(signature, None)
    # Trung bình trượt: lần mới nhất nặng ngang toàn bộ quá khứ, để đổi máy/đổi
    # preset thì ước lượng theo kịp sau vài lần render.
    rates[signature] = rate if previous is None else (previous + rate) / 2
    while len(rates) > _MAX_SIGNATURES:
        rates.pop(next(iter(rates)))   # dict giữ thứ tự chèn: bỏ chữ ký cũ nhất
    try:
        repository.set_app_state(conn, RENDER_RATE_STATE_KEY, json.dumps(rates))
    except sqlite3.Error:
        pass   # mất một mẫu thống kê không đáng làm hỏng job đã render xong


class RenderProgress:
    """Bọc khối render: ``with RenderProgress(ctx, ...) as progress: render()``.

    ``passes`` là số lượt ffmpeg mã hóa lại toàn bộ thời lượng (ví dụ nền chuỗi ảnh:
    một lượt dựng nền + một lượt ghép audio/hiệu ứng). Đây chỉ là ước lượng, nên
    phần trăm được chặn ở 99% cho tới khi render thật sự kết thúc.
    """

    def __init__(self, ctx, *, video_seconds: float | None, config: dict[str, Any],
                 passes: float = 1.0, clock=time.monotonic):
        self._ctx = ctx
        self._video_seconds = float(video_seconds or 0)
        self._config = {k: v for k, v in config.items() if v not in (None, "")}
        self._signature = config_signature(config)
        self._total_work = self._video_seconds * max(1.0, passes)
        self._clock = clock
        self._started_at = clock()
        self._first_event_at: float | None = None
        self._done_work = 0.0
        self._current = 0.0
        self._conn = getattr(ctx, "conn", None)
        self._estimate = None
        if self._conn is not None:
            try:
                self._estimate = estimate_seconds(self._conn, self._signature, self._video_seconds)
            except sqlite3.Error:
                pass
        self._cm = None
        self.stats: dict[str, Any] | None = None

    # ------------------------------------------------------------ context manager

    def __enter__(self) -> "RenderProgress":
        self._publish(speed=None, fps=None)
        self._cm = video_gen.encoder_progress(self._on_encode)
        self._cm.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._cm.__exit__(exc_type, exc, tb)
        if exc_type is None:
            self._finish()

    # ------------------------------------------------------------------ nội bộ

    def _on_encode(self, out_seconds: float, speed: float | None, fps: float | None,
                   done: bool) -> None:
        if self._first_event_at is None:
            self._first_event_at = self._clock()
        if done:
            self._done_work += out_seconds
            self._current = 0.0
        else:
            self._current = out_seconds
        self._publish(speed=speed, fps=fps)

    def _fraction(self) -> float:
        if self._total_work <= 0:
            return 0.0
        return min(0.99, (self._done_work + self._current) / self._total_work)

    def _eta(self, now: float) -> float | None:
        work = self._done_work + self._current
        encoding_for = now - self._first_event_at if self._first_event_at is not None else 0.0
        # Vài giây đầu tốc độ đo được còn nhiễu (ffmpeg đang nạp input, dựng filter).
        if work > 0 and encoding_for >= 5 and self._total_work > work:
            return (self._total_work - work) * encoding_for / work
        if self._estimate is not None:
            return max(0.0, self._estimate - (now - self._started_at))
        return None

    def _publish(self, *, speed: float | None, fps: float | None) -> None:
        now = self._clock()
        fraction = self._fraction()
        eta = self._eta(now)
        detail = {
            "kind": "video",
            **self._config,
            "video_sec": round(self._video_seconds, 1),
            "encoded_sec": round(fraction * self._video_seconds, 1),
            "speed": round(speed, 2) if speed else None,
            "encode_fps": round(fps, 1) if fps else None,
            "eta_sec": round(eta) if eta is not None else None,
            "estimated_sec": round(self._estimate) if self._estimate is not None else None,
            "measured": self._first_event_at is not None,
        }
        # ctx giả trong test có thể không có .detail — không bắt buộc.
        if hasattr(self._ctx, "detail"):
            self._ctx.detail = detail
        self._ctx.progress(ENCODE_START + int((ENCODE_END - ENCODE_START) * fraction), SCALE)

    def _finish(self) -> None:
        render_seconds = self._clock() - self._started_at
        self.stats = {
            **self._config,
            "video_sec": round(self._video_seconds, 1),
            "render_sec": round(render_seconds, 1),
            "speed": (round(self._video_seconds / render_seconds, 2)
                      if render_seconds > 0 and self._video_seconds > 0 else None),
        }
        if hasattr(self._ctx, "detail"):
            self._ctx.detail = {"kind": "video", **self.stats}
        self._ctx.progress(ENCODE_END, SCALE)
        if self._conn is not None:
            try:
                record_rate(self._conn, self._signature, render_seconds, self._video_seconds)
            except sqlite3.Error:
                pass


def render_stats(ctx) -> dict[str, Any] | None:
    """Số liệu lần render vừa xong, để handler đưa vào result của job."""
    detail = getattr(ctx, "detail", None)
    return dict(detail) if isinstance(detail, dict) and "render_sec" in detail else None
