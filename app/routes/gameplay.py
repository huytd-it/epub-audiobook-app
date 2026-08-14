"""Battle Royale administration API."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from app.config import settings
from app.gameplay_pool import maintain_pool
from app.gameplay_repository import list_themes, pool_status
from app.gameplay_themes import install_theme_zip, theme_prompt

router = APIRouter(prefix="/gameplay", tags=["gameplay"])


def _locked(request: Request):
    return request.app.state.db_lock, request.app.state.conn


@router.get("/status")
def status(request: Request):
    lock, conn = _locked(request)
    with lock:
        themes = list_themes(conn)
        fighters = [dict(r) for r in conn.execute(
            "SELECT * FROM gameplay_fighter ORDER BY wins DESC, eliminations DESC, name")]
        return {"pool": pool_status(conn), "target_seconds": settings.gameplay_pool_target_seconds,
                "themes": themes, "fighters": fighters}


@router.post("/generate")
def generate(request: Request, width: int = Form(1920), height: int = Form(1080),
             fps: int = Form(30), count: int = Form(1)):
    lock, conn = _locked(request)
    with lock:
        return maintain_pool(conn, width=width, height=height, fps=fps,
                             target_seconds=max(240, count * 240), max_enqueue=max(1, min(count, 10)))


@router.get("/themes/prompt", response_class=PlainTextResponse)
def prompt(army_theme: str = "futuristic neon army", unit_class: str = "tank, assassin, ranger",
           colors: str = "cyan, magenta, electric lime"):
    return theme_prompt(army_theme, unit_class, colors)


@router.post("/themes/upload")
async def upload_theme(request: Request, file: UploadFile = File(...)):
    data = await file.read(80 * 1024 * 1024 + 1)
    if len(data) > 80 * 1024 * 1024:
        raise HTTPException(413, "Theme ZIP is too large")
    root = Path(settings.data_root) / "gameplay" / "themes"
    try:
        result = install_theme_zip(data, root)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    lock, conn = _locked(request)
    with lock:
        now = conn.execute("SELECT CURRENT_TIMESTAMP").fetchone()[0]
        conn.execute(
            """INSERT INTO gameplay_theme
               (id, version, name, enabled, builtin, manifest_json, asset_dir, created_at, updated_at)
               VALUES (?, ?, ?, 1, 0, ?, ?, ?, ?)
               ON CONFLICT(id,version) DO UPDATE SET name=excluded.name,
               manifest_json=excluded.manifest_json, asset_dir=excluded.asset_dir,
               error_message=NULL, updated_at=excluded.updated_at""",
            (result["id"], result["version"], result["name"], json.dumps(result["manifest"]),
             result["asset_dir"], now, now),)
        conn.commit()
    return result


@router.post("/themes/{theme_id}/{version}/toggle")
def toggle_theme(request: Request, theme_id: str, version: int, enabled: bool = Form(...)):
    lock, conn = _locked(request)
    with lock:
        cur = conn.execute("UPDATE gameplay_theme SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND version=?",
                           (int(enabled), theme_id, version))
        conn.commit()
        if not cur.rowcount:
            raise HTTPException(404, "Theme not found")
    return {"enabled": enabled}


@router.delete("/themes/{theme_id}/{version}")
def delete_theme(request: Request, theme_id: str, version: int):
    lock, conn = _locked(request)
    with lock:
        row = conn.execute("SELECT * FROM gameplay_theme WHERE id=? AND version=?", (theme_id, version)).fetchone()
        if row is None:
            raise HTTPException(404, "Theme not found")
        if row["builtin"]:
            raise HTTPException(409, "Built-in theme cannot be deleted")
        needle = f'"id": "{theme_id}"'
        used = conn.execute("SELECT 1 FROM gameplay_replay WHERE themes_json LIKE ? LIMIT 1", (f"%{needle}%",)).fetchone()
        if used:
            raise HTTPException(409, "Theme is referenced by a replay")
        conn.execute("DELETE FROM gameplay_theme WHERE id=? AND version=?", (theme_id, version))
        conn.commit()
    if row["asset_dir"]:
        shutil.rmtree(row["asset_dir"], ignore_errors=True)
    return {"deleted": True}


@router.get("/themes/{theme_id}/{version}/{filename}")
def theme_asset(request: Request, theme_id: str, version: int, filename: str):
    if filename not in {"tank.png", "assassin.png", "ranger.png", "heal.png", "speed.png", "damage.png", "projectile.png", "elimination.png"}:
        raise HTTPException(404)
    lock, conn = _locked(request)
    with lock:
        row = conn.execute("SELECT asset_dir FROM gameplay_theme WHERE id=? AND version=?", (theme_id, version)).fetchone()
    if not row or not row["asset_dir"] or not (Path(row["asset_dir"]) / filename).is_file():
        raise HTTPException(404)
    return FileResponse(Path(row["asset_dir"]) / filename)
