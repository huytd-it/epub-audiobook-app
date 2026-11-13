"""Tiến độ encode thật từ ffmpeg và ước lượng thời gian render theo cấu hình."""
from __future__ import annotations

import io
import subprocess

import pytest

from app import db, video_gen
from app.jobqueue import render_progress, store
from app.jobqueue.context import JobContext
from app.jobqueue.joblog import JobLogger
from app.jobqueue.render_progress import ENCODE_END, ENCODE_START, SCALE, RenderProgress


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _isolated_data_root(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_root", str(tmp_path))


def _ctx():
    conn = db.connect(":memory:")
    db.init_schema(conn)
    job_id = store.enqueue(conn, "patch_video")
    job = store.claim(conn, "patch_video", "patch_video#0")
    return JobContext(job, conn, JobLogger(job_id, "patch_video"), lambda: False), conn, job_id


CONFIG = {"resolution": "1920x1080", "fps": 30, "codec": "libx264",
          "background": "ảnh tĩnh", "effects": ""}


class _FakePopen:
    def __init__(self, lines, returncode=0, stderr_text=""):
        self._lines, self._returncode, self._stderr_text = lines, returncode, stderr_text
        self.cmd = None

    def __call__(self, cmd, *, stdout, stderr, **_kwargs):
        self.cmd = cmd
        stderr.write(self._stderr_text)
        self.stdout = io.StringIO("".join(line + "\n" for line in self._lines))
        return self

    def wait(self):
        return self._returncode

    def kill(self):
        pass


def test_encoder_reports_ffmpeg_progress_only_inside_the_context(monkeypatch):
    popen = _FakePopen([
        "out_time_us=N/A", "speed=N/A", "progress=continue",
        "fps=60.0", "out_time_us=30000000", "speed=2.5x", "progress=continue",
        "out_time_us=90000000", "speed=3.0x", "progress=end",
    ])
    monkeypatch.setattr(video_gen.subprocess, "Popen", popen)
    events = []

    with video_gen.encoder_progress(lambda *event: events.append(event)):
        video_gen._run_encoder(["ffmpeg", "-i", "in.wav", "-c:v", "libx264", "out.mp4"])

    assert popen.cmd[:4] == ["ffmpeg", "-progress", "pipe:1", "-nostats"]
    assert events == [(0.0, None, None, False), (30.0, 2.5, 60.0, False), (90.0, 3.0, 60.0, True)]
    assert getattr(video_gen._encoder_progress, "callback", None) is None


def test_stream_copy_and_calls_outside_the_context_keep_the_plain_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(video_gen.subprocess, "run",
                        lambda cmd, **kwargs: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(video_gen.subprocess, "Popen",
                        lambda *a, **k: pytest.fail("Popen không được dùng ở đây"))

    video_gen._run_encoder(["ffmpeg", "-c:v", "libx264", "out.mp4"])
    with video_gen.encoder_progress(lambda *event: None):
        video_gen._run_encoder(["ffmpeg", "-f", "concat", "-c", "copy", "out.mp4"])

    assert len(calls) == 2


def test_failed_encode_still_raises_with_ffmpeg_stderr(monkeypatch):
    monkeypatch.setattr(video_gen.subprocess, "Popen",
                        _FakePopen([], returncode=1, stderr_text="No such file or directory"))

    with video_gen.encoder_progress(lambda *event: None):
        with pytest.raises(subprocess.CalledProcessError) as raised:
            video_gen._run_encoder(["ffmpeg", "-c:v", "libx264", "out.mp4"])

    assert "No such file" in raised.value.stderr


def test_progress_follows_the_encode_and_eta_uses_the_measured_speed():
    ctx, conn, job_id = _ctx()
    clock = _Clock()
    ctx.progress(ENCODE_START, SCALE, phase="encoding")

    with RenderProgress(ctx, video_seconds=600, config=CONFIG, clock=clock) as progress:
        assert ctx.detail["eta_sec"] is None          # chưa có lịch sử, chưa có frame nào
        clock.now = 10
        progress._on_encode(0.0, None, None, False)
        clock.now = 70                                 # 60s encode được 150s video
        progress._on_encode(150.0, 2.5, 75.0, False)

        detail = ctx.detail
        assert detail["encoded_sec"] == 150 and detail["video_sec"] == 600
        assert detail["speed"] == 2.5 and detail["measured"] is True
        assert detail["eta_sec"] == 180                # 450s video còn lại ở 2.5x
        ctx.flush()
        row = store.get(conn, job_id)
        assert row.progress_total == SCALE and row.phase == "encoding"
        assert row.progress_current == ENCODE_START + (ENCODE_END - ENCODE_START) // 4

        clock.now = 250
        progress._on_encode(600.0, 2.5, 75.0, True)

    assert progress.stats["render_sec"] == 250 and progress.stats["speed"] == 2.4
    assert render_progress.render_stats(ctx)["video_sec"] == 600


def test_finished_render_feeds_the_estimate_for_the_same_config():
    ctx, conn, _ = _ctx()
    clock = _Clock()
    with RenderProgress(ctx, video_seconds=600, config=CONFIG, clock=clock):
        clock.now = 300                                # 0.5 giây render cho mỗi giây video

    signature = render_progress.config_signature(CONFIG)
    assert render_progress.estimate_seconds(conn, signature, 1200) == 600

    clock.now = 0
    with RenderProgress(ctx, video_seconds=1200, config=CONFIG, clock=clock):
        clock.now = 100
        ctx_detail = None
    # Job kế tiếp cùng cấu hình: chưa có frame nào nhưng đã có thời gian dự kiến.
    clock.now = 0
    pending = RenderProgress(ctx, video_seconds=1200, config=CONFIG, clock=clock)
    with pending:
        ctx_detail = dict(ctx.detail)
        clock.now = 1
    assert ctx_detail["measured"] is False
    assert ctx_detail["estimated_sec"] == ctx_detail["eta_sec"] > 0


def test_multi_pass_render_never_reports_complete_before_it_ends():
    ctx, _, _ = _ctx()
    clock = _Clock()
    with RenderProgress(ctx, video_seconds=100, config=CONFIG, passes=2, clock=clock) as progress:
        clock.now = 10
        progress._on_encode(100.0, 5.0, None, True)    # xong lượt dựng nền
        assert ctx.detail["encoded_sec"] == 50
        clock.now = 20
        progress._on_encode(100.0, 5.0, None, True)    # xong lượt ghép cuối
        progress._on_encode(100.0, 5.0, None, True)    # lượt phụ ngoài dự tính
        assert ctx.detail["encoded_sec"] == 99
