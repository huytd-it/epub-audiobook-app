"""Gameplay administration API."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse

from app import media_library
from app.config import settings
from app.gameplay_pool import enqueue_batch
from app.gameplay_preview import generate_preview
from app.gameplay_registry import get_game, list_games
from app.gameplay_repository import list_themes, pool_status
from app.gameplay_retro import RETRO_GAMES
from app.gameplay_scores import BOARD_LIMIT, leaderboard, standings
from app.gameplay_themes import install_theme_zip, theme_prompt

router = APIRouter(prefix="/gameplay", tags=["gameplay"])

# How many clips one batch request may queue. The queue renders them one at a
# time, so this is a planning cap rather than a promise of instant output.
MAX_BATCH_CLIPS = 1000
SUPPORTED_RESOLUTIONS = {(1920, 1080), (1280, 720), (854, 480), (1080, 1920), (1080, 1080)}
SUPPORTED_FPS = {24, 30, 60}


def _locked(request: Request):
    return request.app.state.db_lock, request.app.state.conn


def _storage_root() -> Path:
    """The background library root that storage folders live under."""
    return Path(settings.data_root) / "backgrounds"


def _resolve_storage_folder(name: str) -> Path:
    """Map a picked storage folder to a path under data/backgrounds.

    Empty / ``__root__`` means the library root; any other name is validated as
    a user folder and created when it does not exist yet."""
    root = _storage_root()
    root.mkdir(parents=True, exist_ok=True)
    raw = (name or "").strip()
    if not raw or raw == media_library.ROOT_FOLDER:
        return root
    cleaned = media_library.clean_folder_name(raw)
    dest = root / cleaned
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _storage_options() -> dict:
    return {
        "root": str(_storage_root()),
        "folders": [{"name": name, "count": len(media_library.folder_files(name))}
                    for name in media_library.list_folders()],
    }


@router.get("/status")
def status(request: Request):
    lock, conn = _locked(request)
    with lock:
        themes = list_themes(conn)
        fighters = [dict(r) for r in conn.execute(
            "SELECT * FROM gameplay_fighter ORDER BY wins DESC, eliminations DESC, name")]
        policies = {row["game_id"]: bool(row["enabled"]) for row in conn.execute("SELECT game_id,enabled FROM gameplay_game")}
        catalog = [{**game, "enabled": policies.get(game["id"], True)} for game in list_games()]
        failed = conn.execute("SELECT COUNT(*) FROM gameplay_clip WHERE status='failed'").fetchone()[0]
        oldest_lease = conn.execute("SELECT MIN(updated_at) FROM gameplay_clip WHERE status='reserved'").fetchone()[0]
        return {"catalog": catalog, "pool": pool_status(conn),
                "health": {"failed_clips": failed, "oldest_lease_at": oldest_lease},
                "target_seconds": settings.gameplay_pool_target_seconds,
                "leaderboard": leaderboard(conn, limit=12), "standings": standings(conn),
                "stat_labels": {spec.id: [list(pair) for pair in spec.stat_labels]
                                for spec in RETRO_GAMES},
                "themes": themes, "fighters": fighters, "storage": _storage_options(),
                "batch_limit": MAX_BATCH_CLIPS}


@router.get("/leaderboard")
def board(request: Request, game_id: str | None = None, limit: int = BOARD_LIMIT):
    """Top runs. Without a game_id the board is cross-game and ranks by the 0-1000 rating."""
    if game_id is not None:
        try:
            get_game(game_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
    lock, conn = _locked(request)
    with lock:
        return {"game_id": game_id, "entries": leaderboard(conn, game_id, limit),
                "standings": standings(conn)}


@router.post("/games/{game_id}/toggle")
def toggle_game(request: Request, game_id: str, enabled: bool = Form(...)):
    try:
        get_game(game_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    lock, conn = _locked(request)
    with lock:
        conn.execute("""INSERT INTO gameplay_game (game_id,enabled,family,display_order,config_json,updated_at)
                      VALUES (?,?,?,0,'{}',CURRENT_TIMESTAMP)
                      ON CONFLICT(game_id) DO UPDATE SET enabled=excluded.enabled,updated_at=excluded.updated_at""",
                     (game_id, int(enabled), get_game(game_id).family))
        conn.commit()
    return {"game_id": game_id, "enabled": enabled}


@router.post("/generate")
def generate(request: Request, width: int = Form(1920), height: int = Form(1080),
             fps: int = Form(30), count: int = Form(1), game_id: str = Form("snake_arena"),
             game_ids: str = Form(""), dest_folder: str = Form("")):
    """Queue a batch of random clips across one or several games.

    ``game_ids`` is an optional comma-separated list; when empty the single
    ``game_id`` is used. ``dest_folder`` picks where the MP4s are stored under
    data/backgrounds (empty = the library root, unknown names are created)."""
    if (width, height) not in SUPPORTED_RESOLUTIONS:
        raise HTTPException(400, "Unsupported gameplay resolution")
    if fps not in SUPPORTED_FPS:
        raise HTTPException(400, "Unsupported gameplay FPS")
    picked = [value.strip() for value in (game_ids or "").split(",") if value.strip()]
    selected = list(dict.fromkeys(picked or [game_id]))
    for current in selected:
        try:
            get_game(current)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    try:
        dest = _resolve_storage_folder(dest_folder)
    except ValueError as exc:
        raise HTTPException(400, "Tên thư mục lưu trữ không hợp lệ") from exc
    count = max(1, min(int(count), MAX_BATCH_CLIPS))
    lock, conn = _locked(request)
    with lock:
        disabled = [row["game_id"] for row in conn.execute(
            "SELECT game_id FROM gameplay_game WHERE enabled=0") if row["game_id"] in selected]
        if disabled:
            raise HTTPException(409, f"Gameplay game is disabled: {', '.join(disabled)}")
        return enqueue_batch(conn, width=width, height=height, fps=fps, count=count,
                             game_ids=selected, dest_dir=dest)


@router.post("/previews/{game_id}")
def generate_gameplay_preview(game_id: str):
    try:
        get_game(game_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:
        return generate_preview(game_id)
    except RuntimeError as exc:
        raise HTTPException(500, str(exc)) from exc


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
