"""Artifact store: full result data lives HERE, never in the model context.

An artifact is identity (artifacts row) + an immutable version chain
(artifact_versions). Every transform appends a version with its ops recorded —
the chain is the undo history and the audit trail. Rows are stored as
zlib-compressed JSON arrays-of-arrays (not objects: size).
"""
from __future__ import annotations

import json
import sqlite3
import uuid
import zlib

from ..db import utcnow

MAX_ROWS = 50_000  # tools must narrow the query beyond this


class ArtifactTooLarge(ValueError):
    pass


def _pack(rows: list[list]) -> bytes:
    return zlib.compress(json.dumps(rows, ensure_ascii=False,
                                    default=str).encode("utf-8"), 6)


def _unpack(blob: bytes) -> list[list]:
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def create(conn: sqlite3.Connection, *, conversation_id: str, tool: str,
           kind: str, title: str, columns: list[dict], rows: list[list],
           styling: dict | None = None, provenance: list[dict] | None = None,
           chart: dict | None = None, map_spec: dict | None = None,
           ops: list | None = None) -> dict:
    if len(rows) > MAX_ROWS:
        raise ArtifactTooLarge(
            f"{len(rows)} rows exceeds the {MAX_ROWS}-row artifact cap — "
            "narrow the query (filters/limit) instead.")
    aid = "art_" + uuid.uuid4().hex[:10]
    now = utcnow()
    with conn:
        conn.execute(
            "INSERT INTO artifacts (artifact_id, conversation_id, created_by_tool,"
            " title, kind, created_at) VALUES (?,?,?,?,?,?)",
            (aid, conversation_id, tool, title, kind, now))
        conn.execute(
            "INSERT INTO artifact_versions (artifact_id, version, parent_version,"
            " ops_json, columns_json, rows_blob, row_count, styling_json,"
            " provenance_json, chart_json, map_json, kind, title, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, 1, None, json.dumps(ops) if ops else None,
             json.dumps(columns), _pack(rows), len(rows),
             json.dumps(styling) if styling else None,
             json.dumps(provenance) if provenance else None,
             json.dumps(chart) if chart else None,
             json.dumps(map_spec) if map_spec else None,
             kind, title, now))
    return {"artifact_id": aid, "version": 1, "kind": kind, "title": title,
            "row_count": len(rows)}


def latest_version(conn: sqlite3.Connection, artifact_id: str) -> int | None:
    r = conn.execute("SELECT MAX(version) AS v FROM artifact_versions "
                     "WHERE artifact_id=?", (artifact_id,)).fetchone()
    return r["v"] if r and r["v"] else None


def get(conn: sqlite3.Connection, artifact_id: str,
        version: int | None = None) -> dict | None:
    """Full spec dict (rows decompressed). None if missing."""
    v = version or latest_version(conn, artifact_id)
    if not v:
        return None
    r = conn.execute(
        "SELECT * FROM artifact_versions WHERE artifact_id=? AND version=?",
        (artifact_id, v)).fetchone()
    if not r:
        return None
    a = conn.execute("SELECT conversation_id, created_by_tool FROM artifacts "
                     "WHERE artifact_id=?", (artifact_id,)).fetchone()
    return {
        "spec_version": 1,
        "artifact_id": artifact_id,
        "version": r["version"],
        "parent_version": r["parent_version"],
        "latest_version": latest_version(conn, artifact_id),
        "kind": r["kind"],
        "title": r["title"],
        "created_by": a["created_by_tool"] if a else None,
        "conversation_id": a["conversation_id"] if a else None,
        "columns": json.loads(r["columns_json"]),
        "rows": _unpack(r["rows_blob"]),
        "row_count": r["row_count"],
        "styling": json.loads(r["styling_json"]) if r["styling_json"] else None,
        "provenance": json.loads(r["provenance_json"]) if r["provenance_json"] else [],
        "chart": json.loads(r["chart_json"]) if r["chart_json"] else None,
        "map": json.loads(r["map_json"]) if r["map_json"] else None,
        "ops": json.loads(r["ops_json"]) if r["ops_json"] else None,
        "created_at": r["created_at"],
    }


def add_version(conn: sqlite3.Connection, artifact_id: str, *,
                base: dict, ops: list, columns: list[dict], rows: list[list],
                kind: str, title: str | None = None,
                styling: dict | None = None, chart: dict | None = None,
                map_spec: dict | None = None,
                provenance: list[dict] | None = None) -> dict:
    if len(rows) > MAX_ROWS:
        raise ArtifactTooLarge(f"{len(rows)} rows exceeds the artifact cap")
    v = (latest_version(conn, artifact_id) or 0) + 1
    with conn:
        conn.execute(
            "INSERT INTO artifact_versions (artifact_id, version, parent_version,"
            " ops_json, columns_json, rows_blob, row_count, styling_json,"
            " provenance_json, chart_json, map_json, kind, title, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (artifact_id, v, base["version"], json.dumps(ops),
             json.dumps(columns), _pack(rows), len(rows),
             json.dumps(styling if styling is not None else base.get("styling")),
             # provenance flows from base unless a merge unioned in more sources
             json.dumps(provenance if provenance is not None
                        else (base.get("provenance") or [])),
             json.dumps(chart) if chart else None,
             json.dumps(map_spec) if map_spec else None,
             kind, title or base.get("title"), utcnow()))
    return {"artifact_id": artifact_id, "version": v, "kind": kind,
            "title": title or base.get("title"), "row_count": len(rows)}
