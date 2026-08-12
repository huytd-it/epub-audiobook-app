"""Patch table episode column + status/YouTube/search filters."""
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def seeded_book(tmp_path):
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO book (id,title,original_filename,epub_path,patch_size,status,
           created_at,updated_at) VALUES (1,'Book','book.epub','/tmp/book.epub',10,'done',?,?)""",
        (now, now),
    )
    conn.commit()
    conn.close()
    return type("BookRef", (), {"id": 1})()


def _add_patch(patch_id, patch_index, status="done", stage=None, last_error=None,
               youtube_video_id=None, has_upload_row=False, audio_path="/tmp/a.wav"):
    conn = db.connect(settings.db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO patch (id,book_id,patch_index,chapter_start,chapter_end,status,
           audio_path,created_at,updated_at) VALUES (?,1,?,0,1,?,?,?,?)""",
        (patch_id, patch_index, status, audio_path, now, now),
    )
    upload_id = None
    if has_upload_row or youtube_video_id:
        cur = conn.execute(
            """INSERT INTO youtube_uploads (video_path,youtube_video_id,status,created_at)
               VALUES ('/tmp/v.mp4',?,'done',?)""",
            (youtube_video_id, now),
        )
        upload_id = cur.lastrowid
    if stage is not None:
        conn.execute(
            """INSERT INTO patch_pipeline (patch_id,stage,last_error,youtube_upload_id,
               config_snapshot,media_snapshot,created_at,updated_at)
               VALUES (?,?,?,?,'{}','{}',?,?)""",
            (patch_id, stage, last_error, upload_id, now, now),
        )
    conn.commit()
    conn.close()


class _PatchTable(HTMLParser):
    """Collect header cells and the first body row's cells of the patch table."""

    def __init__(self):
        super().__init__()
        self.in_table = self.in_thead = self.in_tbody = False
        self.headers = 0
        self.body_cells = 0
        self._body_rows = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table" and attrs.get("data-table-key") == "patch-table":
            self.in_table = True
        elif not self.in_table:
            return
        elif tag == "thead":
            self.in_thead = True
        elif tag == "tbody":
            self.in_tbody = True
        elif tag == "tr" and self.in_tbody:
            self._body_rows += 1
        elif tag == "th" and self.in_thead:
            self.headers += 1
        elif tag == "td" and self.in_tbody and self._body_rows == 1:
            self.body_cells += 1

    def handle_endtag(self, tag):
        if tag == "thead":
            self.in_thead = False
        elif tag == "tbody":
            self.in_tbody = False
        elif tag == "table" and self.in_tbody is False and self.in_table:
            self.in_table = False




















def _write_patch_video(patch_id):
    directory = Path(settings.data_root) / "books" / "1" / "patch_videos"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{patch_id}.mp4").write_bytes(b"mp4")
