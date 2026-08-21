import json
from datetime import datetime, timezone
from pathlib import Path

from app import db, youtube
from app.config import settings
from app.jobqueue import store
from app.jobqueue.context import JobContext
from app.jobqueue.handlers import patch_video
from app.jobqueue.joblog import JobLogger
from app.video_integrity import ValidationFacts, ValidationResult


def test_manual_patch_job_renders_without_pipeline(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "manual.db")); db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat(); audio = tmp_path / "a.wav"; audio.write_bytes(b"a")
    image = tmp_path / "bg.jpg"; image.write_bytes(b"i")
    monkeypatch.setattr(settings, "data_root", str(tmp_path)); monkeypatch.setattr(settings, "default_background_image", str(image))
    conn.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,video_resolution,video_fps,created_at,updated_at) VALUES (1,'B','b','b',1,'done','1280x720',24,?,?)", (now, now))
    conn.execute("INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,audio_path,created_at,updated_at) VALUES (2,1,0,0,0,'done',?,?,?)", (str(audio), now, now)); conn.commit()
    seen = {}
    monkeypatch.setattr(patch_video.image_overlay, "ensure_patch_overlay", lambda *a, **k: str(image))
    monkeypatch.setattr(patch_video.video_gen, "generate_segment", lambda a,b,out,**kw: (seen.update(a=a,b=b,out=out,kw=kw), Path(out).write_bytes(b"new")))
    monkeypatch.setattr(patch_video, "validate_video", lambda p, **kw: ValidationResult(True,None,"",(),ValidationFacts(),0))
    job_id = store.enqueue(conn, "patch_video", payload={"patch_id": 2}, book_id=1); job = store.claim(conn, "patch_video", "w")
    result = patch_video.handle(JobContext(job, conn, JobLogger(job_id, "patch_video"), lambda: False))
    output = tmp_path / "books" / "1" / "patch_videos" / "2.mp4"
    assert result["output_path"] == str(output)
    assert output.read_bytes() == b"new"
    assert seen["out"] != str(output)
    assert seen["kw"]["resolution"] == (1280, 720)
    # Nhạc nền chỉ chèn vào khoảng lặng: config đi kèm lệnh render, không phải
    # đọc lại lúc mux.
    from app import music_bed

    assert music_bed.parse_options(seen["kw"]["music_gaps"]).enabled is True
    assert conn.execute("SELECT patch_id FROM videos WHERE file_path=?", (str(output),)).fetchone()[0] == 2
    assert conn.execute("SELECT phase FROM job WHERE id=?", (job_id,)).fetchone()[0] == "done"
    # Badge "Video" ở bảng Patches đọc patch_pipeline: render xong là hiện ngay, không
    # phải đợi tới lúc bấm upload YouTube.
    pipeline = conn.execute("SELECT stage, video_status, video_path FROM patch_pipeline WHERE patch_id=2").fetchone()
    assert (pipeline["stage"], pipeline["video_status"], pipeline["video_path"]) == ("upload", "done", str(output))


def test_manual_patch_job_ignores_stale_pipeline_snapshot(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "manual-pipeline.db")); db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    audio = tmp_path / "a.wav"; audio.write_bytes(b"a")
    current = tmp_path / "current.jpg"; current.write_bytes(b"current")
    stale = tmp_path / "stale.jpg"; stale.write_bytes(b"stale")
    monkeypatch.setattr(settings, "data_root", str(tmp_path)); monkeypatch.setattr(settings, "default_background_image", str(current))
    config = json.dumps({"video": {"codec": "h264_nvenc", "quality": 19, "audio_bitrate": "256k"}})
    conn.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,video_resolution,video_fps,automation_config,created_at,updated_at) VALUES (1,'B','b','b',1,'done','854x480',60,?,?,?)", (config, now, now))
    conn.execute("INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,audio_path,created_at,updated_at) VALUES (2,1,0,0,0,'done',?,?,?)", (str(audio), now, now))
    conn.execute("INSERT INTO patch_pipeline (patch_id,stage,thumbnail_status,video_status,upload_status,playlist_status,thumbnail_path,config_snapshot,media_snapshot,created_at,updated_at) VALUES (2,'published','done','done','done','done',?,'{}','{}',?,?)", (str(stale), now, now)); conn.commit()
    seen = {}
    monkeypatch.setattr(patch_video.image_overlay, "ensure_patch_overlay", lambda *a, **k: str(current))
    monkeypatch.setattr(patch_video.video_gen, "generate_segment", lambda a,b,out,**kw: (seen.update(image=a, kw=kw), Path(out).write_bytes(b"new")))
    monkeypatch.setattr(patch_video, "validate_video", lambda p, **kw: ValidationResult(True,None,"",(),ValidationFacts(),0))
    job_id = store.enqueue(conn, "patch_video", payload={"patch_id": 2}, book_id=1); job = store.claim(conn, "patch_video", "w")
    patch_video.handle(JobContext(job, conn, JobLogger(job_id, "patch_video"), lambda: False))
    assert seen["image"] == str(current)
    assert seen["kw"]["resolution"] == (854, 480)
    assert seen["kw"]["fps"] == 60
    assert seen["kw"]["codec"] == "h264_nvenc"
    assert seen["kw"]["quality"] == 19
    assert seen["kw"]["audio_bitrate"] == "256k"
    assert conn.execute("SELECT stage FROM patch_pipeline WHERE patch_id=2").fetchone()[0] == "published"


def test_patch_recovery_renders_atomically_and_resumes_upload(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "a.db")); db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat(); audio = tmp_path / "a.wav"; audio.write_bytes(b"a")
    thumb = tmp_path / "thumb.png"; thumb.write_bytes(b"i"); output = tmp_path / "patch.mp4"
    conn.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,video_resolution,video_fps,created_at,updated_at) VALUES (1,'B','b','b',1,'done','1280x720',24,?,?)", (now, now))
    conn.execute("INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,audio_path,created_at,updated_at) VALUES (2,1,0,0,0,'done',?,?,?)", (str(audio), now, now))
    conn.execute("INSERT INTO patch_pipeline (patch_id,stage,thumbnail_status,video_status,upload_status,playlist_status,thumbnail_path,video_path,config_snapshot,media_snapshot,created_at,updated_at) VALUES (2,'video','done','rerendering','waiting_for_rerender','pending',?,?,?, '{}',?,?)", (str(thumb), str(output), json.dumps({}), now, now)); conn.commit()
    upload_id = youtube.enqueue_upload(conn, str(output), "T", render_source_type="patch", render_source_id=2)
    conn.execute("UPDATE youtube_uploads SET status='waiting_for_rerender',validation_status='waiting_for_rerender',integrity_retry_count=1 WHERE id=?", (upload_id,)); conn.commit()
    seen = {}
    monkeypatch.setattr(patch_video.video_gen, "generate_standalone_video", lambda audio, image, out, **kw: (seen.update(out=out, kwargs=kw), Path(out).write_bytes(b"new")))
    monkeypatch.setattr(patch_video, "validate_video", lambda p, **kw: ValidationResult(True, None, "", (), ValidationFacts(), 0))
    job_id = store.enqueue(conn, "patch_video", payload={"patch_id": 2, "recovery_upload_id": upload_id})
    job = store.claim(conn, "patch_video", "w")
    result = patch_video.handle(JobContext(job, conn, JobLogger(job_id, "patch_video"), lambda: False))
    assert result["output_path"] == str(output)
    assert seen["out"] != str(output)
    assert output.read_bytes() == b"new"
    assert seen["kwargs"]["resolution"] == "1280x720"
    assert seen["kwargs"]["fps"] == 24
    assert conn.execute("SELECT status FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()[0] == "pending"


def test_patch_recovery_uses_full_frozen_render_snapshot(tmp_path, monkeypatch):
    conn = db.connect(str(tmp_path / "b.db")); db.init_schema(conn); now = datetime.now(timezone.utc).isoformat()
    files = {}
    for name in ("a.wav", "thumb.png", "music.mp3", "intro.wav", "outro.wav"):
        files[name] = tmp_path / name; files[name].write_bytes(b"x")
    output = tmp_path / "v.mp4"
    frozen = {"resolution":"1280x720","fps":24,"image_type":"zoom-in","codec":"h264_nvenc","crf":19,"audio_bitrate":"256k","music_path":str(files["music.mp3"]),"music_volume":0.22,"intro_audio":str(files["intro.wav"]),"outro_audio":str(files["outro.wav"])}
    conn.execute("INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,video_resolution,video_fps,created_at,updated_at) VALUES (1,'B','b','b',1,'done','1920x1080',30,?,?)", (now, now))
    conn.execute("INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,audio_path,created_at,updated_at) VALUES (2,1,0,0,0,'done',?,?,?)", (str(files["a.wav"]), now, now))
    media = {"render_config": frozen}
    conn.execute("INSERT INTO patch_pipeline (patch_id,stage,thumbnail_status,video_status,upload_status,playlist_status,thumbnail_path,video_path,config_snapshot,media_snapshot,created_at,updated_at) VALUES (2,'video','done','rerendering','waiting_for_rerender','pending',?,?, '{}',?,?,?)", (str(files["thumb.png"]), str(output), json.dumps(media), now, now)); conn.commit()
    upload_id = youtube.enqueue_upload(conn, str(output), "T", render_source_type="patch", render_source_id=2)
    conn.execute("UPDATE youtube_uploads SET status='waiting_for_rerender',validation_status='waiting_for_rerender' WHERE id=?", (upload_id,)); conn.commit(); seen = {}
    monkeypatch.setattr(patch_video.video_gen, "generate_standalone_video", lambda a,b,out,**kw: (seen.update(a=a,b=b,out=out,kw=kw), Path(out).write_bytes(b"new")))
    monkeypatch.setattr(patch_video, "validate_video", lambda p, **kw: ValidationResult(True,None,"",(),ValidationFacts(),0))
    job_id = store.enqueue(conn,"patch_video",payload={"patch_id":2,"recovery_upload_id":upload_id}); job=store.claim(conn,"patch_video","w")
    patch_video.handle(JobContext(job,conn,JobLogger(job_id,"patch_video"),lambda:False))
    assert seen["kw"] == frozen
    assert seen["kw"]["codec"] == "h264_nvenc"
