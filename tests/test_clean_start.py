"""Khởi động sạch: restart phải dọn hàng đợi và không tự đẻ job mới.

Các test dưới chạy qua lifespan thật (TestClient) chứ không gọi thẳng helper —
điểm dễ hỏng nằm ở chỗ lifespan nhánh nào, chứ không ở bản thân store.clear_all.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import db
from app.jobqueue import store
from app.main import app


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _seed(db_path) -> None:
    """DB của một lần chạy trước bị giết giữa chừng: book_job pending, upload pending,
    và hàng đợi còn job kẹt ở running/pending."""
    conn = db.connect(str(db_path))
    db.init_schema(conn)
    now = _now()
    conn.execute(
        """INSERT INTO book (id, title, original_filename, epub_path, patch_size,
                              status, created_at, updated_at)
           VALUES (1, 'Book', 'a.epub', '/tmp/a.epub', 10, 'ready', ?, ?)""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO book_job (id, book_id, job_type, status, attempt_count,
                                 created_at, updated_at)
           VALUES (7, 1, 'video', 'pending', 0, ?, ?)""",
        (now, now),
    )
    conn.execute(
        """INSERT INTO youtube_uploads (id, video_path, title, description, tags,
                                        privacy_status, status, created_at)
           VALUES (3, '/tmp/a.mp4', 'T', 'D', '', 'private', 'pending', ?)""",
        (now,),
    )
    conn.commit()
    store.enqueue(conn, "video", payload={"book_job_id": 7}, book_id=1,
                  dedupe_key="video:book_job=7")
    stuck = store.enqueue(conn, "youtube_upload", payload={"upload_id": 3},
                          dedupe_key="youtube_upload:upload=3")
    conn.execute("UPDATE job SET status='running' WHERE id=?", (stuck,))
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "db_path", str(tmp_path / "app.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", False)
    monkeypatch.setattr(settings, "reset_all_jobs_on_startup", False)
    _seed(tmp_path / "app.db")
    return settings


def _jobs_after_boot(db_path) -> list:
    conn = db.connect(str(db_path))
    try:
        return store.list_jobs(conn)
    finally:
        conn.close()


def test_clean_start_empties_the_queue_and_enqueues_nothing(env, tmp_path, monkeypatch):
    monkeypatch.setattr(env, "clean_start_on_startup", True)

    with TestClient(app):
        pass

    assert _jobs_after_boot(tmp_path / "app.db") == []


def test_clean_start_cancels_producers_so_a_second_boot_stays_empty(env, tmp_path, monkeypatch):
    """Xoá job mà để nguyên book_job/upload ở 'pending' thì lần boot sau backfill sẽ
    dựng lại đúng việc vừa dọn — đó mới là cái bug người dùng thấy."""
    monkeypatch.setattr(env, "clean_start_on_startup", True)

    with TestClient(app):
        pass

    conn = db.connect(str(tmp_path / "app.db"))
    try:
        assert conn.execute("SELECT status FROM book_job WHERE id=7").fetchone()[0] == "cancelled"
        assert conn.execute(
            "SELECT status FROM youtube_uploads WHERE id=3"
        ).fetchone()[0] == "cancelled"
    finally:
        conn.close()

    # Bật lại nhánh cũ: không còn hàng 'pending' nào để backfill nhặt.
    monkeypatch.setattr(env, "clean_start_on_startup", False)
    with TestClient(app):
        pass
    assert _jobs_after_boot(tmp_path / "app.db") == []


def test_clean_start_off_still_picks_the_work_back_up(env, tmp_path, monkeypatch):
    """Cờ tắt phải giữ nguyên hành vi cũ, nếu không test trên chỉ đang xác nhận
    một app không bao giờ enqueue được gì."""
    monkeypatch.setattr(env, "clean_start_on_startup", False)

    with TestClient(app):
        pass

    types = sorted(j.job_type for j in _jobs_after_boot(tmp_path / "app.db"))
    assert "video" in types and "youtube_upload" in types


def test_clean_start_leaves_patch_audio_and_files_alone(env, tmp_path, monkeypatch):
    """Dọn hàng đợi khác với reset-all: không được đụng vào file đã render."""
    monkeypatch.setattr(env, "clean_start_on_startup", True)
    conn = db.connect(str(tmp_path / "app.db"))
    now = _now()
    audio = tmp_path / "done.wav"
    audio.write_bytes(b"RIFF")
    conn.execute(
        """INSERT INTO patch (id, book_id, patch_index, chapter_start, chapter_end,
                              status, audio_path, attempt_count, created_at, updated_at)
           VALUES (11, 1, 0, 0, 0, 'done', ?, 0, ?, ?)""",
        (str(audio), now, now),
    )
    conn.commit()
    conn.close()

    with TestClient(app):
        pass

    assert audio.exists()
    conn = db.connect(str(tmp_path / "app.db"))
    try:
        row = conn.execute("SELECT status, audio_path FROM patch WHERE id=11").fetchone()
        assert row["status"] == "done"
        assert row["audio_path"] == str(audio)
    finally:
        conn.close()
