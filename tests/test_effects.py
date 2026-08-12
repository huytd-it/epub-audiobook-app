"""Tests for global sound effect library routes."""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

from app import repository
from app.db import connect, init_schema


@pytest.fixture()
def client(tmp_path, monkeypatch):
    settings_mod = __import__("app.config", fromlist=["settings"])
    monkeypatch.setattr(settings_mod.settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings_mod.settings, "data_root", str(tmp_path))
    from app.main import app
    with TestClient(app) as c:
        yield c


def _upload_effect(client, marker="[test]", desc="test effect"):
    audio = b"\x00" * 100
    return client.post(
        "/effects/add",
        data={"marker": marker, "description": desc},
        files={"file": ("test.wav", io.BytesIO(audio), "audio/wav")},
    )


class TestEffectsRoutes:

    def test_add_effect(self, client):
        resp = _upload_effect(client)
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "id" in data

    def test_list_effects(self, client):
        _upload_effect(client, marker="[khóc]")
        resp = client.get("/effects/list")
        assert resp.status_code == 200
        effects = resp.json()["effects"]
        assert len(effects) >= 1

    def test_edit_effect(self, client):
        resp = _upload_effect(client, marker="[old]")
        eid = resp.json()["id"]
        resp = client.post(f"/effects/{eid}/edit", json={"marker": "[new]", "description": "updated"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_delete_effect(self, client):
        resp = _upload_effect(client)
        eid = resp.json()["id"]
        resp = client.post(f"/effects/{eid}/delete")
        assert resp.status_code == 200
        resp = client.get("/effects/list")
        effects = resp.json()["effects"]
        assert all(e["id"] != eid for e in effects)

    def test_bulk_add(self, client):
        files = [
            ("files", ("laugh.wav", io.BytesIO(b"\x00" * 50), "audio/wav")),
            ("files", ("cry.wav", io.BytesIO(b"\x00" * 50), "audio/wav")),
        ]
        resp = client.post("/effects/bulk-add", files=files)
        assert resp.status_code == 200
        data = resp.json()
        assert data["added"] == 2

    def test_global_effects_visible_in_book(self, client):
        _upload_effect(client, marker="[global]")
        resp = client.get("/effects/list")
        assert resp.status_code == 200
        markers = [e["marker"] for e in resp.json()["effects"]]
        assert "[global]" in markers
