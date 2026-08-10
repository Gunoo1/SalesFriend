"""View tools: mutate what the rep is looking at WITHOUT re-querying, and
peek at real rows when the agent needs concrete values."""
from __future__ import annotations

import json

from ..artifacts import store
from ..artifacts.transforms import TransformError, apply_ops
from .envelope import PEEK_ROWS_CAP, envelope, error_envelope
from .registry import CostClass, tool_spec

OPS_DOC = ("ops: [{op:filter, col, cmp:eq|ne|gt|gte|lt|lte|in|not_in|contains|"
           "between|isnull|notnull, value}, {op:sort, by:[{col,dir:asc|desc}]}, "
           "{op:groupby, by:[col], aggs:[{col,fn:sum|count|avg|min|max,as}]}, "
           "{op:select, cols:[...]}, {op:limit, n}, {op:rename, map:{key:label}}, "
           "{op:set_styling, styling:{tier_rules:[{column, eq|contains|gte|lte:"
           "value, class:hot|warm|now|std, label?}]}} — color-codes TABLE ROWS "
           "and MAP PINS (hot=green, warm=yellow, now=red, std=gray); first "
           "matching rule wins; eq/contains are text tests (e.g. "
           "{column:'status', eq:'closed', class:'now'}), gte/lte numeric; a "
           "color legend renders automatically on the map/table from the "
           "rules — ALWAYS set label to a short rep-friendly phrase (e.g. "
           "'Top target — 200k+ students nearby'), else the raw test shows, "
           "{op:to_chart, chart:{kind:bar|line|pie, x, y, series?, "
           "agg?:sum|count|avg, top_n?, colors?:{label:'#hex'|hot|warm|now|std}}} "
           "— colors keys are series names, or x/slice values when no series, "
           "{op:to_map, map:{lat,lng,label,popup_cols?}}, {op:to_table}, "
           "{op:revert, to_version}, "
           "{op:append_rows, rows:[{column_key:value,...}]} — hand-add up to "
           "200 literal rows, "
           "{op:concat, artifact_id} — stack another table's rows under this "
           "one (columns unioned by key), "
           "{op:join, artifact_id, on, right_on?, how?:left|inner, "
           "select?:[cols]} — pull columns from another table where key "
           "values match (exact; use entity_merge for fuzzy name matching)]")


def _load_owned(ctx, artifact_id: str, version: int | None = None) -> dict | None:
    spec = store.get(ctx.rw(), artifact_id, version)
    if not spec or spec.get("conversation_id") != ctx.conversation_id:
        return None
    return spec


@tool_spec(
    name="transform_artifact",
    description=(
        "Mutate a view the user is already looking at WITHOUT re-querying: "
        "filter/sort/group/select/limit, switch table<->chart<->map, set row "
        "color tiers, or revert to an earlier version ('undo'). Executes "
        "server-side on the stored data — pass only the small ops spec. Prefer "
        "this over re-querying when the needed columns already exist in the "
        "artifact; re-query the source tool when they don't. Free. " + OPS_DOC),
    input_schema={"properties": {
        "artifact_id": {"type": "string"},
        "ops": {"type": "array", "items": {"type": "object"}},
        "title": {"type": "string", "description": "optional new panel title"},
    }, "required": ["artifact_id", "ops"]},
    cost_class=CostClass.FREE,
)
def transform_artifact(ctx, artifact_id: str, ops: list, title: str | None = None) -> dict:
    base = _load_owned(ctx, artifact_id)
    if not base:
        return error_envelope(f"artifact {artifact_id} not found in this conversation")

    # concat/join reference ANOTHER artifact — resolve it here (ownership
    # checked), hand apply_ops the data via a private _other key, and keep the
    # persisted ops record clean of it (it would bloat the version chain).
    ops_run, extra_prov = [], []
    for op in ops:
        op = dict(op) if isinstance(op, dict) else op
        if isinstance(op, dict) and op.get("op") in ("concat", "join"):
            ref = op.get("artifact_id")
            other = _load_owned(ctx, str(ref)) if ref else None
            if not other:
                return error_envelope(
                    f"{op.get('op')} needs artifact_id of another table in this "
                    f"conversation; '{ref}' not found")
            op["_other"] = {"columns": other["columns"], "rows": other["rows"]}
            extra_prov.extend(other.get("provenance") or [])
        ops_run.append(op)
    ops_clean = [{k: v for k, v in op.items() if k != "_other"}
                 if isinstance(op, dict) else op for op in ops_run]

    try:
        result = apply_ops(base, ops_run)
    except TransformError as e:
        return error_envelope(str(e), error_type="TransformError")

    merged_prov = None
    if extra_prov:
        merged_prov = list(base.get("provenance") or [])
        seen = {json.dumps(p, sort_keys=True, default=str) for p in merged_prov}
        for p in extra_prov:
            k = json.dumps(p, sort_keys=True, default=str)
            if k not in seen:
                seen.add(k)
                merged_prov.append(p)

    if result["revert_to"]:
        target = _load_owned(ctx, artifact_id, result["revert_to"])
        if not target:
            return error_envelope(f"version {result['revert_to']} not found")
        art = store.add_version(ctx.rw(), artifact_id, base=base,
                                ops=[{"op": "revert", "to_version": result["revert_to"]}],
                                columns=target["columns"], rows=target["rows"],
                                kind=target["kind"], title=title or target["title"],
                                styling=target.get("styling"),
                                chart=target.get("chart"),
                                map_spec=target.get("map"))
    else:
        art = store.add_version(ctx.rw(), artifact_id, base=base, ops=ops_clean,
                                columns=result["columns"], rows=result["rows"],
                                kind=result["kind"], title=title or result["title"],
                                styling=result["styling"], chart=result["chart"],
                                map_spec=result["map"], provenance=merged_prov)
    ctx.emit("artifact", {"artifact_id": artifact_id, "version": art["version"],
                          "kind": art["kind"], "title": art["title"]})
    new = store.get(ctx.rw(), artifact_id, art["version"])
    return envelope(
        kind=new["kind"],
        summary=f"artifact {artifact_id} -> v{art['version']}: "
                f"{new['row_count']} rows ({new['kind']})",
        artifact=art, columns=new["columns"],
        sample_rows=new["rows"][:10], row_count=new["row_count"],
        provenance=new["provenance"])


@tool_spec(
    name="archive_artifacts",
    description=(
        "Tidy the workspace: hide intermediate artifacts (previews, scratch "
        "searches, single-result tables) from the panel view after you've "
        "consolidated them into a final view with join/concat. Archived "
        "artifacts keep their data and version history — they just stop "
        "rendering. Pass archived=false to bring one back. Use at the END of "
        "multi-step work so the rep sees 1-3 clean panels, not a dozen "
        "intermediates. Free."),
    input_schema={"properties": {
        "artifact_ids": {"type": "array", "items": {"type": "string"}},
        "archived": {"type": "boolean", "default": True},
    }, "required": ["artifact_ids"]},
    cost_class=CostClass.FREE,
)
def archive_artifacts(ctx, artifact_ids: list, archived: bool = True) -> dict:
    done, missing = [], []
    conn = ctx.rw()
    for aid in artifact_ids or []:
        if _load_owned(ctx, str(aid)):
            done.append(str(aid))
        else:
            missing.append(str(aid))
    if done:
        with conn:
            conn.executemany(
                "UPDATE artifacts SET archived=? WHERE artifact_id=?",
                [(1 if archived else 0, a) for a in done])
        ctx.emit("artifact_archived",
                 {"artifact_ids": done, "archived": bool(archived)})
    verb = "archived" if archived else "restored"
    return envelope(
        kind="markdown",
        summary=f"{len(done)} artifacts {verb}"
                + (f"; not found here: {missing}" if missing else ""),
        provenance=[])


@tool_spec(
    name="artifact_peek",
    description=(
        "Read actual rows from an existing artifact when you need concrete "
        "values (e.g. picking which contacts to research). Paged, max 50 "
        "rows per call — transforms and merges happen server-side, so never "
        "try to page a whole large artifact through the conversation. Free."),
    input_schema={"properties": {
        "artifact_id": {"type": "string"},
        "version": {"type": "integer"},
        "offset": {"type": "integer", "default": 0},
        "limit": {"type": "integer", "default": 20, "maximum": 50},
        "cols": {"type": "array", "items": {"type": "string"}},
    }, "required": ["artifact_id"]},
    cost_class=CostClass.FREE,
)
def artifact_peek(ctx, artifact_id: str, version: int | None = None,
                  offset: int = 0, limit: int = 20,
                  cols: list[str] | None = None) -> dict:
    spec = _load_owned(ctx, artifact_id, version)
    if not spec:
        return error_envelope(f"artifact {artifact_id} not found in this conversation")
    limit = max(1, min(int(limit or 20), PEEK_ROWS_CAP))
    offset = max(0, int(offset or 0))
    columns = spec["columns"]
    rows = spec["rows"]
    if cols:
        idx = [i for i, c in enumerate(columns) if c["key"] in cols]
        if not idx:
            return error_envelope(
                f"none of {cols} exist; columns: {[c['key'] for c in columns]}")
        columns = [columns[i] for i in idx]
        rows = [[r[i] for i in idx] for r in rows]
    page = rows[offset:offset + limit]
    return envelope(
        kind="table",
        summary=f"rows {offset}-{offset + len(page) - 1} of {spec['row_count']} "
                f"from {artifact_id} v{spec['version']}",
        columns=columns, sample_rows=page, row_count=spec["row_count"],
        provenance=spec["provenance"], max_sample=PEEK_ROWS_CAP)
