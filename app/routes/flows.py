from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app import repository
from app.deps import locked_conn
from app.jobqueue import store

router = APIRouter(prefix="/flows")
NODE_TYPES = {"audio": "flow_audio", "video": "flow_video", "youtube": "flow_youtube"}


class FlowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    nodes: list[str] = Field(min_length=1)


class FlowRunCreate(BaseModel):
    book_id: int
    patch_ids: list[int] = Field(min_length=1)
    privacy: str = "private"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _flow(row) -> dict:
    return {"id": row["id"], "name": row["name"],
            "nodes": json.loads(row["definition_json"])["nodes"]}




@router.get("/api")
def list_flows(request: Request):
    with locked_conn(request) as conn:
        rows = conn.execute("SELECT * FROM flow_definition ORDER BY id DESC").fetchall()
    return {"flows": [_flow(row) for row in rows]}


@router.post("/api", status_code=201)
def create_flow(request: Request, body: FlowCreate):
    nodes = list(dict.fromkeys(body.nodes))
    if any(node not in NODE_TYPES for node in nodes):
        raise HTTPException(422, detail="node must be audio, video or youtube")
    now = _now()
    with locked_conn(request) as conn:
        cur = conn.execute(
            "INSERT INTO flow_definition(name, definition_json, created_at, updated_at) VALUES(?,?,?,?)",
            (body.name.strip(), json.dumps({"nodes": nodes}), now, now),
        )
        conn.commit()
    return {"id": cur.lastrowid, "name": body.name.strip(), "nodes": nodes}


@router.get("/{flow_id}/patches")
def flow_patches(request: Request, flow_id: int, book_id: int):
    with locked_conn(request) as conn:
        if conn.execute("SELECT 1 FROM flow_definition WHERE id=?", (flow_id,)).fetchone() is None:
            raise HTTPException(404, detail="flow not found")
        patches = repository.list_patches(conn, book_id)
    return {"patches": [{"id": p.id, "index": p.patch_index, "name": p.name,
                          "status": p.status} for p in patches]}


@router.post("/{flow_id}/runs", status_code=201)
def run_flow(request: Request, flow_id: int, body: FlowRunCreate):
    if body.privacy not in {"private", "unlisted", "public"}:
        raise HTTPException(422, detail="invalid privacy")
    patch_ids = list(dict.fromkeys(body.patch_ids))
    with locked_conn(request) as conn:
        flow = conn.execute("SELECT * FROM flow_definition WHERE id=?", (flow_id,)).fetchone()
        if flow is None:
            raise HTTPException(404, detail="flow not found")
        placeholders = ",".join("?" for _ in patch_ids)
        patches = conn.execute(
            f"SELECT id FROM patch WHERE book_id=? AND id IN ({placeholders})",
            [body.book_id, *patch_ids],
        ).fetchall()
        if len(patches) != len(patch_ids):
            raise HTTPException(422, detail="one or more patches do not belong to the book")
        definition = json.loads(flow["definition_json"])
        now = _now()
        cur = conn.execute(
            "INSERT INTO flow_run(flow_definition_id, book_id, definition_snapshot, created_at) VALUES(?,?,?,?)",
            (flow_id, body.book_id, flow["definition_json"], now),
        )
        run_id = cur.lastrowid
        job_ids = []
        for patch_id in patch_ids:
            previous = None
            for node in definition["nodes"]:
                payload = {"patch_id": patch_id}
                if node == "youtube":
                    payload["privacy"] = body.privacy
                job_id = store.enqueue(
                    conn, NODE_TYPES[node], payload=payload, book_id=body.book_id,
                    dedupe_key=f"flow={run_id}:patch={patch_id}:node={node}",
                    flow_run_id=run_id, node_id=node, patch_id=patch_id,
                    depends_on=previous,
                )
                previous = job_id
                job_ids.append(job_id)
    return {"run_id": run_id, "jobs": job_ids, "job_count": len(job_ids)}
