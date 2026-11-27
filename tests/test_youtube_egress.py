"""YouTube khi server egress (proxy scope youtube) chết: process_upload không trả
"failed" lặng lẽ mà raise EgressUnavailable để runner hoãn job."""
from __future__ import annotations

import pytest

from app import db, egress, youtube


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_schema(connection)
    yield connection
    connection.close()


def test_process_upload_raises_egress_unavailable_when_proxy_is_down(conn, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 16)
    monkeypatch.setattr(youtube, "_require_google_imports", lambda: None)
    monkeypatch.setattr(youtube, "MediaFileUpload", lambda *a, **kw: object(), raising=False)
    upload_id = youtube.enqueue_upload(conn, str(video), "Title")

    def down(c):
        raise egress.EgressUnavailable("server 2 chết")

    monkeypatch.setattr(youtube, "get_youtube_service", down)
    with pytest.raises(egress.EgressUnavailable):
        youtube.process_upload(conn, upload_id)
    row = conn.execute("SELECT status FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()
    assert row["status"] == "failed"      # lần sau claim lại được


def test_process_upload_still_returns_failed_for_ordinary_errors(conn, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"\x00" * 16)
    monkeypatch.setattr(youtube, "_require_google_imports", lambda: None)
    upload_id = youtube.enqueue_upload(conn, str(video), "Title")
    monkeypatch.setattr(youtube, "get_youtube_service", lambda c: (_ for _ in ()).throw(ValueError("bad")))
    assert youtube.process_upload(conn, upload_id)["status"] == "failed"
