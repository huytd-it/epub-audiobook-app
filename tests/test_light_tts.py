"""Tests for LightTTSEngine."""
from __future__ import annotations

import pytest


class TestLightTTSEngine:
    def test_list_backends(self):
        from app.light_tts import LightTTSEngine

        engine = LightTTSEngine.__new__(LightTTSEngine)
        engine.backend = "edge-tts"
        engine.voice = "vi-VN-HoaiMyNeural"
        backends = engine.list_backends()
        names = [b["name"] for b in backends]
        assert "edge-tts" in names
        assert "gtts" in names

    def test_only_supported_backends_are_listed(self):
        from app.light_tts import _BACKENDS, _BACKEND_SYNTH

        assert "kokoro" not in _BACKENDS
        assert "kokoro" not in _BACKEND_SYNTH
        assert set(_BACKENDS) == {"edge-tts", "gtts"}

    def test_synthesize_unavailable_backend(self):
        from app.light_tts import _check_backend

        with pytest.raises(RuntimeError, match="Unknown TTS backend"):
            _check_backend("nonexistent")

    def test_synthesize_with_edge_tts(self):
        pytest.importorskip("edge_tts")
        from app.light_tts import LightTTSEngine

        engine = LightTTSEngine(backend="edge-tts")
        wav_bytes, sr = engine.synthesize_to_wav_bytes("Xin chào")
        assert len(wav_bytes) > 44  # WAV header is 44 bytes
        assert sr > 0


class TestEdgeSynthesizeRetry:
    """The Edge endpoint drops streams mid-run on valid text (NoAudioReceived);
    _edge_tts_synthesize must absorb that instead of failing the patch job."""

    @staticmethod
    def _install_fake_communicate(monkeypatch, failures: int):
        edge_tts = pytest.importorskip("edge_tts")
        import app.light_tts as lt

        calls = {"n": 0}

        class _FakeCommunicate:
            def __init__(self, text, voice):
                self.text = text

            async def stream(self):
                calls["n"] += 1
                if calls["n"] <= failures:
                    raise edge_tts.exceptions.NoAudioReceived("No audio was received.")
                yield {"type": "audio", "data": b"mp3-bytes"}

        monkeypatch.setattr(edge_tts, "Communicate", _FakeCommunicate)
        monkeypatch.setattr(lt.time, "sleep", lambda _s: None)
        monkeypatch.setattr(lt, "_mp3_to_wav_bytes", lambda b: (b"wav:" + b, 24000))
        return calls

    def test_retries_then_succeeds(self, monkeypatch):
        from app.light_tts import _edge_tts_synthesize

        calls = self._install_fake_communicate(monkeypatch, failures=2)
        wav_bytes, sr = _edge_tts_synthesize("Xin chào", "vi-VN-HoaiMyNeural")
        assert wav_bytes == b"wav:mp3-bytes"
        assert sr == 24000
        assert calls["n"] == 3

    def test_raises_after_exhausting_retries(self, monkeypatch):
        edge_tts = pytest.importorskip("edge_tts")
        from app.light_tts import _EDGE_RETRIES, _edge_tts_synthesize

        calls = self._install_fake_communicate(monkeypatch, failures=99)
        with pytest.raises(edge_tts.exceptions.NoAudioReceived):
            _edge_tts_synthesize("Xin chào", "vi-VN-HoaiMyNeural")
        assert calls["n"] == _EDGE_RETRIES


class TestListVoices:
    def test_edge_tts_sorted_vietnamese_first(self, monkeypatch):
        import app.light_tts as lt

        fake = [
            {"ShortName": "en-US-AriaNeural", "Gender": "Female", "Locale": "en-US"},
            {"ShortName": "vi-VN-NamMinhNeural", "Gender": "Male", "Locale": "vi-VN"},
            {"ShortName": "vi-VN-HoaiMyNeural", "Gender": "Female", "Locale": "vi-VN"},
        ]
        lt._EDGE_VOICES_CACHE = None
        monkeypatch.setattr(lt, "_edge_list_voices_raw", lambda: fake)
        voices = lt.list_voices("edge-tts")
        assert voices[0]["language"] == "vi-VN"
        assert voices[1]["language"] == "vi-VN"
        assert voices[-1]["id"] == "en-US-AriaNeural"
        assert voices[0]["id"].startswith("vi-VN-")
        assert "(" in voices[0]["label"]  # gender in label

    def test_gtts_lists_languages(self, monkeypatch):
        import app.light_tts as lt

        monkeypatch.setattr(lt, "_gtts_langs", lambda: {"vi": "Vietnamese", "en": "English"})
        voices = lt.list_voices("gtts")
        ids = {v["id"] for v in voices}
        assert ids == {"vi", "en"}
        vi = next(v for v in voices if v["id"] == "vi")
        assert vi["label"] == "Vietnamese"

    def test_fallback_on_enumeration_error(self, monkeypatch):
        import app.light_tts as lt

        def _boom():
            raise RuntimeError("network down")

        lt._EDGE_VOICES_CACHE = None
        monkeypatch.setattr(lt, "_edge_list_voices_raw", _boom)
        voices = lt.list_voices("edge-tts")
        assert voices == [{
            "id": "vi-VN-HoaiMyNeural",
            "label": "vi-VN-HoaiMyNeural",
            "language": "",
        }]


