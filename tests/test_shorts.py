"""Short Video Studio: AI parse, validate dọc, render mock, upload mock 3 kênh."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import ai_content, db, shorts_repository
from app.video_config import SHORT_DEFAULTS, validate_short_config


@pytest.fixture()
def conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    now = datetime.now(timezone.utc).isoformat()
    cur = c.execute(
        """INSERT INTO book (title,original_filename,epub_path,patch_size,status,created_at,updated_at)
           VALUES ('Truyen Test','b.epub','b.epub',10,'ready',?,?)""",
        (now, now),
    )
    book_id = cur.lastrowid
    c.execute(
        "INSERT INTO chapter (book_id, chapter_index, title, text, char_count) VALUES (?, 0, 'Chuong 1', ?, 100)",
        (book_id, "Ngay xua co mot nguoi " * 20),
    )
    c.commit()
    yield c
    c.close()


def test_short_script_task_registered_and_parses():
    task = ai_content.get_task("short_script")
    assert task.name == "short_script"
    payload = json.dumps({
        "script": "Ngay xua " * 60,
        "caption": "Nghe truyen hay",
        "hashtags": "sach noi, short",
        "story_hook": "Bi mat dong troi",
    })
    parsed = ai_content.parse_short_script(payload)
    assert parsed["script"].startswith("Ngay xua")
    assert parsed["hashtags"] == "sach noi, short"
    assert parsed["story_hook"] == "Bi mat dong troi"


def test_short_script_rejects_empty_or_absurd():
    with pytest.raises(ai_content.GenerationError):
        ai_content.parse_short_script(json.dumps({"caption": "x"}))
    with pytest.raises(ai_content.GenerationError):
        ai_content.parse_short_script(json.dumps({"script": "ngan"}))


def test_validate_short_defaults_vertical():
    cfg = validate_short_config({})
    assert cfg["resolution"] == "1080x1920"
    assert cfg["fps"] == 30
    assert cfg["subtitle_enabled"] is True
    assert SHORT_DEFAULTS["resolution"] == "1080x1920"


def test_validate_short_rejects_landscape_only_choice():
    with pytest.raises(ValueError):
        validate_short_config({"resolution": "1280x720"})
    for ok in ("1080x1920", "1080x1080", "1920x1080"):
        assert validate_short_config({"resolution": ok})["resolution"] == ok


def test_shorts_crud_and_uploads_idempotent(conn):
    book_id = conn.execute("SELECT id FROM book LIMIT 1").fetchone()["id"]
    short = shorts_repository.create_short(conn, book_id=book_id,
                                           script_text="hello " * 60,
                                           script_source="manual")
    assert short.status == "draft"
    assert short.resolution == "1080x1920"
    uploads = shorts_repository.ensure_uploads(conn, short.id)
    assert {u.platform for u in uploads} == {"fb", "tiktok", "youtube"}
    # idempotent
    again = shorts_repository.ensure_uploads(conn, short.id)
    assert len(again) == 3
    updated = shorts_repository.update_short(conn, short.id, caption="cap",
                                             story_link="https://x/y")
    assert updated.caption == "cap"
    row = shorts_repository.set_upload_status(conn, short.id, "fb", "done",
                                              platform_video_id="vid1")
    assert row.status == "done" and row.platform_video_id == "vid1"


def test_short_render_handler_mocks_tts_and_video(conn, monkeypatch, tmp_path):
    from app.jobqueue.handlers import short_render
    from app import repository as _repo

    book_id = conn.execute("SELECT id FROM book LIMIT 1").fetchone()["id"]
    short = shorts_repository.create_short(conn, book_id=book_id,
                                           script_text="kinh ban " * 60)
    fake_audio = tmp_path / "n.wav"
    fake_audio.write_bytes(b"RIFF....")
    fake_cover = tmp_path / "c.jpg"
    fake_cover.write_bytes(b"\xff\xd8\xff")

    class FakeEngine:
        sample_rate = 24000

        def synthesize_chunk(self, text, reference_wav_path=None, prompt_text=None):
            import numpy as np
            return np.zeros(240, dtype="float32")

    monkeypatch.setattr("app.tts_engine.create_tts_engine",
                        lambda engine_id, **k: FakeEngine())
    monkeypatch.setattr(_repo.get_book(conn, book_id), "cover_image_path",
                        str(fake_cover), raising=False)
    monkeypatch.setattr(short_render.video_gen, "generate_standalone_video",
                        lambda *a, **k: Path(a[2]).write_bytes(b"MP4"))

    class FakeResult:
        valid = True
        error_code = None
        message = ""

    monkeypatch.setattr(short_render, "validate_video",
                        lambda *a, **k: FakeResult())

    class Ctx:
        def __init__(self):
            self.conn = conn
            self.job = type("J", (), {"payload": {"short_id": short.id}})()
        def progress(self, *a, **k):
            pass

    result = short_render.handle(Ctx())
    assert result["video_path"].endswith(".mp4")
    assert shorts_repository.get_short(conn, short.id).status == "ready"


def test_build_vertical_cues_covers_full_duration():
    from app.short_cues import build_vertical_cues
    text = ("Câu một. Câu hai ngắn. " + "Câu ba rất dài " * 6 + ". Câu bốn.")
    cues = build_vertical_cues(text, 60.0)
    assert cues[0]["start"] == 0.0
    # Cue cuối chốt đúng cuối audio để không hụt đuôi.
    assert cues[-1]["end"] == 60.0
    assert all(cue["end"] > cue["start"] for cue in cues)
    assert all(cue["words"] for cue in cues)
    joined = " ".join(cue["text"] for cue in cues)
    assert "Câu bốn." in joined


def test_build_vertical_cues_rejects_bad_duration():
    from app.short_cues import build_vertical_cues
    with pytest.raises(ValueError):
        build_vertical_cues("Câu một.", 0)


def test_build_vertical_cues_empty_script():
    from app.short_cues import build_vertical_cues
    assert build_vertical_cues("   ", 30.0) == []


def test_remotion_props_shape():
    from app import short_remotion
    props = short_remotion.build_props(
        audio_path="narration.wav", cover_path="cover.jpg",
        script_text="Một. Hai. Ba.", duration_seconds=12.0,
        resolution="1080x1920", fps=30, music_path=None,
        book_title="Truyen Hay", hook_text="Hook")
    assert props["width"] == 1080 and props["height"] == 1920
    assert props["durationInFrames"] == 360
    assert len(props["cues"]) == 3
    # Media phải là tên file phẳng (Chrome Remotion chặn file://).
    assert props["audioSrc"] == "narration.wav"
    assert "\\" not in props["audioSrc"] and props["musicSrc"] == ""
    assert props["bookTitle"] == "Truyen Hay"


def test_stage_assets_copies_media_to_flat_dir(tmp_path):
    from app import short_remotion
    audio = tmp_path / "narration.wav"
    audio.write_bytes(b"RIFF")
    cover = tmp_path / "cover.png"
    cover.write_bytes(b"\x89PNG")
    stage = tmp_path / "assets"
    staged = short_remotion.stage_assets(stage, audio_path=str(audio),
                                         cover_path=str(cover))
    assert (stage / "narration.wav").is_file()
    # Giữ đuôi gốc để Remotion đoán mimetype.
    assert (stage / "cover.png").is_file()
    assert staged == {"narration": "narration.wav", "cover": "cover.png"}


def test_stage_assets_missing_media_raises(tmp_path):
    from app import short_remotion
    with pytest.raises(FileNotFoundError):
        short_remotion.stage_assets(tmp_path / "assets",
                                    audio_path=str(tmp_path / "khong.wav"),
                                    cover_path=None)


def test_short_render_remotion_branch(monkeypatch, tmp_path):
    """Nhánh remotion: gọi short_remotion.render, không gọi ffmpeg video."""
    from app.jobqueue.handlers import short_render

    calls: dict = {}

    class FakeResult:
        valid = True
        error_code = None
        message = ""

    def fake_synth(*a, **kwargs):
        Path(kwargs["out_path"]).write_bytes(b"RIFF....")
        return 24000

    monkeypatch.setattr(short_render, "_synthesize_narration", fake_synth)
    monkeypatch.setattr(short_render, "validate_video",
                        lambda *a, **k: FakeResult())

    def fake_probe(audio_path):
        calls["probed"] = audio_path
        return 15.0

    def fake_render(props, out_path, *, stage_dir=None, **k):
        calls["props"] = props
        calls["out"] = out_path
        calls["stage_dir"] = stage_dir
        Path(out_path).write_bytes(b"MP4")

    from app import short_remotion, video_gen
    monkeypatch.setattr(short_remotion, "probe_audio_duration", fake_probe)
    monkeypatch.setattr(short_remotion, "stage_assets",
                        lambda stage_dir, **k: {"narration": "narration.wav",
                                               "cover": "cover.jpg"})
    monkeypatch.setattr(short_remotion, "render", fake_render)
    monkeypatch.setattr(video_gen, "generate_standalone_video",
                        lambda *a, **k: pytest.fail("ffmpeg không được gọi ở nhánh remotion"))

    monkeypatch.setattr(short_render.settings, "data_root", str(tmp_path))
    import app.repository as repo

    conn = db.connect(":memory:")
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO book (title,original_filename,epub_path,patch_size,status,created_at,updated_at)
           VALUES ('T','b.epub','b.epub',10,'ready',?,?)""", (now, now))
    short = shorts_repository.create_short(conn, book_id=cur.lastrowid,
                                           script_text="Một. Hai. Ba. Bốn.",
                                           renderer="remotion")

    class Ctx:
        def __init__(self):
            self.conn = conn
            self.job = type("J", (), {"payload": {"short_id": short.id}})()
        def progress(self, *a, **k):
            pass

    result = short_render.handle(Ctx())
    assert result["renderer"] == "remotion"
    assert calls["props"]["durationInFrames"] == 450
    assert calls["props"]["audioSrc"] == "narration.wav"
    assert calls["stage_dir"] is not None
    assert len(calls["props"]["cues"]) >= 3
    assert shorts_repository.get_short(conn, short.id).status == "ready"
    conn.close()


def test_short_upload_handler_mocks_three_platforms(conn, monkeypatch, tmp_path):
    from app.jobqueue.handlers import short_upload

    book_id = conn.execute("SELECT id FROM book LIMIT 1").fetchone()["id"]
    short = shorts_repository.create_short(conn, book_id=book_id,
                                           script_text="x " * 60)
    vid = tmp_path / "s.mp4"
    vid.write_bytes(b"MP4")
    shorts_repository.update_short(conn, short.id, video_path=str(vid),
                                   caption="cap", story_link="https://x")
    shorts_repository.ensure_uploads(conn, short.id)

    monkeypatch.setattr("app.facebook.publish_short_video", lambda *a, **k: "fb1")
    monkeypatch.setattr("app.tiktok.publish_short_video", lambda *a, **k: "tt1")
    import app.youtube as _yt
    monkeypatch.setattr(_yt, "enqueue_upload", lambda *a, **k: 99)
    monkeypatch.setattr(_yt, "process_upload",
                        lambda *a, **k: {"status": "done", "youtube_video_id": "yt1"})

    class Ctx:
        def __init__(self, platform):
            self.conn = conn
            self.job = type("J", (), {"payload": {"short_id": short.id,
                                                 "platform": platform}})()
        def progress(self, *a, **k):
            pass

    for platform, expect in (("fb", "fb1"), ("tiktok", "tt1"), ("youtube", "yt1")):
        out = short_upload.handle(Ctx(platform))
        assert out["platform_video_id"] == expect
    assert shorts_repository.get_short(conn, short.id).status == "published"


def test_db_migrate_keeps_old_db(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.executescript("CREATE TABLE book (id INTEGER PRIMARY KEY, title TEXT);")
    c.commit()
    c.close()
    conn2 = db.connect(path)
    db.init_schema(conn2)
    tables = {r[0] for r in
              conn2.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"shorts", "short_uploads", "facebook_credentials",
            "tiktok_credentials"} <= tables
    conn2.close()
