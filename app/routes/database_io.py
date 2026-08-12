"""Database import/export REST API."""
from __future__ import annotations

import json
import logging
import sqlite3

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from app.database_io import export_json, export_sql, import_json, import_sql, user_table_names
from app.deps import locked_conn

logger = logging.getLogger(__name__)

router = APIRouter()

_VALID_EXTENSIONS = {".sql", ".json"}


@router.get("/api/db/export")
def db_export(
    request: Request,
    format: str = "sql",
    tables: str | None = None,
):
    if format not in ("sql", "json"):
        raise HTTPException(status_code=400, detail="format must be 'sql' or 'json'")
    table_list = tables.split(",") if tables else None
    with locked_conn(request) as conn:
        if format == "sql":
            content = export_sql(conn, tables=table_list)
            return Response(
                content=content,
                media_type="application/sql",
                headers={"Content-Disposition": "attachment; filename=export.sql"},
            )
        else:
            content = export_json(conn, tables=table_list)
            return Response(
                content=json.dumps(content, ensure_ascii=False, indent=2),
                media_type="application/json",
                headers={"Content-Disposition": "attachment; filename=export.json"},
            )


@router.post("/api/db/import")
def db_import(
    request: Request,
    file: UploadFile = File(...),
    format: str = Form("sql"),
    mode: str = Form("overwrite"),
    tables: str | None = Form(None),
):
    if format not in ("sql", "json"):
        raise HTTPException(status_code=400, detail="format must be 'sql' or 'json'")
    if mode not in ("overwrite", "merge"):
        raise HTTPException(status_code=400, detail="mode must be 'overwrite' or 'merge'")

    ext = "." + file.filename.split(".")[-1].lower() if file.filename else ""
    if ext not in _VALID_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"file extension must be .sql or .json, got '{ext}'")

    raw = file.file.read()
    table_list = tables.split(",") if tables else None

    with locked_conn(request) as conn:
        try:
            if format == "sql":
                import_sql(conn, raw.decode("utf-8"), mode=mode, tables=table_list)
            else:
                import_json(conn, json.loads(raw), mode=mode, tables=table_list)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            logger.exception("db import failed")
            status = 500 if isinstance(exc, sqlite3.OperationalError) else 400
            raise HTTPException(status_code=status, detail=str(exc))

    return {"status": "ok", "mode": mode}
