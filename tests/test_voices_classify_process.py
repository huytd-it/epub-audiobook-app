"""Route tests for voice classification (gender/genre) and audio processing.

The processing tests that actually render audio are skipped when ffmpeg is not
on the machine; the validation and bookkeeping tests always run.
"""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    with TestClient(app) as c:
        yield c


def _voices_dir() -> Path:
    from app.config import settings

    d = Path(settings.data_root) / "voices"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _db() -> sqlite3.Connection:
    from app.config import settings

    c = sqlite3.connect(settings.db_path)
    c.row_factory = sqlite3.Row
    return c


def _has_ffmpeg() -> bool:
    from app.config import settings

    return shutil.which(settings.get_ffmpeg_path()) is not None or Path(settings.get_ffmpeg_path()).exists()


needs_ffmpeg = pytest.mark.skipif(not _has_ffmpeg(), reason="ffmpeg không có trên máy này")


def _make_wav(dest: Path, seconds: float = 3.0, silent_head: float = 0.0) -> Path:
    """Render a real wav: `silent_head` seconds of silence then a sine tone."""
    from app.config import settings

    ffmpeg = settings.get_ffmpeg_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not silent_head:
        subprocess.run(
            [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", f"sine=frequency=440:r=44100:duration={seconds}", str(dest)],
            check=True,
        )
        return dest
    subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={silent_head}",
         "-f", "lavfi", "-i", f"sine=frequency=440:r=44100:duration={seconds}",
         "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1", str(dest)],
        check=True,
    )
    return dest


def _seed_voice(name: str) -> Path:
    p = _voices_dir() / name
    p.write_bytes(b"RIFFfakewav")
    return p


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_taxonomy_lists_genders_and_genres(client):
    resp = client.get("/voices/taxonomy")
    assert resp.status_code == 200
    body = resp.json()
    assert {"male", "female"} <= {item["value"] for item in body["genders"]}
    assert "tien-hiep" in {item["value"] for item in body["genres"]}
    assert all(item["label"] for item in body["genders"] + body["genres"])


def test_meta_saves_gender_and_genres(client):
    _seed_voice("narrator.wav")
    resp = client.post(
        "/voices/narrator.wav/meta",
        json={"description": "Giọng nam trầm", "gender": "male",
              "genre": ["tien-hiep", "kiem-hiep"]},
    )
    assert resp.status_code == 200
    assert resp.json()["gender"] == "male"
    assert resp.json()["genre"] == ["tien-hiep", "kiem-hiep"]

    media = client.get("/api/ui/media").json()
    voice = next(v for v in media["voices"] if v["name"] == "narrator.wav")
    assert voice["gender"] == "male"
    assert voice["genre"] == ["tien-hiep", "kiem-hiep"]
    assert voice["description"] == "Giọng nam trầm"


def test_meta_deduplicates_and_clears_genres(client):
    _seed_voice("a.wav")
    resp = client.post("/voices/a.wav/meta", json={"genre": ["do-thi", "do-thi", "", "kinh-di"]})
    assert resp.json()["genre"] == ["do-thi", "kinh-di"]

    resp = client.post("/voices/a.wav/meta", json={"genre": [], "gender": ""})
    assert resp.json()["genre"] == []
    assert resp.json()["gender"] == ""


def test_meta_omitted_field_is_left_untouched(client):
    _seed_voice("b.wav")
    client.post("/voices/b.wav/meta", json={"description": "mô tả gốc", "gender": "female"})
    resp = client.post("/voices/b.wav/meta", json={"genre": ["ngon-tinh"]})
    body = resp.json()
    assert body["description"] == "mô tả gốc"
    assert body["gender"] == "female"
    assert body["genre"] == ["ngon-tinh"]


@pytest.mark.parametrize("payload", [
    {"gender": "alien"},
    {"genre": ["khong-ton-tai"]},
    {"genre": ["do-thi", "sai-be-het"]},
])
def test_meta_rejects_unknown_tags(client, payload):
    _seed_voice("c.wav")
    assert client.post("/voices/c.wav/meta", json=payload).status_code == 400


def test_meta_missing_file_404(client):
    assert client.post("/voices/nope.wav/meta", json={"gender": "male"}).status_code == 404


def test_classification_survives_rename(client):
    _seed_voice("old.wav")
    client.post("/voices/old.wav/meta", json={"gender": "male", "genre": ["lich-su"]})
    resp = client.post(
        "/voices/rename", data={"old_name": "old.wav", "new_name": "moi"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    media = client.get("/api/ui/media").json()
    voice = next(v for v in media["voices"] if v["name"] == "moi.wav")
    assert voice["gender"] == "male"
    assert voice["genre"] == ["lich-su"]


def test_legacy_description_endpoint_keeps_classification(client):
    """/description predates the tags; it must not wipe them."""
    _seed_voice("d.wav")
    client.post("/voices/d.wav/meta", json={"gender": "child", "genre": ["thieu-nhi"]})
    assert client.post("/voices/d.wav/description", json={"description": "mới"}).status_code == 200

    media = client.get("/api/ui/media").json()
    voice = next(v for v in media["voices"] if v["name"] == "d.wav")
    assert voice["description"] == "mới"
    assert voice["gender"] == "child"
    assert voice["genre"] == ["thieu-nhi"]


# ---------------------------------------------------------------------------
# Audio processing - validation
# ---------------------------------------------------------------------------


def test_process_rejects_empty_ops(client):
    _seed_voice("e.wav")
    resp = client.post("/voices/e.wav/process", json={"ops": {}})
    assert resp.status_code == 400
    assert "thao tác" in resp.json()["detail"]


@pytest.mark.parametrize("ops", [
    {"trim_start": -1},
    {"trim_start": 2, "trim_end": 1},
    {"fade_in": 999},
    {"gain_db": 60},
    {"sample_rate": 12345},
    {"trim_start": "abc"},
])
def test_process_rejects_bad_ops(client, ops):
    _seed_voice("f.wav")
    assert client.post("/voices/f.wav/process", json={"ops": ops}).status_code == 400


def test_process_missing_file_404(client):
    assert client.post("/voices/nope.wav/process", json={"ops": {"denoise": True}}).status_code == 404


def test_process_rejects_traversal(client):
    resp = client.post("/voices/..%2Fsecret.wav/process", json={"ops": {"denoise": True}})
    assert resp.status_code in (400, 404)


def test_info_missing_file_404(client):
    assert client.get("/voices/nope.wav/info").status_code == 404


def test_process_of_unreadable_file_leaves_original(client):
    """A fake wav makes ffmpeg fail; the original bytes must survive."""
    p = _seed_voice("broken.wav")
    original = p.read_bytes()
    resp = client.post("/voices/broken.wav/process", json={"ops": {"normalize": True}})
    assert resp.status_code in (400, 500)
    assert p.read_bytes() == original
    assert not any(name.startswith(".") for name in (q.name for q in _voices_dir().iterdir()))


# ---------------------------------------------------------------------------
# Audio processing - real renders
# ---------------------------------------------------------------------------


@needs_ffmpeg
def test_info_reports_duration(client):
    _make_wav(_voices_dir() / "g.wav", seconds=3.0)
    body = client.get("/voices/g.wav/info").json()
    assert body["duration_sec"] == pytest.approx(3.0, abs=0.1)
    assert body["sample_rate"] == 44100
    assert body["size"] > 0


@needs_ffmpeg
def test_process_trim_overwrites_in_place(client):
    p = _make_wav(_voices_dir() / "h.wav", seconds=4.0)
    resp = client.post("/voices/h.wav/process", json={"ops": {"trim_start": 1.0, "trim_end": 2.0}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "h.wav"
    assert body["duration_sec"] == pytest.approx(1.0, abs=0.1)
    assert p.exists()
    # Only the one file - an in-place edit must not litter the library.
    assert [q.name for q in _voices_dir().iterdir()] == ["h.wav"]


@needs_ffmpeg
def test_process_save_as_copy_keeps_original_and_inherits_tags(client):
    p = _make_wav(_voices_dir() / "i.wav", seconds=4.0)
    client.post("/voices/i.wav/meta", json={"gender": "female", "genre": ["ngon-tinh"]})

    resp = client.post(
        "/voices/i.wav/process",
        json={"ops": {"trim_start": 0.5, "trim_end": 1.5}, "save_as": "copy"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "i_edited.wav"
    assert body["gender"] == "female"
    assert body["genre"] == ["ngon-tinh"]
    assert body["duration_sec"] == pytest.approx(1.0, abs=0.1)

    from app import audio_process

    assert audio_process.probe(p)["duration_sec"] == pytest.approx(4.0, abs=0.1)


@needs_ffmpeg
def test_process_copy_uniquifies_name(client):
    _make_wav(_voices_dir() / "j.wav", seconds=2.0)
    first = client.post("/voices/j.wav/process", json={"ops": {"normalize": True}, "save_as": "copy"})
    second = client.post("/voices/j.wav/process", json={"ops": {"normalize": True}, "save_as": "copy"})
    assert first.json()["name"] == "j_edited.wav"
    assert second.json()["name"] == "j_edited_1.wav"


@needs_ffmpeg
def test_process_copy_honours_requested_name(client):
    _make_wav(_voices_dir() / "k.wav", seconds=2.0)
    resp = client.post(
        "/voices/k.wav/process",
        json={"ops": {"normalize": True}, "save_as": "copy", "new_name": "giong ke chuyen"},
    )
    assert resp.json()["name"] == "giong ke chuyen.wav"
    assert (_voices_dir() / "giong ke chuyen.wav").exists()


@needs_ffmpeg
def test_process_trim_silence_removes_leading_silence(client):
    _make_wav(_voices_dir() / "l.wav", seconds=2.0, silent_head=2.0)
    before = client.get("/voices/l.wav/info").json()["duration_sec"]
    assert before == pytest.approx(4.0, abs=0.15)

    resp = client.post("/voices/l.wav/process", json={"ops": {"trim_silence": True}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["duration_sec"] == pytest.approx(2.0, abs=0.15)


@needs_ffmpeg
def test_normalize_keeps_source_sample_rate(client):
    """loudnorm runs at 192 kHz internally; that must not leak into the output."""
    _make_wav(_voices_dir() / "o.wav", seconds=2.0)
    resp = client.post("/voices/o.wav/process", json={"ops": {"normalize": True}})
    assert resp.status_code == 200, resp.text
    assert resp.json()["sample_rate"] == 44100


@needs_ffmpeg
def test_normalize_still_honours_explicit_sample_rate(client):
    _make_wav(_voices_dir() / "p.wav", seconds=2.0)
    resp = client.post(
        "/voices/p.wav/process", json={"ops": {"normalize": True, "sample_rate": 24000}}
    )
    assert resp.json()["sample_rate"] == 24000


def test_normalize_pins_rate_only_when_needed():
    """The -ar pin is a loudnorm workaround, not a blanket resample."""
    from app import audio_process

    src, dest = Path("a.wav"), Path("b.wav")
    plain = audio_process.build_command(src, dest, audio_process.parse_ops({"denoise": True}), 44100)
    assert "-ar" not in plain

    normalized = audio_process.build_command(
        src, dest, audio_process.parse_ops({"normalize": True}), 44100
    )
    assert normalized[normalized.index("-ar") + 1] == "44100"


@needs_ffmpeg
def test_process_reports_applied_steps(client):
    _make_wav(_voices_dir() / "m.wav", seconds=2.0)
    resp = client.post(
        "/voices/m.wav/process",
        json={"ops": {"denoise": True, "normalize": True, "mono": True, "gain_db": 1.5}},
    )
    applied = resp.json()["applied"]
    assert len(applied) == 4
    assert any("nhiễu" in step for step in applied)


@needs_ffmpeg
def test_process_keeps_book_reference_working(client):
    """Overwriting must not move the file - books point at the absolute path."""
    p = _make_wav(_voices_dir() / "n.wav", seconds=3.0)
    db = _db()
    now = "2026-01-01T00:00:00+00:00"
    db.execute(
        "INSERT INTO book (title, original_filename, epub_path, patch_size, status, "
        "voice_clip_path, created_at, updated_at) "
        "VALUES ('b', 'b.epub', 'b.epub', 10, 'ready', ?, ?, ?)",
        (str(p), now, now),
    )
    db.commit()
    db.close()

    assert client.post("/voices/n.wav/process", json={"ops": {"normalize": True}}).status_code == 200

    db = _db()
    row = db.execute("SELECT voice_clip_path FROM book").fetchone()
    db.close()
    assert row["voice_clip_path"] == str(p)
    assert Path(row["voice_clip_path"]).exists()
