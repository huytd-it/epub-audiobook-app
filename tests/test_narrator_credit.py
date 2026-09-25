"""Narrator credit overlay: video-config flag, credit text, extra overlay layers."""
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from app import image_overlay
from app.config import settings
from app.video_config import VIDEO_DEFAULTS, validate_video_config


def _book(**kwargs):
    defaults = {"id": 1, "title": "Sach Test", "overlay_config": None,
                "background_image_path": None, "tts_model": None, "tts_voice_id": None}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _patch(**kwargs):
    defaults = {"id": 1, "name": "Patch 1", "patch_index": 0,
                "chapter_start": 0, "chapter_end": 5}
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def _seed_zerotts_voices(root):
    voices_dir = root / "zerotts" / "voices"
    voices_dir.mkdir(parents=True, exist_ok=True)
    (voices_dir / "index.json").write_text(json.dumps({"voices": [
        {"name": "maichi", "display_name": "Mai Chi", "description": "nu tre"},
    ]}), encoding="utf-8")


def test_video_config_defaults_credit_off_and_validates_bool():
    assert VIDEO_DEFAULTS["narrator_credit_enabled"] is False
    assert validate_video_config({})["narrator_credit_enabled"] is False
    assert validate_video_config({"narrator_credit_enabled": True})["narrator_credit_enabled"] is True
    with pytest.raises(ValueError, match="enhancement flags must be boolean"):
        validate_video_config({"narrator_credit_enabled": "yes"})


def test_resolve_credit_preset_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "tts_api_providers", "")
    _seed_zerotts_voices(tmp_path)
    credit = image_overlay.resolve_narrator_credit(
        _book(tts_model="zerotts", tts_voice_id="preset:zerotts:maichi"))
    assert credit == "Giọng đọc: Mai Chi — nu tre · ZeroTTS"


def test_resolve_credit_cloud_voice(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "tts_api_providers", "")
    credit = image_overlay.resolve_narrator_credit(
        _book(tts_model="edge-tts", tts_voice_id="vi-VN-HoaiMyNeural"))
    assert credit == "Giọng đọc: vi-VN-HoaiMyNeural · Edge TTS"


def test_resolve_credit_empty_and_unknown_never_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "tts_api_providers", "")
    assert image_overlay.resolve_narrator_credit(_book()) == ""
    # Model lạ: dùng raw id chứ không gãy render.
    assert image_overlay.resolve_narrator_credit(_book(tts_model="nope")) == "TTS: nope"


def test_credit_overlays_gated_by_flag():
    book = _book(tts_model="edge-tts", tts_voice_id="vi")
    assert image_overlay.narrator_credit_overlays(book, None) == []
    assert image_overlay.narrator_credit_overlays(book, {}) == []
    assert image_overlay.narrator_credit_overlays(book, {"narrator_credit_enabled": False}) == []
    assert image_overlay.narrator_credit_overlays(_book(), {"narrator_credit_enabled": True}) == []
    layers = image_overlay.narrator_credit_overlays(book, {"narrator_credit_enabled": True})
    assert len(layers) == 1
    assert layers[0]["position"] == "bottom" and "Giọng đọc" in layers[0]["text"]


def test_ensure_patch_overlay_appends_extra_layer(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    bg = tmp_path / "bg.png"
    Image.new("RGB", (640, 360), (20, 20, 60)).save(str(bg), "PNG")
    book = _book(background_image_path=str(bg))
    patch = _patch()
    out_plain = tmp_path / "plain.png"
    out_credit = tmp_path / "credit.png"
    image_overlay.ensure_patch_overlay(book, patch, out_path=str(out_plain))
    layer = image_overlay.narrator_credit_layer("Giọng đọc: Mai Chi · ZeroTTS")
    image_overlay.ensure_patch_overlay(book, patch, out_path=str(out_credit),
                                       extra_overlays=[layer])
    assert out_plain.is_file() and out_credit.is_file()
    # Layer phụ phải đổi pixels thật, không chỉ chạm file.
    assert out_plain.read_bytes() != out_credit.read_bytes()
