"""Tests for database import/export."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import db
from app.database_io import user_table_names, export_sql, export_json, import_sql, import_json

_NOW = datetime.now(timezone.utc).isoformat()

def _conn():
    c = db.connect(":memory:")
    db.init_schema(c)
    c.execute("INSERT INTO app_state (key, value) VALUES ('k1', 'v1')")
    c.execute("INSERT INTO music (name, file_path, created_at) VALUES ('m1', '/tmp/m1.mp3', ?)", (_NOW,))
    c.commit()
    return c

def test_user_table_names():
    conn = _conn()
    names = user_table_names(conn)
    assert "book" in names
    assert "app_state" in names
    assert "music" in names
    assert not any(n.startswith("sqlite_") for n in names)

def test_export_sql_all_tables():
    conn = _conn()
    sql = export_sql(conn)
    assert sql.startswith("-- TABLE:")
    assert "app_state" in sql
    assert "music" in sql
    assert "INSERT INTO" in sql

def test_export_sql_selected_tables():
    conn = _conn()
    sql = export_sql(conn, tables=["app_state"])
    assert "app_state" in sql
    assert "music" not in sql

def test_export_sql_includes_create_and_indexes():
    conn = _conn()
    sql = export_sql(conn, tables=["patch"])
    assert "CREATE TABLE" in sql
    # patch has 3 indexes in sqlite_master
    idx_count = sql.count("CREATE INDEX")
    assert idx_count >= 1

def test_export_sql_empty_table():
    conn = _conn()
    sql = export_sql(conn, tables=["voice_meta"])
    assert "voice_meta" in sql
    # empty table → no INSERT statements
    insert_lines = [l for l in sql.split("\n") if l.startswith("INSERT")]
    assert len(insert_lines) == 0

def test_export_json_all_tables():
    conn = _conn()
    data = export_json(conn)
    assert isinstance(data, dict)
    assert "app_state" in data
    assert "music" in data
    assert data["app_state"] == [{"key": "k1", "value": "v1"}]

def test_export_json_selected_tables():
    conn = _conn()
    data = export_json(conn, tables=["music"])
    assert "music" in data
    assert "app_state" not in data

def test_export_json_returns_dicts():
    conn = _conn()
    data = export_json(conn, tables=["app_state"])
    row = data["app_state"][0]
    assert row["key"] == "k1"
    assert row["value"] == "v1"

def test_import_sql_overwrite():
    conn = _conn()
    sql = export_sql(conn, tables=["music"])
    import_sql(conn, sql, mode="overwrite", tables=["music"])
    rows = conn.execute("SELECT name FROM music").fetchall()
    assert len(rows) == 1
    assert rows[0]["name"] == "m1"

def test_import_sql_overwrite_clears_old_data():
    conn = _conn()
    sql = export_sql(conn, tables=["music"])
    conn.execute("INSERT INTO music (name, file_path, created_at) VALUES ('old', '/tmp/o.mp3', ?)", (_NOW,))
    conn.commit()
    import_sql(conn, sql, mode="overwrite", tables=["music"])
    rows = [r["name"] for r in conn.execute("SELECT name FROM music").fetchall()]
    assert rows == ["m1"]

def test_import_sql_merge_appends_new_data():
    conn = _conn()
    sql = export_sql(conn, tables=["music"])
    conn.execute("INSERT INTO music (name, file_path, created_at) VALUES ('existing', '/tmp/e.mp3', ?)", (_NOW,))
    conn.commit()
    import_sql(conn, sql, mode="merge", tables=["music"])
    rows = [r["name"] for r in conn.execute("SELECT name FROM music ORDER BY name").fetchall()]
    assert "existing" in rows
    assert "m1" in rows

def test_import_json_overwrite():
    conn = _conn()
    data = export_json(conn, tables=["app_state"])
    conn.execute("INSERT INTO app_state (key, value) VALUES ('existing', 'ev')")
    conn.commit()
    import_json(conn, data, mode="overwrite", tables=["app_state"])
    rows = dict(conn.execute("SELECT key, value FROM app_state").fetchall())
    assert rows == {"k1": "v1"}

def test_import_json_merge():
    conn = _conn()
    data = export_json(conn, tables=["app_state"])
    conn.execute("INSERT INTO app_state (key, value) VALUES ('existing', 'ev')")
    conn.commit()
    import_json(conn, data, mode="merge", tables=["app_state"])
    rows = dict(conn.execute("SELECT key, value FROM app_state").fetchall())
    assert rows == {"k1": "v1", "existing": "ev"}

def test_import_json_merge_ignores_duplicate_pk():
    conn = _conn()
    data = export_json(conn, tables=["app_state"])
    conn.execute("INSERT OR REPLACE INTO app_state (key, value) VALUES ('k1', 'modified')")
    conn.commit()
    import_json(conn, data, mode="merge", tables=["app_state"])
    row = conn.execute("SELECT value FROM app_state WHERE key='k1'").fetchone()
    assert row["value"] == "modified"

def test_import_sql_filter_tables():
    conn = _conn()
    full_sql = export_sql(conn)
    conn.execute("INSERT INTO app_state (key, value) VALUES ('keep', 'me')")
    conn.commit()
    import_sql(conn, full_sql, mode="overwrite", tables=["music"])
    row = conn.execute("SELECT value FROM app_state WHERE key='keep'").fetchone()
    assert row is not None
    assert row["value"] == "me"

def test_import_json_filter_tables():
    conn = _conn()
    full_json = export_json(conn)
    conn.execute("INSERT INTO app_state (key, value) VALUES ('keep', 'me')")
    conn.commit()
    import_json(conn, full_json, mode="overwrite", tables=["music"])
    row = conn.execute("SELECT value FROM app_state WHERE key='keep'").fetchone()
    assert row is not None

def test_import_sql_invalid_mode_raises():
    conn = _conn()
    sql = export_sql(conn)
    with pytest.raises(ValueError, match="mode must be"):
        import_sql(conn, sql, mode="overite")

def test_import_json_invalid_mode_raises():
    conn = _conn()
    data = export_json(conn)
    with pytest.raises(ValueError, match="mode must be"):
        import_json(conn, data, mode="merg")

def test_import_unknown_table_sql_raises():
    conn = _conn()
    sql = "-- TABLE: nonexistent\nCREATE TABLE nonexistent (id INT);"
    with pytest.raises(ValueError, match="nonexistent"):
        import_sql(conn, sql)

def test_import_unknown_table_json_raises():
    conn = _conn()
    with pytest.raises(ValueError, match="nonexistent"):
        import_json(conn, {"nonexistent": [{"id": 1}]})

def test_import_unknown_table_with_filter_skips_check():
    conn = _conn()
    sql = "-- TABLE: nonexistent\nCREATE TABLE nonexistent (id INT);"
    # With explicit table filter, validation is skipped (table must exist)
    conn.execute("CREATE TABLE IF NOT EXISTS nonexistent (id INT)")
    import_sql(conn, sql, mode="overwrite", tables=["nonexistent"])  # no error


import json as json_mod

from fastapi.testclient import TestClient
from app.main import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    settings_mod = __import__("app.config", fromlist=["settings"])
    monkeypatch.setattr(settings_mod.settings, "db_path", str(tmp_path / "test.db"))
    monkeypatch.setattr(settings_mod.settings, "data_root", str(tmp_path))
    monkeypatch.setattr(settings_mod.settings, "enable_worker", False)
    with TestClient(app) as c:
        yield c


def test_export_sql_via_api(client):
    resp = client.get("/api/db/export?format=sql")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/sql"
    assert resp.headers["content-disposition"].startswith("attachment")
    assert "CREATE TABLE" in resp.text


def test_export_json_via_api(client):
    resp = client.get("/api/db/export?format=json")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/json"
    data = resp.json()
    assert isinstance(data, dict)


def test_export_filter_tables_via_api(client):
    resp = client.get("/api/db/export?format=json&tables=app_state,music")
    assert resp.status_code == 200
    data = resp.json()
    assert "app_state" in data
    assert "music" in data
    assert "book" not in data


def test_export_invalid_format_returns_400(client):
    resp = client.get("/api/db/export?format=csv")
    assert resp.status_code == 400


def test_import_sql_overwrite_via_api(client):
    resp = client.get("/api/db/export?format=sql")
    sql_content = resp.text
    resp = client.post("/api/db/import", files={
        "file": ("dump.sql", sql_content, "application/sql"),
    }, data={"format": "sql", "mode": "overwrite"})
    assert resp.status_code == 200


def test_import_json_merge_via_api(client):
    resp = client.get("/api/db/export?format=json")
    content = json_mod.dumps(resp.json())
    resp = client.post("/api/db/import", files={
        "file": ("dump.json", content, "application/json"),
    }, data={"format": "json", "mode": "merge"})
    assert resp.status_code == 200


def test_import_with_table_filter_via_api(client):
    resp = client.get("/api/db/export?format=json")
    content = json_mod.dumps(resp.json())
    resp = client.post("/api/db/import", files={
        "file": ("dump.json", content, "application/json"),
    }, data={"format": "json", "mode": "overwrite", "tables": "music"})
    assert resp.status_code == 200


def test_import_invalid_file_returns_400(client):
    resp = client.post("/api/db/import", files={
        "file": ("dump.txt", b"invalid", "text/plain"),
    }, data={"format": "sql", "mode": "overwrite"})
    assert resp.status_code == 400
