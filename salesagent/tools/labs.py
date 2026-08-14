"""US lab prospecting tools — free reads over SalesAgent's OWN labs estate
(data/estate/labs, built by the labs_build job from the public CMS CLIA
registry). This is the census answer to "find me testing labs": every US
clinical lab with phone numbers, vs. the handfuls OSM/web search surface.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import estate
from ..integrations import clia
from ..jobs.manager import JobManager
from .envelope import envelope, error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec

_STATES_PARAM = {"type": "array", "items": {"type": "string",
                                            "pattern": "^[A-Za-z]{2}$"},
                 "description": "2-letter USPS state codes; omit = national"}


def _labs_prov(manifest: dict) -> dict:
    return prov(
        "CMS CLIA registry (self-built estate)",
        f"run {manifest.get('run')}: {manifest.get('source_file')} — the "
        "official registry of every CLIA-certified US clinical lab, "
        "downloaded fresh from data.cms.gov",
        "https://data.cms.gov")


@tool_spec(
    name="labs_build_reference",
    description=(
        "Build or refresh SalesAgent's OWN labs estate by downloading the "
        "current CMS CLIA registry (official census of every US clinical "
        "lab: ~300k active facilities with name/address/PHONE/certificate/"
        "ownership/test volume). Run when labs_find reports the estate is "
        "missing or stale. National in one shot, ~220MB download, a few "
        "minutes. Free, background job."),
    input_schema={"properties": {
        "force": {"type": "boolean", "default": False,
                  "description": "rebuild even if a fresh estate exists"},
    }},
    cost_class=CostClass.SLOW_JOB,
)
def labs_build_reference(ctx, force: bool = False) -> dict:
    m = estate.current_manifest(ctx.settings, "labs")
    if m and not force:
        fresh_cut = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        if (m.get("built_at") or "") >= fresh_cut:
            c = m.get("counts") or {}
            return envelope(
                kind="markdown",
                summary=f"labs estate already built {m.get('built_at', '')[:10]} "
                        f"({c.get('active', '?')} active labs, "
                        f"{c.get('active_independent', '?')} independent) and "
                        "is <90 days old (CLIA file is quarterly) — no rebuild "
                        "needed. Pass force=true to rebuild anyway.",
                markdown="", provenance=[_labs_prov(m)])
    job_id = JobManager.get(ctx.settings).submit(
        "labs_build", {},
        conversation_id=ctx.conversation_id, user_id=ctx.user.get("id"),
        tool_name="labs_build_reference")
    ctx.emit("job_started", {"job_id": job_id, "tool": "labs_build_reference",
                             "title": "Labs estate build — CLIA registry"})
    return envelope(
        kind="job_ref", job_id=job_id,
        summary=f"labs estate build job {job_id} started — downloading the "
                "current CMS CLIA registry (~220MB), a few minutes. Tell the "
                "rep it's running; labs_find works as soon as it finishes "
                "(job_status checks progress).",
        provenance=[])


def _columns() -> list[dict]:
    cols = []
    for key, label, fmt in clia.FIND_COLUMNS:
        c = {"key": key, "label": label}
        if fmt == "int":
            c["type"] = "number"
            c["format"] = "int"
        else:
            c["type"] = "string"
        cols.append(c)
    return cols


@tool_spec(
    name="labs_find",
    description=(
        "Search the official CLIA registry of ALL US clinical labs (app's "
        "own estate; ~300k active facilities, every one with certificate "
        "data and most with phone numbers). THE tool for lab lead lists — "
        "returns hundreds/thousands where web search returns dozens. "
        "Defaults: independent labs only (facility type 15), active only, "
        "phone required, national chains/franchises screened out. Facility "
        "types include: independent lab, hospital, physician office, "
        "industrial (in-house lab), public health lab, blood bank, mobile "
        "lab... Certificate classes: compliance & accreditation = real "
        "testing operations (best B2B prospects); waiver/PPM = point-of-care "
        "sites (huge volume, low-end consumables). test_volume = annual "
        "tests (size proxy); affiliated_labs > 0 = multi-site operator. "
        "No lat/lng in this registry — map via city/state if needed. "
        "Sort: test_volume|name|state|oldest. Free, local."),
    input_schema={"properties": {
        "states": _STATES_PARAM,
        "fac_types": {"type": "array", "items": {},
                      "description": "facility types, names or codes "
                                     "(default: ['independent lab'])"},
        "cert_types": {"type": "array", "items": {"type": "string"},
                       "description": "compliance|waiver|accreditation|PPM|"
                                      "registration (default: all)"},
        "q": {"type": "string", "description": "lab or city name substring"},
        "require_phone": {"type": "boolean", "default": True},
        "exclude_chains": {"type": "boolean", "default": True,
                           "description": "screen out LabCorp/Quest/plasma/"
                                          "franchise names"},
        "active_only": {"type": "boolean", "default": True},
        "min_test_volume": {"type": "integer"},
        "max_test_volume": {"type": "integer",
                            "description": "cap to target SMALL labs, e.g. 500000"},
        "max_affiliated_labs": {"type": "integer",
                                "description": "0 = single-site only"},
        "sort": {"type": "string",
                 "enum": ["test_volume", "name", "state", "oldest"],
                 "default": "test_volume"},
        "limit": {"type": "integer", "default": 500, "maximum": 5000},
    }},
    cost_class=CostClass.FREE,
)
def labs_find(ctx, **params) -> dict:
    m = estate.current_manifest(ctx.settings, "labs")
    if not m:
        return error_envelope(estate.LABS_MISSING_MSG,
                              error_type="EstateMissing")
    conn = estate.open_labs(ctx.settings)
    try:
        try:
            rows, warnings = clia.query_labs(conn, **params)
        except ValueError as e:
            return error_envelope(str(e), error_type="BadParams")
    finally:
        conn.close()
    cols = _columns()
    keys = [c["key"] for c in cols]
    data = [[r.get(k) for k in keys] for r in rows]
    by_state: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for r in rows:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        by_type[r["fac_type_label"]] = by_type.get(r["fac_type_label"], 0) + 1
    states = params.get("states") or []
    title = (f"Labs — {', '.join(s.upper() for s in states)}" if states
             else "Labs — national")
    hit_limit = len(rows) >= int(params.get("limit") or 500)
    if hit_limit:
        warnings.append(
            f"result hit the {params.get('limit') or 500}-row limit — there "
            "are MORE matching labs; raise limit (max 5000) or narrow by "
            "state to get them all")
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="labs_find", title=title,
        columns=cols, rows=data, provenance=[_labs_prov(m)],
        summary=f"{len(rows)} labs matched"
                + (" (LIMIT HIT — more exist, raise limit)" if hit_limit else "")
                + f". Registry: {m.get('counts', {}).get('active', '?')} "
                  "active US labs total.",
        warnings=warnings,
        stats={"by_state": by_state, "by_facility_type": by_type})
