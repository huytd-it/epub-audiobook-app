"""Import/export SQLite database."""
from __future__ import annotations

import re
import sqlite3


def user_table_names(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r["name"] for r in rows]


def _resolve_tables(conn: sqlite3.Connection, tables: list[str] | None) -> list[str]:
    all_tables = user_table_names(conn)
    if tables is None:
        return all_tables
    unknown = set(tables) - set(all_tables)
    if unknown:
        raise ValueError(f"Unknown tables: {', '.join(sorted(unknown))}")
    return tables


def _sql_val(v):
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, bytes):
        return "X'" + v.hex() + "'"
    escaped = str(v).replace("'", "''")
    return f"'{escaped}'"


def export_sql(conn: sqlite3.Connection, tables: list[str] | None = None) -> str:
    selected = _resolve_tables(conn, tables)
    lines: list[str] = []
    for table in selected:
        create = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        if create is None or not create["sql"]:
            continue
        lines.append(f"-- TABLE: {table}")
        lines.append(create["sql"] + ";")
        for idx in conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        ):
            lines.append(idx["sql"] + ";")
        cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()]
        for row in conn.execute(f'SELECT * FROM "{table}"'):
            vals = [_sql_val(v) for v in row]
            lines.append(f'INSERT INTO "{table}" ({", ".join(cols)}) VALUES ({", ".join(vals)});')
    return "\n".join(lines)


def export_json(conn: sqlite3.Connection, tables: list[str] | None = None) -> dict[str, list[dict]]:
    selected = _resolve_tables(conn, tables)
    result: dict[str, list[dict]] = {}
    for table in selected:
        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
        result[table] = [dict(r) for r in rows]
    return result


def _table_order() -> list[str]:
    return [
        "gameplay_clip",
        "gameplay_replay",
        "gameplay_fighter",
        "gameplay_theme",
        "gameplay_game",
        "job_dependency",
        "job",
        "patch_pipeline",
        "book_media_selection",
        "videos",
        "youtube_playlist_map",
        "patch_warning",
        "voice_meta",
        "patch_export",
        "patch",
        "chapter",
        "book_job",
        "text_replace_rule",
        "google_drive_credentials",
        "youtube_uploads",
        "youtube_credentials",
        "drive_oauth_client",
        "drive_sync_target",
        "app_state",
        "music",
        "book",
        "automation_settings",
        "media_assets",
        "batches",
        "material_cache",
        "sound_effect",
    ]


def _clear_tables(conn: sqlite3.Connection, tables: list[str]) -> None:
    order = _table_order()
    conn.execute("PRAGMA foreign_keys = OFF")
    for table in order:
        if table in tables:
            conn.execute(f'DELETE FROM "{table}"')
    conn.execute("PRAGMA foreign_keys = ON")


def _validate_import_tables(conn: sqlite3.Connection, input_tables: set[str]) -> None:
    existing = set(user_table_names(conn))
    unknown = input_tables - existing
    if unknown:
        raise ValueError(f"Import contains unknown tables: {', '.join(sorted(unknown))}")


def import_sql(
    conn: sqlite3.Connection,
    sql: str,
    mode: str = "overwrite",
    tables: list[str] | None = None,
) -> None:
    if mode not in ("overwrite", "merge"):
        raise ValueError(f"mode must be 'overwrite' or 'merge', got '{mode}'")
    selected = _resolve_tables(conn, tables) if tables else None
    _TABLE_MARKER_RE = re.compile(r"^-- TABLE:\s*(\w+)", re.MULTILINE)
    blocks = re.split(_TABLE_MARKER_RE, sql)[1:]
    if selected is None:
        _validate_import_tables(conn, {blocks[i] for i in range(0, len(blocks), 2)})
    if mode == "overwrite":
        conn.execute("PRAGMA foreign_keys = OFF")
        for i in range(0, len(blocks), 2):
            table = blocks[i]
            body = blocks[i + 1]
            if selected is not None and table not in selected:
                continue
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.executescript(body)
        conn.execute("PRAGMA foreign_keys = ON")
    else:
        for i in range(0, len(blocks), 2):
            table = blocks[i]
            body = blocks[i + 1]
            if selected is not None and table not in selected:
                continue
            stmts = [s.strip() for s in body.split(";") if s.strip()]
            for stmt in stmts:
                if stmt.upper().startswith("INSERT"):
                    stmt = stmt.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)
                    conn.execute(stmt)
    conn.commit()


def import_json(
    conn: sqlite3.Connection,
    data: dict[str, list[dict]],
    mode: str = "overwrite",
    tables: list[str] | None = None,
) -> None:
    if mode not in ("overwrite", "merge"):
        raise ValueError(f"mode must be 'overwrite' or 'merge', got '{mode}'")
    selected = _resolve_tables(conn, tables) if tables else None
    if selected is None:
        _validate_import_tables(conn, set(data.keys()))
    for table, rows in data.items():
        if selected is not None and table not in selected:
            continue
        if mode == "overwrite":
            _clear_tables(conn, [table])
        for row in rows:
            cols = ", ".join(row.keys())
            placeholders = ", ".join("?" for _ in row)
            sql = f'INSERT OR IGNORE INTO "{table}" ({cols}) VALUES ({placeholders})'
            conn.execute(sql, list(row.values()))
    conn.commit()
