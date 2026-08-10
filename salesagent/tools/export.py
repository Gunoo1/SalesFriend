"""Excel export: artifact(s) -> formatted workbook with READ ME + auto
Methodology & Sources tabs (shop conventions)."""
from __future__ import annotations

import uuid

from ..artifacts import store
from ..integrations.xlsx import build_workbook
from .envelope import envelope, error_envelope
from .registry import CostClass, tool_spec


@tool_spec(
    name="export_excel",
    description=(
        "Export one or more artifacts (latest versions) to a color-coded Excel "
        "workbook with a READ ME tab and an auto-generated Methodology & "
        "Sources tab. Omit artifact_ids to export every artifact in this "
        "conversation. Returns a download link. Free."),
    input_schema={"properties": {
        "artifact_ids": {"type": "array", "items": {"type": "string"}},
        "filename": {"type": "string", "description": "optional, without extension"},
    }},
    cost_class=CostClass.FREE,
)
def export_excel(ctx, artifact_ids: list[str] | None = None,
                 filename: str | None = None) -> dict:
    conn = ctx.rw()
    if not artifact_ids:
        artifact_ids = [r["artifact_id"] for r in conn.execute(
            "SELECT artifact_id FROM artifacts WHERE conversation_id=? "
            "AND archived=0 ORDER BY created_at", (ctx.conversation_id,))]
    specs = []
    for aid in artifact_ids:
        spec = store.get(conn, aid)
        if spec and spec.get("conversation_id") == ctx.conversation_id:
            specs.append(spec)
    if not specs:
        return error_envelope("no artifacts to export in this conversation")

    wb = build_workbook(specs)
    safe = "".join(ch for ch in (filename or "salesagent_export")
                   if ch.isalnum() or ch in "-_")[:60] or "salesagent_export"
    fname = f"{safe}_{uuid.uuid4().hex[:6]}.xlsx"
    out = ctx.settings.files_dir / fname
    wb.save(out)
    url = f"/api/files/{fname}"
    return envelope(
        kind="markdown",
        summary=f"exported {len(specs)} tab(s) -> {url}",
        markdown=f"**Excel ready:** [{fname}]({url}) — {len(specs)} data tab(s) "
                 f"+ READ ME + Methodology & Sources.",
        provenance=[])
