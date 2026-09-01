"""Part 1 — 5 helpers phải trả về path tuyệt đối và bám theo settings.data_root."""
from pathlib import Path

import pytest

from app.config import settings
from app import repository


def test_chunk_dir_for_is_absolute_and_follows_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "mydata"))
    p = repository._chunk_dir_for(book_id=7, patch_id=99)
    assert p.is_absolute()
    assert str(p).startswith(str(tmp_path / "mydata"))
    assert p == Path(settings.data_root) / "books" / "7" / "patches" / "99_chunks"


def test_get_patch_audio_path_is_absolute_and_follows_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "data2"))
    p = repository.get_patch_audio_path(book_id=25, patch_index=11)
    assert p.is_absolute()
    assert p == Path(settings.data_root) / "books" / "25" / "audio" / "25_012.wav"


def test_get_patch_chunk_dir_is_absolute_and_follows_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "root"))
    p = repository.get_patch_chunk_dir(book_id=3, patch_index=0)
    assert p.is_absolute()
    assert p == Path(settings.data_root) / "books" / "3" / "audio" / "3_001_chunks"


def test_get_backup_path_is_absolute_and_follows_data_root(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "bk"))
    p = repository.get_backup_path(book_id=2, patch_index=4, extension=".wav", timestamp="20240101_010101")
    assert p.is_absolute()
    assert str(p).startswith(str(tmp_path / "bk"))
    assert p == Path(settings.data_root) / "books" / "2" / "backup_audio" / "2_005_20240101_010101.wav"


def test_backup_helpers_use_data_root(tmp_path, monkeypatch):
    # backup_patch_audio_files và backup_all_book_audio phải tạo thư mục dưới data_root
    monkeypatch.setattr(settings, "data_root", str(tmp_path / "ds"))
    # backup_patch_audio_files: tạo backup từ file giả
    audio = tmp_path / "ds" / "books" / "1" / "audio" / "1_001.wav"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"wavdata")
    # audio_path truyền vào là đường dẫn tuyệt đối hiện tại
    repository.backup_patch_audio_files(book_id=1, patch_index=0, old_audio_path=str(audio))
    # phải có file backup dưới settings.data_root
    backup_dir = Path(settings.data_root) / "books" / "1" / "backup_audio"
    assert backup_dir.is_dir()
    assert any(backup_dir.glob("1_001_*.wav"))

    # backup_all_book_audio: backup toàn bộ audio
    audio2 = Path(settings.data_root) / "books" / "1" / "audio" / "1_002.wav"
    audio2.write_bytes(b"more")
    (audio2.with_suffix(".timeline.json")).write_text("{}", encoding="utf-8")
    repository.backup_all_book_audio(book_id=1)
    # vẫn dưới data_root
    assert (Path(settings.data_root) / "books" / "1" / "backup_audio").is_dir()
