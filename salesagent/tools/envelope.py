"""Envelope builder — the context-protection rule lives HERE as code, not
convention: the model sees summary + ≤15 sample rows + ≤20 columns + stats;
full rows go to the artifact store. artifact_peek may raise the sample cap
to 50 for its own responses.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Callable

from ..artifacts import store
from ..db import utcnow

SAMPLE_ROWS_CAP = 15
PEEK_ROWS_CAP = 50
COLUMNS_CAP = 20
STATS_ENTRIES_CAP = 30


def _cap_stats(stats: dict | None) -> dict | None:
    if not stats:
        return stats
    out = {}
    for k, v in stats.items():
        if isinstance(v, dict) and len(v) > STATS_ENTRIES_CAP:
            top = dict(sorted(v.items(), key=lambda kv: -(kv[1] if isinstance(kv[1], (int, float)) else 0))[:STATS_ENTRIES_CAP])
            top["..."] = f"+{len(v) - STATS_ENTRIES_CAP} more"
            out[k] = top
        else:
            out[k] = v
    return out


def envelope(*, ok: bool = True, kind: str, summary: str,
             artifact: dict | None = None,
             columns: list[dict] | None = None,
             sample_rows: list[list] | None = None,
             row_count: int | None = None,
             stats: dict | None = None,
             warnings: list[str] | None = None,
             provenance: list[dict] | None = None,
             job_id: str | None = None,
             markdown: str | None = None,
             error: str | None = None,
             max_sample: int = SAMPLE_ROWS_CAP) -> dict:
    cols = (columns or [])[:COLUMNS_CAP]
    env = {
        "ok": ok, "kind": kind, "summary": summary,
        "artifact_id": artifact["artifact_id"] if artifact else None,
        "version": artifact["version"] if artifact else None,
        "columns": [{k: c[k] for k in ("key", "label", "type", "format", "hidden")
                     if k in c} for c in cols],
        "sample_rows": (sample_rows or [])[:max_sample],
        "row_count": row_count,
        "stats": _cap_stats(stats),
        "warnings": warnings or [],
        "provenance": provenance or [],
        "job_id": job_id,
    }
    if markdown is not None:
        env["markdown"] = markdown[:6000]
    if error:
        env["error"] = error
    return env


def error_envelope(message: str, *, error_type: str = "ToolError") -> dict:
    return envelope(ok=False, kind="error", summary=message, error=error_type)


def table_envelope(conn: sqlite3.Connection, emit: Callable[[str, dict], None], *,
                   conversation_id: str, tool: str, title: str,
                   columns: list[dict], rows: list[list],
                   provenance: list[dict], summary: str | None = None,
                   warnings: list[str] | None = None, stats: dict | None = None,
                   styling: dict | None = None, chart: dict | None = None,
                   map_spec: dict | None = None, kind: str = "table") -> dict:
    """The one-call path every data tool uses: store full rows as an artifact,
    notify the frontend, hand the model the capped preview."""
    art = store.create(conn, conversation_id=conversation_id, tool=tool,
                       kind=kind, title=title, columns=columns, rows=rows,
                       styling=styling, provenance=provenance, chart=chart,
                       map_spec=map_spec)
    emit("artifact", {"artifact_id": art["artifact_id"], "version": art["version"],
                      "kind": kind, "title": title})
    return envelope(
        kind=kind,
        summary=summary or f"{len(rows)} rows -> artifact {art['artifact_id']}",
        artifact=art, columns=columns, sample_rows=rows[:SAMPLE_ROWS_CAP],
        row_count=len(rows), stats=stats, warnings=warnings,
        provenance=provenance)


def prov(source: str, detail: str | None = None, url: str | None = None) -> dict:
    return {"source": source, "detail": detail, "url": url, "fetched_at": utcnow()}
