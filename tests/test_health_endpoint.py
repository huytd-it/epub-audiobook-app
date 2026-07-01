"""End-to-end test for the /health endpoint behavior under fresh and stale heartbeats."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import db as app_db
from app.main import app
from app.worker import PatchWorker


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    settings_mod = __import__("app.config", fromlist=["settings"])
    monkeypatch.setattr(settings_mod.settings, "db_path", str(db_path))
    monkeypatch.setattr(settings_mod.settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings_mod.settings, "worker_poll_interval", 0.05)
    monkeypatch.setattr(settings_mod.settings, "worker_shutdown_timeout_seconds", 1.0)
    # This test needs a live PatchWorker regardless of the developer's local .env (which may
    # have ENABLE_WORKER=false for their own dev server) - config.py now loads .env, so pin
    # this explicitly rather than relying on the field default.
    monkeypatch.setattr(settings_mod.settings, "enable_worker", True)
    with TestClient(app) as c:
        yield c


def test_health_returns_200_when_worker_is_alive(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["worker_state"] in ("idle", "busy", "paused")
    assert "last_heartbeat_at" in payload
    assert "queue_depth" in payload
    assert "current_patch_id" in payload


def test_health_returns_503_when_heartbeat_is_stale(client):
    worker: PatchWorker = client.app.state.worker
    # Backdate the heartbeat to more than 3 * poll_interval ago.
    stale = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    worker.last_heartbeat_at = stale
    resp = client.get("/health")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["status"] == "degraded"
    assert "reason" in payload
    assert "heartbeat" in payload["reason"].lower()
