"""Artifact fetch/transform + exported-file download. Ownership = the
artifact's conversation belongs to the signed-in user."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from ..artifacts import store
from ..artifacts.transforms import TransformError, apply_ops
from ..auth import current_user, db_conn
from ..settings import load_settings

router = APIRouter(prefix="/api")


def _owned_spec(conn: sqlite3.Connection, user: dict, artifact_id: str,
                version: int | None = None) -> dict:
    spec = store.get(conn, artifact_id, version)
    if not spec:
        raise HTTPException(404, "artifact not found")
    row = conn.execute(
        "SELECT c.user_id FROM artifacts a JOIN conversations c "
        "ON c.id = a.conversation_id WHERE a.artifact_id=?",
        (artifact_id,)).fetchone()
    if not row or row["user_id"] != user["id"]:
        raise HTTPException(404, "artifact not found")
    return spec


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, version: int | None = None,
                 user: dict = Depends(current_user),
                 conn: sqlite3.Connection = Depends(db_conn)):
    return _owned_spec(conn, user, artifact_id, version)


@router.get("/artifacts/{artifact_id}/versions")
def get_versions(artifact_id: str, user: dict = Depends(current_user),
                 conn: sqlite3.Connection = Depends(db_conn)):
    _owned_spec(conn, user, artifact_id)  # ownership check
    rows = conn.execute(
        "SELECT version, parent_version, kind, title, row_count, ops_json,"
        " created_at FROM artifact_versions WHERE artifact_id=? ORDER BY version",
        (artifact_id,)).fetchall()
    return {"versions": [dict(r) for r in rows]}


class TransformReq(BaseModel):
    ops: list
    title: str | None = None


@router.post("/artifacts/{artifact_id}/transform")
def post_transform(artifact_id: str, req: TransformReq,
                   user: dict = Depends(current_user),
                   conn: sqlite3.Connection = Depends(db_conn)):
    """Manual UI transforms (the Restore button) — same op grammar as the
    agent's transform_artifact tool."""
    base = _owned_spec(conn, user, artifact_id)
    ops_run = []
    for op in req.ops:   # concat/join reference another owned artifact
        op = dict(op) if isinstance(op, dict) else op
        if isinstance(op, dict) and op.get("op") in ("concat", "join"):
            other = _owned_spec(conn, user, str(op.get("artifact_id") or ""))
            op["_other"] = {"columns": other["columns"], "rows": other["rows"]}
        ops_run.append(op)
    try:
        result = apply_ops(base, ops_run)
    except TransformError as e:
        raise HTTPException(400, str(e))
    if result["revert_to"]:
        target = _owned_spec(conn, user, artifact_id, result["revert_to"])
        art = store.add_version(conn, artifact_id, base=base,
                                ops=req.ops, columns=target["columns"],
                                rows=target["rows"], kind=target["kind"],
                                title=req.title or target["title"],
                                styling=target.get("styling"),
                                chart=target.get("chart"),
                                map_spec=target.get("map"))
    else:
        art = store.add_version(conn, artifact_id, base=base, ops=req.ops,
                                columns=result["columns"], rows=result["rows"],
                                kind=result["kind"],
                                title=req.title or result["title"],
                                styling=result["styling"],
                                chart=result["chart"], map_spec=result["map"])
    return art


@router.get("/artifacts/{artifact_id}/export.xlsx")
def export_one(artifact_id: str, version: int | None = None,
               user: dict = Depends(current_user),
               conn: sqlite3.Connection = Depends(db_conn)):
    import io

    from fastapi.responses import Response

    from ..integrations.xlsx import build_workbook
    spec = _owned_spec(conn, user, artifact_id, version)
    wb = build_workbook([spec])
    buf = io.BytesIO()
    wb.save(buf)
    safe = "".join(ch for ch in (spec.get("title") or artifact_id)
                   if ch.isalnum() or ch in " -_")[:50].strip() or artifact_id
    return Response(
        buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument"
                   ".spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe}.xlsx"'})


@router.get("/files/{name}")
def get_file(name: str, user: dict = Depends(current_user)):
    settings = load_settings()
    safe = Path(name).name  # no traversal
    path = settings.files_dir / safe
    if not path.exists():
        raise HTTPException(404, "file not found")
    return FileResponse(path, filename=safe,
                        media_type="application/vnd.openxmlformats-officedocument"
                                   ".spreadsheetml.sheet")
