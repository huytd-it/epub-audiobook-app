import json
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, upload_worker, youtube
from app.config import settings
from app.main import app


@pytest.fixture
def db_conn(tmp_path):
    conn = db.connect(str(tmp_path / "progress.db"))
    db.init_schema(conn)
    return conn


class ChunkedUpload:
    """Mimics googleapiclient's resumable request: several chunks, then the response.

    Reads the persisted progress at the start of every chunk, so the recorded sequence
    is what a poll request would actually have seen mid-upload.
    """

    def __init__(self, fractions, conn=None, upload_id=None):
        self._fractions = list(fractions)
        self._conn, self._upload_id = conn, upload_id
        self.observed = []

    def videos(self):
        return self

    def insert(self, *args, **kwargs):
        return self

    def next_chunk(self):
        if self._conn is not None:
            self.observed.append(
                self._conn.execute(
                    "SELECT upload_progress FROM youtube_uploads WHERE id=?", (self._upload_id,)
                ).fetchone()[0]
            )
        if self._fractions:
            fraction = self._fractions.pop(0)
            return type("Status", (), {"progress": lambda self, f=fraction: f})(), None
        return None, {"id": "vid123"}


def test_upload_progress_is_persisted_for_each_chunk(db_conn, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(youtube, "MediaFileUpload", lambda *args, **kwargs: object(), raising=False)
    upload_id = youtube.enqueue_upload(db_conn, str(video), "Title")
    service = ChunkedUpload([0.25, 0.5, 0.75], db_conn, upload_id)
    monkeypatch.setattr(youtube, "get_youtube_service", lambda conn: service)

    youtube.process_upload(db_conn, upload_id)

    assert service.observed == [0, 25.0, 50.0, 75.0]
    assert db_conn.execute(
        "SELECT upload_progress FROM youtube_uploads WHERE id=?", (upload_id,)
    ).fetchone()[0] == 100


def test_upload_progress_resets_when_a_retried_upload_starts(db_conn, tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(youtube, "MediaFileUpload", lambda *args, **kwargs: object(), raising=False)
    upload_id = youtube.enqueue_upload(db_conn, str(video), "Title")
    db_conn.execute("UPDATE youtube_uploads SET upload_progress=64 WHERE id=?", (upload_id,))
    db_conn.commit()
    service = ChunkedUpload([], db_conn, upload_id)
    monkeypatch.setattr(youtube, "get_youtube_service", lambda conn: service)

    youtube.process_upload(db_conn, upload_id)

    assert service.observed == [0]


def _seed_playlist_upload(conn, playlist_status="pending"):
    metadata = {"automation": {"youtube": {"playlist_mode": "existing", "playlist_id": "PL1"}}}
    conn.execute(
        "INSERT INTO youtube_uploads (video_path, title, status, youtube_video_id, thumbnail_status, playlist_status, metadata_snapshot, created_at)"
        " VALUES ('v', 'v', 'done', 'yt', 'done', ?, ?, '2020')",
        (playlist_status, json.dumps(metadata)),
    )
    conn.commit()
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def test_playlist_status_is_processing_while_playlist_work_runs(db_conn, monkeypatch):
    upload_id = _seed_playlist_upload(db_conn)
    during = []
    monkeypatch.setattr(youtube, "get_creds_from_db", lambda conn: {"channel_id": "c"})
    monkeypatch.setattr(
        youtube, "playlist_contains_video",
        lambda conn, playlist_id, video_id: during.append(
            conn.execute("SELECT playlist_status FROM youtube_uploads WHERE id=?", (upload_id,)).fetchone()[0]
        ) or True,
    )

    assert youtube.postprocess_upload(db_conn, upload_id)["status"] == "published"
    assert during == ["processing"]
    assert db_conn.execute(
        "SELECT playlist_status FROM youtube_uploads WHERE id=?", (upload_id,)
    ).fetchone()[0] == "done"


def test_playlist_failure_clears_processing_so_ui_does_not_hang(db_conn, monkeypatch):
    upload_id = _seed_playlist_upload(db_conn)
    monkeypatch.setattr(youtube, "get_creds_from_db", lambda conn: {"channel_id": "c"})
    monkeypatch.setattr(youtube, "playlist_contains_video", lambda *args: False)
    monkeypatch.setattr(
        youtube, "add_video_to_playlist",
        lambda *args: (_ for _ in ()).throw(RuntimeError("api down")),
    )

    assert youtube.postprocess_upload(db_conn, upload_id)["status"] == "failed"
    assert db_conn.execute(
        "SELECT playlist_status FROM youtube_uploads WHERE id=?", (upload_id,)
    ).fetchone()[0] == "failed"


def test_failed_playlist_is_still_retryable(db_conn, monkeypatch):
    upload_id = _seed_playlist_upload(db_conn, playlist_status="failed")
    monkeypatch.setattr(youtube, "get_creds_from_db", lambda conn: {"channel_id": "c"})
    monkeypatch.setattr(youtube, "playlist_contains_video", lambda *args: True)

    assert youtube.postprocess_upload(db_conn, upload_id)["status"] == "published"
    assert db_conn.execute(
        "SELECT playlist_status FROM youtube_uploads WHERE id=?", (upload_id,)
    ).fetchone()[0] == "done"


class RunningWorker:
    def get_status(self):
        return {"running": True}


def _client(tmp_path):
    conn = db.connect(str(tmp_path / "routes.db"))
    db.init_schema(conn)
    app.state.conn = conn
    app.state.db_lock = threading.Lock()
    app.state.worker = None
    app.state.job_queue = object()
    return conn, TestClient(app)


def test_upload_route_enqueues_instead_of_blocking_on_the_network(tmp_path, monkeypatch):
    conn, client = _client(tmp_path)
    conn.execute(
        "INSERT INTO youtube_credentials (access_token, refresh_token, token_expiry, channel_id, channel_name, created_at, updated_at)"
        " VALUES ('a', 'r', '2030', 'c', 'Chan', '2020', '2020')"
    )
    conn.commit()
    monkeypatch.setattr(youtube, "is_configured", lambda: True)
    monkeypatch.setattr(upload_worker, "upload_worker", RunningWorker())
    monkeypatch.setattr(
        youtube, "process_upload",
        lambda *args: pytest.fail("route must not run the upload inline"),
    )

    res = client.post("/youtube/upload", data={"video_path": "/tmp/v.mp4", "title": "T"})

    assert res.status_code == 200
    assert res.json()["status"] == "pending"
    row = conn.execute("SELECT status, video_path FROM youtube_uploads").fetchone()
    assert row["status"] == "pending" and row["video_path"] == "/tmp/v.mp4"
    # The shared lock must be free the moment the response lands, or the progress poll
    # that this feature depends on could never be served during an upload.
    assert app.state.db_lock.acquire(blocking=False)
    app.state.db_lock.release()


def test_upload_route_reports_when_no_worker_can_drain_the_queue(tmp_path, monkeypatch):
    conn, client = _client(tmp_path)
    conn.execute(
        "INSERT INTO youtube_credentials (access_token, refresh_token, token_expiry, channel_id, channel_name, created_at, updated_at)"
        " VALUES ('a', 'r', '2030', 'c', 'Chan', '2020', '2020')"
    )
    conn.commit()
    monkeypatch.setattr(youtube, "is_configured", lambda: True)
    app.state.job_queue = None

    res = client.post("/youtube/upload", data={"video_path": "/tmp/v.mp4", "title": "T"})

    assert res.status_code == 503
    assert conn.execute("SELECT COUNT(*) FROM youtube_uploads").fetchone()[0] == 0


def test_lifespan_starts_the_job_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "life.db"))
    monkeypatch.setattr(settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings, "enable_worker", True)
    with TestClient(app):
        assert app.state.job_queue is app.state.worker
        assert app.state.job_queue is not None






def test_standalone_upload_keeps_description_and_playlist(db_conn):
    upload_id = youtube.enqueue_upload(
        db_conn,
        "video.mp4",
        "Title",
        description="Full description",
        playlist_id="PL123",
    )

    row = db_conn.execute(
        "SELECT description, metadata_snapshot FROM youtube_uploads WHERE id=?", (upload_id,)
    ).fetchone()
    assert row["description"] == "Full description"
    assert json.loads(row["metadata_snapshot"])["automation"]["youtube"] == {
        "playlist_mode": "existing",
        "playlist_id": "PL123",
    }
