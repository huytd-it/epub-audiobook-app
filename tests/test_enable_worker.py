"""Tests for the ENABLE_WORKER=false dev mode.

When settings.enable_worker is False, the FastAPI lifespan must still set up
the DB and run recovery/backfill, but must NOT start the background PatchWorker
loop. /health reports worker_state='disabled'.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app import db as app_db
from app.main import app
from app.worker import DisabledWorker


def test_lifespan_skips_worker_loop_when_disabled(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    settings_mod = __import__("app.config", fromlist=["settings"])
    monkeypatch.setattr(settings_mod.settings, "db_path", str(db_path))
    monkeypatch.setattr(settings_mod.settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings_mod.settings, "enable_worker", False)

    with TestClient(app) as c:
        worker = c.app.state.worker
        assert isinstance(worker, DisabledWorker)
        assert worker.state == "disabled"

        # /health reports disabled state with 200.
        resp = c.get("/health")
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["status"] == "ok"
        assert payload["worker_state"] == "disabled"
        assert payload["current_patch_id"] is None
        assert payload["queue_depth"] == 0
        assert payload["last_heartbeat_at"] is None


def test_lifespan_runs_db_init_even_when_worker_disabled(tmp_path, monkeypatch):
    """The DB schema must still be created and the tables accessible, even if
    the worker loop is suppressed. /queue/stats must respond."""
    db_path = tmp_path / "test.db"
    settings_mod = __import__("app.config", fromlist=["settings"])
    monkeypatch.setattr(settings_mod.settings, "db_path", str(db_path))
    monkeypatch.setattr(settings_mod.settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings_mod.settings, "enable_worker", False)

    with TestClient(app) as c:
        resp = c.get("/queue/stats")
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["patch"] == {"pending": 0, "processing": 0, "done": 0, "failed": 0}
        assert stats["book_job"] == {"pending": 0, "processing": 0, "done": 0, "failed": 0}

        # And the schema really is there: app_state is queryable.
        with c.app.state.db_lock:
            cur = c.app.state.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('book_job', 'app_state')"
            )
            tables = {row["name"] for row in cur.fetchall()}
        assert tables == {"book_job", "app_state"}
