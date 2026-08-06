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


def test_episode_number_matches_youtube_metadata_numbering(client, seeded_book):
    """The Tập column must equal patch_index + 1, the same value
    youtube_metadata.py substitutes into the published title."""
    _add_patch(1, 4)
    html = client.get("/books/1").text
    assert '<td class="patch-stt-cell">5</td>' in html
    assert 'data-episode="5"' in html


def test_header_column_count_matches_body_cells(client, seeded_book):
    _add_patch(1, 0)
    parser = _PatchTable()
    parser.feed(client.get("/books/1").text)
    assert parser.headers == parser.body_cells
    assert parser.headers == 10


@pytest.mark.parametrize(
    "stage,last_error,expected",
    [
        (None, None, "none"),
        ("published", None, "published"),
        ("auth_required", None, "error"),
        ("upload", "boom", "error"),
        ("upload", None, "uploading"),
        ("thumbnail", None, "uploading"),
    ],
)
def test_row_exposes_pipeline_filter_group(client, seeded_book, stage, last_error, expected):
    _add_patch(1, 0, stage=stage, last_error=last_error)
    html = client.get("/books/1").text
    row = re.search(r"<tr data-patch-status=.*?>", html, re.S).group(0)
    assert f'data-pipeline="{expected}"' in row


def test_row_exposes_patch_status_for_filtering(client, seeded_book):
    _add_patch(1, 0, status="failed")
    html = client.get("/books/1").text
    assert 'data-patch-status="failed"' in html


def test_filter_controls_render(client, seeded_book):
    _add_patch(1, 0)
    html = client.get("/books/1").text
    for control in ("patch-search-input", "patch-status-filter", "patch-pipeline-filter",
                    "patch-has-video", "patch-has-audio", "patch-has-yt",
                    "patch-filter-reset"):
        assert f'id="{control}"' in html
    # Filter changes must reset to page 1, not strand the user on a stale page.
    assert 'onchange="filterAndPaginate(true)"' in html
    assert 'oninput="filterAndPaginate(true)"' in html


def test_filters_are_anded_and_persisted(client, seeded_book):
    _add_patch(1, 0)
    html = client.get("/books/1").text
    assert "row.dataset.patchStatus === status" in html
    assert "row.dataset.pipeline === pipelineStage" in html
    assert "row.dataset.episode === query" in html
    assert "flagMatch(hasVideo, !!videoCell && videoCell.dataset.hasVideo === '1')" in html
    assert "flagMatch(hasAudio, !!videoCell && videoCell.dataset.audioReady === '1')" in html
    assert "flagMatch(hasYt, row.dataset.hasYtUpload === '1')" in html


def test_every_filter_is_wired_into_save_restore_and_reset(client, seeded_book):
    """PATCH_FILTER_IDS is the single map the three paths share, so a filter
    missing from it silently loses persistence or survives 'Xóa lọc'."""
    _add_patch(1, 0)
    html = client.get("/books/1").text
    mapping = re.search(r"const PATCH_FILTER_IDS = \{(.*?)\};", html, re.S).group(1)
    for control in ("patch-search-input", "patch-status-filter", "patch-pipeline-filter",
                    "patch-has-video", "patch-has-audio", "patch-has-yt"):
        assert f"'{control}'" in mapping


def test_youtube_upload_flag_uses_real_video_id_not_stage(client, seeded_book):
    """stage='published' with no upload behind it must not count as uploaded."""
    _add_patch(1, 0, stage="published")                                  # no upload row
    _add_patch(2, 1, stage="published", youtube_video_id="rTdPVAoher8")  # real video
    html = client.get("/books/1").text
    rows = re.findall(r"<tr data-patch-status=.*?>", html, re.S)
    assert len(rows) == 2
    assert 'data-has-yt-upload="0"' in rows[0]
    assert 'data-has-yt-upload="1"' in rows[1]


def test_upload_row_without_video_id_is_not_uploaded(client, seeded_book):
    """An upload that was claimed but never returned an id is not on YouTube."""
    _add_patch(1, 0, stage="upload", has_upload_row=True)
    html = client.get("/books/1").text
    assert 'data-has-yt-upload="0"' in html


def _write_patch_video(patch_id):
    directory = Path(settings.data_root) / "books" / "1" / "patch_videos"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{patch_id}.mp4").write_bytes(b"mp4")


def test_existing_video_keeps_its_buttons_when_audio_is_not_ready(client, seeded_book):
    """A rendered MP4 must stay playable/deletable even after its audio is gone.

    The audio gate exists to stop you *creating* a video without TTS output. Applying it
    to the whole cell also hid preview/download/delete/YT for videos already on disk -
    reset or failed patches keep their MP4, so the row lost every button.
    """
    _add_patch(1, 0, status="failed", audio_path=None)
    _write_patch_video(1)

    html = client.get("/books/1").text
    cell = re.search(r'<td class="patch-video-cell".*?</td>', html, re.S).group(0)

    assert 'data-has-video="1"' in cell
    assert 'data-audio-ready="0"' in cell
    assert "pv-wrap" in cell, "no pv-wrap, so buildVideoCell bails and renders no buttons"


def test_cell_without_audio_or_video_still_shows_the_dash(client, seeded_book):
    """Nothing to play and nothing to render from - the muted dash is correct here."""
    _add_patch(1, 0, status="failed", audio_path=None)

    html = client.get("/books/1").text
    cell = re.search(r'<td class="patch-video-cell".*?</td>', html, re.S).group(0)

    assert 'data-has-video="0"' in cell
    assert "pv-muted" in cell
    assert "pv-wrap" not in cell
