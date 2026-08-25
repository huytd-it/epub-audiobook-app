"""Voice preview endpoint: synthesizes a short in-memory sample for a
model/voice combination. Covers: success, missing model, synthesis failure,
and that no persistent file is written.
"""
from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient


def _client(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.settings.db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr("app.config.settings.enable_worker", False)
    return TestClient(__import__("app.main", fromlist=["app"]).app)


class _FakeEngine:
    """Minimal TTS engine that returns a short silent array."""

    sample_rate = 24000

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def synthesize_chunk(self, text, **kwargs):
        return np.zeros(4800, dtype="float32")


def test_voice_preview_returns_wav(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/ui/voice-preview",
            json={"model_id": "edge-tts", "voice_id": "vi-VN-HoaiMyNeural"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert len(response.content) > 0
    # WAV magic bytes
    assert response.content[:4] == b"RIFF"


def test_voice_preview_default_voice(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/ui/voice-preview",
            json={"model_id": "gtts"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"


def test_voice_preview_rejects_unknown_model(tmp_path, monkeypatch):
    with _client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/ui/voice-preview",
            json={"model_id": "nonexistent-model"},
        )
    assert response.status_code == 400
    assert "Unknown TTS engine" in response.json()["detail"]


def test_voice_preview_handles_synthesis_error(tmp_path, monkeypatch):
    """Engine that raises on synthesize_chunk must yield 500, not 500 + traceback."""
    def _broken_engine(engine_id, **kwargs):
        class Broken:
            sample_rate = 24000
            def synthesize_chunk(self, text, **kw):
                raise RuntimeError("GPU out of memory")
        return Broken()

    with _client(monkeypatch, tmp_path) as client:
        monkeypatch.setattr("app.tts_engine.create_tts_engine", _broken_engine)
        response = client.post(
            "/api/ui/voice-preview",
            json={"model_id": "edge-tts", "voice_id": "vi-VN-HoaiMyNeural"},
        )
    assert response.status_code == 500
    assert "GPU out of memory" in response.json()["detail"]


def test_voice_preview_no_persistent_file(tmp_path, monkeypatch):
    """The synthesis must not write anything under data_root."""
    monkeypatch.setattr("app.config.settings.data_root", str(tmp_path))
    voices_dir = tmp_path / "voices"
    voices_dir.mkdir()
    files_before = set(voices_dir.iterdir()) if voices_dir.exists() else set()

    with _client(monkeypatch, tmp_path) as client:
        client.post(
            "/api/ui/voice-preview",
            json={"model_id": "edge-tts", "voice_id": "vi-VN-HoaiMyNeural"},
        )

    files_after = set(voices_dir.iterdir()) if voices_dir.exists() else set()
    assert files_after == files_before
