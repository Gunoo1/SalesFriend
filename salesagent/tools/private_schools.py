"""US private-school prospecting tools — free reads over SalesAgent's OWN
private-schools estate (data/estate/private_schools, built by the pss_build
job from the official NCES Private School Universe Survey). The private-side
complement to the public-district estate: ~22k schools with phone numbers,
enrollment, religious typology, and locale."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import estate
from ..integrations import pss
from ..jobs.manager import JobManager
from .envelope import envelope, error_envelope, prov, table_envelope
from .registry import CostClass, tool_spec

_STATES_PARAM = {"type": "array", "items": {"type": "string",
                                            "pattern": "^[A-Za-z]{2}$"},
                 "description": "2-letter USPS state codes; omit = national"}


def _pss_prov(manifest: dict) -> dict:
    return prov(
        "NCES Private School Universe Survey (self-built estate)",
        f"run {manifest.get('run')}: {manifest.get('source_file')} — the "
        "federal census of US private schools, downloaded fresh from "
        "nces.ed.gov",
        "https://nces.ed.gov/surveys/pss/")


@tool_spec(
    name="private_schools_build_reference",
    description=(
        "Build or refresh SalesAgent's OWN private-schools estate by "
        "downloading the current NCES Private School Universe Survey "
        "(official census: ~22k US private schools with address/PHONE/"
        "enrollment/religious type/grade span/locale). Run when "
        "private_schools_find reports the estate missing. ~4MB download, "
        "under a minute. Free, background job."),
    input_schema={"properties": {
        "force": {"type": "boolean", "default": False,
                  "description": "rebuild even if a fresh estate exists"},
    }},
    cost_class=CostClass.SLOW_JOB,
)
def private_schools_build_reference(ctx, force: bool = False) -> dict:
    m = estate.current_manifest(ctx.settings, "private_schools")
    if m and not force:
        fresh_cut = (datetime.now(timezone.utc)
                     - timedelta(days=365)).isoformat()
        if (m.get("built_at") or "") >= fresh_cut:
            c = m.get("counts") or {}
            return envelope(
                kind="markdown",
                summary=f"private-schools estate already built "
                        f"{m.get('built_at', '')[:10]} "
                        f"({c.get('schools', '?')} schools, "
                        f"{m.get('school_year')}) and is <1 year old (PSS is "
                        "biennial) — no rebuild needed. Pass force=true to "
                        "rebuild anyway.",
                markdown="", provenance=[_pss_prov(m)])
    job_id = JobManager.get(ctx.settings).submit(
        "pss_build", {},
        conversation_id=ctx.conversation_id, user_id=ctx.user.get("id"),
        tool_name="private_schools_build_reference")
    ctx.emit("job_started", {"job_id": job_id,
                             "tool": "private_schools_build_reference",
                             "title": "Private schools estate build — PSS"})
    return envelope(
        kind="job_ref", job_id=job_id,
        summary=f"private-schools estate build job {job_id} started — ~4MB "
                "NCES download, under a minute. private_schools_find works "
                "as soon as it finishes.",
        provenance=[])


def _columns() -> list[dict]:
    cols = []
    for key, label, fmt in pss.FIND_COLUMNS:
        c = {"key": key, "label": label}
        if fmt == "hidden":
            c["type"] = "number"
            c["hidden"] = True
        elif fmt == "int":
            c["type"] = "number"
            c["format"] = "int"
        else:
            c["type"] = "string"
        cols.append(c)
    return cols


@tool_spec(
    name="private_schools_find",
    description=(
        "Search the federal census of ALL US private schools (app's own "
        "estate from the NCES Private School Universe Survey; ~22k schools, "
        "nearly all with phone numbers). THE tool for private-school lead "
        "lists — Catholic/diocesan systems, Christian academies, "
        "nonsectarian prep and special-ed schools. Filters: state, "
        "enrollment, level (elementary/secondary/combined), religious "
        "(catholic | other religious | nonsectarian), rural_only or "
        "locale_groups (city/suburb/town/rural). Returns lat/lng for "
        "mapping. Free, local."),
    input_schema={"properties": {
        "states": _STATES_PARAM,
        "q": {"type": "string", "description": "school or city name substring"},
        "min_enrollment": {"type": "integer",
                           "description": "e.g. 200 = schools big enough "
                                          "for real lab programs"},
        "max_enrollment": {"type": "integer"},
        "levels": {"type": "array", "items": {"type": "integer"},
                   "description": "1=elementary, 2=secondary, 3=combined "
                                  "(secondary+combined = the lab buyers)"},
        "religious": {"type": "array", "items": {},
                      "description": "catholic | other religious | "
                                     "nonsectarian (or codes 1/2/3)"},
        "rural_only": {"type": "boolean", "default": False,
                       "description": "only schools in NCES rural locales "
                                      "(41-43)"},
        "locale_groups": {"type": "array", "items": {"type": "string"},
                          "description": "any of city|suburb|town|rural"},
        "require_phone": {"type": "boolean", "default": True},
        "sort": {"type": "string", "enum": ["enrollment", "name", "state"],
                 "default": "enrollment"},
        "limit": {"type": "integer", "default": 500, "maximum": 5000},
    }},
    cost_class=CostClass.FREE,
)
def private_schools_find(ctx, **params) -> dict:
    m = estate.current_manifest(ctx.settings, "private_schools")
    if not m:
        return error_envelope(estate.PSS_MISSING_MSG,
                              error_type="EstateMissing")
    conn = estate.open_private_schools(ctx.settings)
    try:
        try:
            rows, warnings = pss.query_private_schools(conn, **params)
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
        tl = r.get("typology_label") or "unknown"
        by_type[tl] = by_type.get(tl, 0) + 1
    states = params.get("states") or []
    title = (f"Private schools — {', '.join(s.upper() for s in states)}"
             if states else "Private schools — national")
    if params.get("rural_only"):
        title += " (rural)"
    hit_limit = len(rows) >= int(params.get("limit") or 500)
    if hit_limit:
        warnings.append(
            f"result hit the {params.get('limit') or 500}-row limit — MORE "
            "matching schools exist; raise limit (max 5000) or narrow")
    return table_envelope(
        ctx.rw(), ctx.emit, conversation_id=ctx.conversation_id,
        tool="private_schools_find", title=title,
        columns=cols, rows=data, provenance=[_pss_prov(m)],
        summary=f"{len(rows)} private schools matched"
                + (" (LIMIT HIT — more exist)" if hit_limit else "")
                + f". Estate: {m.get('counts', {}).get('schools', '?')} US "
                  "private schools total.",
        warnings=warnings,
        stats={"by_state": by_state, "by_type": by_type},
        map_spec={"lat": "lat", "lng": "lng", "label": "name",
                  "popup_cols": ["name", "city", "state", "phone"]}
        if any(r.get("lat") for r in rows) else None)
